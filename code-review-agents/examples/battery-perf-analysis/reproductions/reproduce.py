"""Reproduce the defects cited in the battery-perf-analysis review by running the reviewed repo's own code.

    pip install pandas numpy reportlab python-dotenv anthropic pymupdf     # pymupdf is optional (PDF check)
    python reproduce.py /path/to/multi-agent-battery-perf-analysis

Safety: the package is copied to a temp directory first; the target repo is never modified. No LLM is
called and no network is used. All input data is synthetic. (The repo's `.env` is not copied.)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

if len(sys.argv) != 2:
    sys.exit(__doc__)

SRC = Path(sys.argv[1]).resolve() / "battery_agent"
WORK = Path(tempfile.mkdtemp(prefix="repro-"))
shutil.copytree(SRC, WORK / "battery_agent", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
sys.dont_write_bytecode = True
sys.path.insert(0, str(WORK))

import pandas as pd  # noqa: E402

from battery_agent.agents import base  # noqa: E402
from battery_agent.tools import analysis_tools, data_tools, rec_tools  # noqa: E402


def make_df(soc_scale: float = 1.0) -> pd.DataFrame:
    """Two hours of 5-minute data for 4 scenario/schedule combinations."""
    rows, t0 = [], datetime(2026, 1, 26)
    for combo in [("historical", "cleared"), ("historical", "expected"), ("perfect", "cleared"), ("perfect", "expected")]:
        for i in range(24):
            price = 20.0 if i < 12 else 300.0
            perfect = combo[0] == "perfect"
            charge = 1.0 if i < 12 else 0.0
            discharge = 1.0 if (i >= 12 and (perfect or i < 18)) else 0.0
            soc = (min(i, 12) * 8.0 if i < 12 else max(0, 96 - (i - 12) * 8.0)) * soc_scale
            rows.append(dict(SCENARIO_NAME=combo[0], SCHEDULE_TYPE=combo[1], START_DATETIME=(t0 + timedelta(minutes=5 * i)).isoformat(),
                             SOC=soc, CHARGE_ENERGY=charge, DISCHARGE_ENERGY=discharge, PRICE_ENERGY=price, REVENUE=discharge * price - charge * price))
    return pd.DataFrame(rows)


def write(df: pd.DataFrame, name: str) -> str:
    p = WORK / f"{name}.csv"
    df.to_csv(p, index=False)
    return str(p)


def prep(df: pd.DataFrame, name: str = "d"):
    data_tools.reset_state()
    analysis_tools._results.clear()
    path = write(df, name)
    return data_tools.load_csv(path), data_tools.validate_schema(), data_tools.clean_data(), data_tools.summarize_shape()


def say(tag: str, text: str) -> None:
    print(f"{tag}: {text}")


# R01 raw rows are put in the tool result the model receives
a, *_ = prep(make_df())
say("R01 load_csv returns raw rows to the model", f"preview of {len(a['preview'])} records (base.py json-dumps every tool result into the model context)")

# R02 values that are not NA tokens are coerced after validation
df = make_df(); df["REVENUE"] = df["REVENUE"].astype(object); df.loc[df.index[:6], "REVENUE"] = "TBD"
_, b, c, _ = prep(make_df(), "base"); base_rev = analysis_tools.compute_revenue_summary()
_, b, c, _ = prep(df, "tbd"); bad_rev = analysis_tools.compute_revenue_summary()
say("R02 'TBD' in 6 REVENUE cells", f"validate_schema={b['status']} critical_nulls={b['critical_nulls']} rows_dropped={c['rows_dropped']}; "
    f"historical {base_rev['historical_cleared_total_revenue']} -> {bad_rev['historical_cleared_total_revenue']}, gap {base_rev['gap_pct']}% -> {bad_rev['gap_pct']}%")

# R03 duplicates
df = make_df(); dup = df[(df.SCENARIO_NAME == "historical") & (df.SCHEDULE_TYPE == "cleared")].head(3)
prep(make_df(), "base"); n0 = analysis_tools.compare_dispatch()["intervals_compared"]
prep(pd.concat([df, dup]), "dups"); n1 = analysis_tools.compare_dispatch()["intervals_compared"]
say("R03 3 duplicated historical rows", f"intervals_compared {n0} -> {n1}")

# R04 abs() in the primary-driver rule
analysis_tools._results.clear()
out = analysis_tools.find_gap_drivers(
    revenue_summary={"gap_dollars": 100.0, "gap_pct": 10.0},
    high_price_analysis={"missed_revenue_in_high_price": -80.0, "high_price_intervals_count": 12, "threshold_used": 300.0,
                         "historical_cleared_in_high_price": {"total_discharge_MWh": 9.0}, "perfect_cleared_in_high_price": {"total_discharge_MWh": 8.0}},
    dispatch_comparison={"missed_discharge_revenue": 5.0, "missed_discharge_intervals": 1, "unnecessary_charge_intervals": 0},
    soc_analysis={"soc_constrained_discharge_intervals": 0, "historical_cleared_soc": {"mean": 40}, "perfect_cleared_soc": {"mean": 41}})
say("R04 missed revenue = -80 vs gap 100", f"{out['primary_driver']['factor']}: {out['primary_driver']['evidence']}")

# R05 model-supplied arguments override cached results
prep(make_df(), "base")
for fn in (analysis_tools.compute_revenue_summary, analysis_tools.identify_high_price_intervals, analysis_tools.compare_dispatch, analysis_tools.analyze_soc):
    fn()
fab = dict(analysis_tools._results["revenue_summary"]); real = fab["gap_dollars"]; fab["gap_dollars"] = 9999.0
res = analysis_tools.execute_tool("find_gap_drivers", {"revenue_summary": fab, "high_price_analysis": analysis_tools._results["high_price_analysis"],
                                                       "dispatch_comparison": analysis_tools._results["dispatch_comparison"], "soc_analysis": analysis_tools._results["soc_analysis"]})
say("R05 model passes gap_dollars=9999", f"find_gap_drivers uses {res['total_gap_dollars']} while the cached revenue_summary says {analysis_tools._results['revenue_summary']['gap_dollars']} (real {real})")

# R06 the 'evidence' check is a keyword test; non-dict items crash
rec_tools._results.clear()
bogus = [{"action": "X", "reasoning": "We should charge more at night; this will add $48,000 per day.", "expected_benefit": "$48,000/day", "tradeoff": "none"},
         {"action": "Y", "reasoning": "Price spikes are unpredictable, so discharge differently.", "expected_benefit": "+37%", "tradeoff": "none"}]
say("R06 fabricated '$48,000 per day' recommendation", f"status={rec_tools.generate_recommendations(bogus)['status']}")
try:
    rec_tools.execute_tool("generate_recommendations", {"recommendations": ["a string", "another"]})
except Exception as exc:  # noqa: BLE001
    say("R06b string items in recommendations", f"{type(exc).__name__}: {exc} (uncaught)")

# R07 a malformed tool call raises through the agent loop
try:
    data_tools.execute_tool("load_csv", {})
except Exception as exc:  # noqa: BLE001
    say("R07 execute_tool('load_csv', {})", f"{type(exc).__name__}: {exc}")

# R08 iteration cap returns ''
class _Resp:
    stop_reason = "tool_use"
    content = [types.SimpleNamespace(type="tool_use", name="validate_schema", input={}, id="t1")]


class _Client:
    class messages:  # noqa: N801
        @staticmethod
        def create(**_kw):
            return _Resp()


base.MAX_ITERATIONS = 3
say("R08 agent loop at the iteration cap", f"returns {base.run_agent_loop(_Client(), 's', [], 'hi', lambda n, i: {'status': 'ok'})!r}")

# R09 CLI crash when perfect revenue is 0
df = make_df(); df.loc[df.SCENARIO_NAME == "perfect", "REVENUE"] = 0.0
prep(df, "zero"); rs = analysis_tools.compute_revenue_summary()
try:
    f"({rs.get('gap_pct', 'N/A'):.1f}%)"  # the expression on main.py line 127
except Exception as exc:  # noqa: BLE001
    say("R09 perfect revenue = 0", f"gap_pct={rs['gap_pct']} -> {type(exc).__name__}: {exc}")

# R10 battery id from a temp-file name
tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv"); tmp.close()
stem, battery_id = Path(tmp.name).stem.upper(), "BLYTHB1"
for part in stem.split("_"):
    if len(part) >= 5 and any(ch.isdigit() for ch in part):
        battery_id = part
        break
os.unlink(tmp.name)
say("R10 Streamlit-style temp upload", f"{Path(tmp.name).name} -> battery_id {battery_id}")

# R11 SOC as a fraction
prep(make_df(soc_scale=0.01), "socfrac")
for fn in (analysis_tools.compute_revenue_summary, analysis_tools.identify_high_price_intervals, analysis_tools.compare_dispatch):
    fn()
s = analysis_tools.analyze_soc()
say("R11 SOC given as 0-1", f"pct_time_below_20={s['historical_cleared_soc']['pct_time_below_20']}, soc_constrained_discharge_intervals={s['soc_constrained_discharge_intervals']}")

# R12 'missed discharge revenue' is the benchmark's revenue, not the difference
df = make_df()
mask = (df.SCENARIO_NAME == "historical") & (df.SCHEDULE_TYPE == "cleared") & (df.START_DATETIME >= "2026-01-26T01:40:00") & (df.START_DATETIME < "2026-01-26T01:55:00")
df.loc[mask, ["CHARGE_ENERGY", "DISCHARGE_ENERGY", "REVENUE"]] = [1.0, 0.0, -300.0]  # historical bought during the spike
prep(df, "spike"); analysis_tools.compute_revenue_summary(); analysis_tools.identify_high_price_intervals(); d = analysis_tools.compare_dispatch()
dd = data_tools._state["clean_df"]; h = dd[(dd.SCENARIO_NAME == "historical") & (dd.SCHEDULE_TYPE == "cleared")]; p = dd[(dd.SCENARIO_NAME == "perfect") & (dd.SCHEDULE_TYPE == "cleared")]
m = h.merge(p, on="START_DATETIME", suffixes=("_h", "_p")); mk = (m.DISCHARGE_ENERGY_p > 1e-6) & (m.DISCHARGE_ENERGY_h <= 1e-6)
say("R12 historical charged during a spike", f"missed_discharge_revenue={d['missed_discharge_revenue']} vs true perfect-minus-historical={round((m.loc[mk, 'REVENUE_p'] - m.loc[mk, 'REVENUE_h']).sum(), 2)}")

# R13 two thresholds
analysis_tools._results.clear(); hp = analysis_tools.identify_high_price_intervals(threshold=100.0); sa = analysis_tools.analyze_soc()
say("R13 custom high-price threshold", f"identify_high_price_intervals used {hp['threshold_used']}, analyze_soc used {sa['high_price_threshold']}")

# R14 missing historical/cleared rows
df = make_df(); df = df[~((df.SCENARIO_NAME == "historical") & (df.SCHEDULE_TYPE == "cleared"))]
_, b, _, _ = prep(df, "nohist"); rs = analysis_tools.compute_revenue_summary()
say("R14 no historical/cleared rows", f"validate_schema={b['status']}, reported gap_pct={rs['gap_pct']} (gap ${rs['gap_dollars']}), compare_dispatch={analysis_tools.compare_dispatch()['status']}")

# R15 ANTHROPIC_MODEL defined only in .env is ignored (a .env at the package's parent, like the real repo layout)
(WORK / ".env").write_text("ANTHROPIC_MODEL=model-from-dotenv-file\n")
# Run it as a real script file: python-dotenv treats `python -c` as an interactive session and would search the cwd instead of
# the module's directory, which is not how the app is started (`python -m ...` / `streamlit run ...` both have a __main__ file).
probe = WORK / "probe.py"
probe.write_text("import os\nimport battery_agent.orchestrator\nfrom battery_agent.agents import base\nprint(os.environ.get('ANTHROPIC_MODEL'), '|', base.MODEL)\n")
env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_MODEL"} | {"PYTHONPATH": str(WORK), "PYTHONDONTWRITEBYTECODE": "1"}
out = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True, env=env, cwd=tempfile.gettempdir()).stdout.strip()
say("R15 ANTHROPIC_MODEL only in .env", f"os.environ | base.MODEL = {out}")
(WORK / ".env").unlink()

# R16 PDF: long recommendation text in a plain-string table cell
try:
    import fitz  # PyMuPDF

    from battery_agent.report import pdf_generator

    prep(make_df(), "pdf")
    rs, dc, so = analysis_tools.compute_revenue_summary(), None, None
    analysis_tools.identify_high_price_intervals(); dc = analysis_tools.compare_dispatch(); so = analysis_tools.analyze_soc(); gd = analysis_tools.find_gap_drivers()
    long = ("Hold state of charge above 60% ahead of the evening price peak so the battery can discharge for the full high-price window rather than "
            "running out of energy early; historical operation discharged only 6.00 MWh versus 12.00 MWh for the benchmark.")
    report = {"status": "ok", "metadata": {"battery_id": "B1", "analysis_date": "2026-01-26", "run_timestamp": "2026-01-26 10:00:00", "file_path": "x", "user_query": "q"},
              "revenue_summary": rs, "gap_drivers": gd, "dispatch_comparison": dc, "soc_analysis": so,
              "recommendations": {"status": "ok", "recommendations": [{"id": 1, "action": long, "reasoning": long + " " + long, "expected_benefit": long, "tradeoff": long}]}}
    pdf = fitz.open(stream=pdf_generator.generate_pdf(report), filetype="pdf")
    width = pdf[0].rect.width
    over = [round(b[2]) for page in pdf for b in page.get_text("blocks") if b[2] > width - 20]
    say("R16 PDF recommendation table", f"{len(over)} text block(s) reach x={max(over) if over else '-'} on a {width:.0f}pt page (past the margin => cut off)")
except ImportError:
    say("R16 PDF recommendation table", "skipped (pip install pymupdf to run)")

shutil.rmtree(WORK, ignore_errors=True)
