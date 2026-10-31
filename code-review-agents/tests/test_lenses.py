from __future__ import annotations

import copy
import json

import pytest

from code_review_agents.lenses.catalog import ALL_LENSES, plan_lenses, render_system_prompt
from code_review_agents.lenses.context import render_context
from code_review_agents.lenses.runner import AnthropicLensRunner, LensError, ReplayLensRunner, parse_report
from code_review_agents.lenses.tools import RepoTools
from code_review_agents.scout.snapshot import load_snapshot
from code_review_agents.scout.topology import Topology

from .conftest import GOOD_REPORT, SAMPLE_APP, FakeClient, make_ctx, tool_use

FAKE_KEY = "sk-ant-api03-" + "Zq9" * 20


# ---- catalog ------------------------------------------------------------------------------------
def test_rag_and_agentic_packs_are_gated_on_topology():
    none = Topology()
    run, status = plan_lenses(none)
    assert {s.pack for s in run} == {"general"}
    assert all(v["status"] == "skipped" for k, v in status.items() if ALL_LENSES[k].pack in {"rag", "agentic"})
    rag = Topology(rag_applicable=True, agentic_applicable=True)
    run, _ = plan_lenses(rag)
    assert {s.pack for s in run} == {"general", "agentic", "rag"}


def test_lens_selection_flag_limits_the_run():
    run, status = plan_lenses(Topology(), only=["accuracy"])
    assert [s.name for s in run] == ["accuracy"] and status["approach"]["detail"].startswith("not selected")


def test_system_prompt_states_the_trust_boundary_and_evidence_rule():
    text = render_system_prompt(ALL_LENSES["accuracy"], 8)
    assert "untrusted DATA" in text and "VERBATIM quote" in text and "budget: 8 calls" in text
    assert "{" not in text.replace("{axis, stance}", "")  # no unformatted placeholders left


# ---- repo tools: the repo under review is untrusted ----------------------------------------------
def test_read_file_is_confined_and_numbered(repo_factory):
    tools = RepoTools(load_snapshot(repo_factory({"app.py": SAMPLE_APP})))
    out = tools.read_file("app.py", 2, 3)
    assert "    2|     total = sum(xs)" in out and "lines 2-3 of 3" in out
    for bad in ("../secrets.txt", "/etc/passwd", "~/x", "nope.py", "pkg/../app.py"):
        assert tools.read_file(bad).startswith("error:")


def test_sensitive_files_are_unreadable_and_secrets_redacted(repo_factory):
    root = repo_factory({".env": f"K={FAKE_KEY}\n", "cfg.py": f'KEY = "{FAKE_KEY}"\n'})
    tools = RepoTools(load_snapshot(root))
    assert tools.read_file(".env").startswith("error:")
    assert FAKE_KEY not in tools.read_file("cfg.py") and "<redacted" in tools.read_file("cfg.py")
    assert FAKE_KEY not in tools.grep("KEY")


def test_grep_respects_glob_and_caps_matches(repo_factory):
    root = repo_factory({"a.py": "hit\n" * 80, "b.txt": "hit\n"})
    tools = RepoTools(load_snapshot(root))
    assert "stopped at 50 matches" in tools.grep("hit", "*.py")
    assert "b.txt" not in tools.grep("hit", "*.py") and tools.grep("zzz") == "(no matches)"


# ---- context pack -------------------------------------------------------------------------------
def test_context_pack_never_contains_secrets_or_secret_files(repo_factory):
    root = repo_factory({".env": f"K={FAKE_KEY}\n", "cfg.py": f'KEY = "{FAKE_KEY}"\n', "app.py": SAMPLE_APP})
    text, included = render_context(make_ctx(root))
    assert FAKE_KEY not in text and ".env" not in included
    assert '<repo_file path="app.py"' in text and "<deterministic_findings" in text


def test_context_budget_truncates_and_lists_omitted_files(repo_factory):
    files = {f"m{i}.py": "x = 1\n" * 400 for i in range(6)}
    text, included = render_context(make_ctx(repo_factory(files), context_chars=6000))
    assert len(included) < 6 and "<not_included" in text


def test_lens_prioritises_files_matching_its_hints(repo_factory):
    files = {"noise.py": "a = 1\n" * 30, "calc.py": "def f(x):\n    return round(x.mean() / 3, 2)  # threshold\n"}
    _, included = render_context(make_ctx(repo_factory(files), lens="accuracy"))
    assert included[0] == "calc.py"


# ---- anthropic runner (scripted client; no network) --------------------------------------------
def test_runner_reads_a_file_then_reports(repo_factory):
    ctx = make_ctx(repo_factory({"app.py": SAMPLE_APP}))
    client = FakeClient([[tool_use("read_file", {"path": "app.py", "start_line": 1, "end_line": 3}, "a")], [tool_use("report_findings", GOOD_REPORT, "b")]])
    report = AnthropicLensRunner(client=client, model="m").run(ctx)
    assert report.status == "ok" and report.tool_calls == 1 and report.model == "m"
    assert report.findings[0].lens == "accuracy" and report.findings[0].origin == "llm"
    first = client.calls[0]
    assert first["temperature"] == 0.0 and first["model"] == "m"
    assert {t["name"] for t in first["tools"]} == {"read_file", "grep", "report_findings"}
    tool_result = client.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "total = sum(xs)" in tool_result["content"]


def test_runner_retries_an_invalid_report_once_with_the_error_visible(repo_factory):
    ctx = make_ctx(repo_factory({"app.py": SAMPLE_APP}))
    broken = copy.deepcopy(GOOD_REPORT)
    broken["findings"][0]["evidence"] = []  # violates "at least one evidence item"
    client = FakeClient([[tool_use("report_findings", broken, "a")], [tool_use("report_findings", GOOD_REPORT, "b")]])
    report = AnthropicLensRunner(client=client).run(ctx)
    assert len(report.findings) == 1
    retry_result = client.calls[1]["messages"][-1]["content"][0]
    assert retry_result["is_error"] is True and "Invalid report" in retry_result["content"]
    assert client.calls[1]["tool_choice"] == {"type": "tool", "name": "report_findings"}


def test_runner_gives_up_after_two_invalid_reports(repo_factory):
    ctx = make_ctx(repo_factory({"app.py": SAMPLE_APP}))
    bad = {"summary": "x", "findings": [{"severity": "high"}]}
    client = FakeClient([[tool_use("report_findings", bad, "a")], [tool_use("report_findings", bad, "b")]])
    with pytest.raises(LensError, match="schema validation twice"):
        AnthropicLensRunner(client=client).run(ctx)


def test_exhausted_tool_budget_forces_the_report(repo_factory):
    ctx = make_ctx(repo_factory({"app.py": SAMPLE_APP}))
    client = FakeClient([
        [tool_use("grep", {"pattern": "sum"}, "a")],
        [tool_use("grep", {"pattern": "len"}, "b")],
        [tool_use("report_findings", GOOD_REPORT, "c")],
    ])
    report = AnthropicLensRunner(client=client, max_tool_calls=2).run(ctx)
    assert report.tool_calls == 2
    assert client.calls[2].get("tool_choice") == {"type": "tool", "name": "report_findings"}
    assert "tool_choice" not in client.calls[0]


def test_model_that_never_reports_raises(repo_factory):
    ctx = make_ctx(repo_factory({"app.py": SAMPLE_APP}))
    client = FakeClient([[tool_use("grep", {"pattern": "x"}, str(i))] for i in range(20)])
    with pytest.raises(LensError, match="never called report_findings"):
        AnthropicLensRunner(client=client, max_tool_calls=1).run(ctx)


def test_temperature_can_be_omitted_for_models_that_reject_it(repo_factory):
    ctx = make_ctx(repo_factory({"app.py": SAMPLE_APP}))
    client = FakeClient([[tool_use("report_findings", GOOD_REPORT)]])
    AnthropicLensRunner(client=client, temperature=None).run(ctx)
    assert "temperature" not in client.calls[0]


def test_report_schema_matches_the_pydantic_model():
    report = parse_report("accuracy", GOOD_REPORT)
    assert report.findings[0].evidence[0].file == "app.py"
    with pytest.raises(Exception):
        parse_report("accuracy", {"summary": "", "findings": [{"severity": "urgent"}]})


# ---- replay runner ------------------------------------------------------------------------------
def test_replay_runner_loads_recorded_findings_and_flags_missing_ones(repo_factory, tmp_path):
    ctx = make_ctx(repo_factory({"app.py": SAMPLE_APP}))
    (tmp_path / "accuracy.json").write_text(json.dumps(GOOD_REPORT))
    runner = ReplayLensRunner(tmp_path)
    assert runner.run(ctx).status == "ok"
    missing = runner.run(make_ctx(repo_factory({"app.py": SAMPLE_APP}), lens="approach"))
    assert missing.status == "not_run" and "approach.json" in missing.detail
    (tmp_path / "approach.json").write_text("{not json")
    assert runner.run(make_ctx(repo_factory({"app.py": SAMPLE_APP}), lens="approach")).status == "error"
