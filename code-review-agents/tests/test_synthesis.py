from __future__ import annotations

import random

from code_review_agents.models import ConventionProfile, ConventionRule, Evidence, Finding, LensReport, ReviewOptions, Tradeoff
from code_review_agents.scout.snapshot import load_snapshot
from code_review_agents.scout.stack import detect_stack
from code_review_agents.scout.topology import detect_topology
from code_review_agents.synthesis.merge import (
    apply_convention_layering,
    apply_suppressions,
    detect_conflicts,
    merge_duplicates,
    synthesize,
    validate_anchors,
)

APP = (
    "import os\n"                         # 1
    "\n"                                  # 2
    "def calc(xs):\n"                     # 3
    "    total = sum(xs)\n"               # 4
    "    return total / len(xs)\n"        # 5
    "\n"                                  # 6
    "def other():\n"                      # 7
    "    return os.getcwd()  # cra: ignore[accuracy]\n"  # 8
)


def finding(**kw) -> Finding:
    base = dict(
        lens="accuracy", severity="medium", confidence=0.8, title="Division by zero on empty input",
        claim="calc divides by len(xs) with no guard", why_it_matters="crash", suggested_fix="guard it",
        evidence=[Evidence(file="app.py", line=5, quote="return total / len(xs)")],
    )
    base.update(kw)
    return Finding(**base)


def snap_for(repo_factory, files=None, root=None):
    return load_snapshot(root or repo_factory(files or {"app.py": APP}))


# ---- evidence anchors ---------------------------------------------------------------------------
def test_verified_evidence_is_kept(repo_factory):
    kept, dropped = validate_anchors([finding()], snap_for(repo_factory))
    assert len(kept) == 1 and not dropped and not kept[0].notes


def test_wrong_line_number_is_snapped_to_the_line_that_has_the_quote(repo_factory):
    near = finding(evidence=[Evidence(file="app.py", line=4, quote="return total / len(xs)")])  # off by one
    far = finding(evidence=[Evidence(file="app.py", line=1, quote="return os.getcwd()")])      # off by seven
    kept, dropped = validate_anchors([near, far], snap_for(repo_factory))
    assert not dropped
    assert [k.evidence[0].line for k in kept] == [5, 8]
    assert any("line corrected from 4 to 5" in n for n in kept[0].notes)


def test_hallucinated_quote_is_dropped(repo_factory):
    f = finding(evidence=[Evidence(file="app.py", line=5, quote="return total // (len(xs) + 1)")])
    kept, dropped = validate_anchors([f], snap_for(repo_factory))
    assert not kept and "quote not found" in dropped[0].reason


def test_nonexistent_file_and_out_of_range_line_are_dropped(repo_factory):
    s = snap_for(repo_factory)
    _, d1 = validate_anchors([finding(evidence=[Evidence(file="nope.py", line=1, quote="x")])], s)
    _, d2 = validate_anchors([finding(evidence=[Evidence(file="app.py", line=999, quote="x")])], s)
    assert "does not exist" in d1[0].reason and "outside" in d2[0].reason


def test_bad_evidence_is_pruned_but_a_good_item_keeps_the_finding(repo_factory):
    f = finding(evidence=[Evidence(file="app.py", line=5, quote="invented"), Evidence(file="app.py", line=5, quote="return total / len(xs)")])
    kept, dropped = validate_anchors([f], snap_for(repo_factory))
    assert len(kept) == 1 and not dropped and len(kept[0].evidence) == 1
    assert any("removed as unverifiable" in n for n in kept[0].notes)


def test_redacted_secret_evidence_is_checked_by_key_name(repo_factory):
    s = snap_for(repo_factory, {".env": "API_KEY=abc\n", "a.py": "x = 1\n"})
    good = finding(evidence=[Evidence(file=".env", line=1, quote="API_KEY=<redacted>", redacted=True)])
    bad = finding(evidence=[Evidence(file=".env", line=1, quote="OTHER_KEY=<redacted>", redacted=True)])
    assert validate_anchors([good], s)[0] and not validate_anchors([bad], s)[0]


def test_absence_claims_are_reverified(repo_factory):
    s = snap_for(repo_factory, {"a.py": "x = 1\n", "tests/test_a.py": "def test_a(): pass\n"})
    claim = finding(evidence=[Evidence(kind="absence", file="(repo)", quote="no tests/ directory and no test files")])
    kept, dropped = validate_anchors([claim], s)
    assert not kept and "absence claim is false" in dropped[0].reason


def test_unverifiable_absence_is_kept_with_reduced_confidence(repo_factory):
    claim = finding(confidence=0.8, evidence=[Evidence(kind="absence", file="(repo)", quote="no retry logic anywhere")])
    kept, _ = validate_anchors([claim], snap_for(repo_factory))
    assert kept[0].confidence < 0.8 and any("not machine-verified" in n for n in kept[0].notes)


# ---- suppression --------------------------------------------------------------------------------
def test_inline_suppression_drops_matching_finding(repo_factory):
    s = snap_for(repo_factory)
    f = finding(evidence=[Evidence(file="app.py", line=8, quote="return os.getcwd()")])
    kept, dropped = apply_suppressions([f], s, ConventionProfile())
    assert not kept and "suppressed inline" in dropped[0].reason


def test_suppression_scoped_to_other_lens_does_not_apply(repo_factory):
    s = snap_for(repo_factory)
    f = finding(lens="governance", evidence=[Evidence(file="app.py", line=8, quote="return os.getcwd()")])
    assert apply_suppressions([f], s, ConventionProfile())[0]


def test_ignore_paths_and_untouched_legacy(repo_factory):
    s = snap_for(repo_factory)
    prof = ConventionProfile(ignore_paths=["app.*"])
    assert not apply_suppressions([finding()], s, prof)[0]
    s.changed = set()  # PR mode, nothing changed
    assert not apply_suppressions([finding()], s, ConventionProfile(legacy_paths=["app.py"]))[0]


# ---- convention layering --------------------------------------------------------------------------
def _rule(**kw):
    base = dict(id="early-returns", topic="style", statement="prefer early returns", provenance="declared", status="confirmed")
    base.update(kw)
    return ConventionRule(**base)


def test_confirmed_team_rule_is_capped_below_defect_severity():
    f = finding(layer="team", severity="critical", convention_source="early-returns")
    kept, _ = apply_convention_layering([f], ConventionProfile(rules=[_rule()]))
    assert kept[0].severity == "low"


def test_unconfirmed_team_rule_is_capped_to_nit_and_labelled():
    f = finding(layer="team", severity="high", convention_source="early-returns")
    kept, _ = apply_convention_layering([f], ConventionProfile(rules=[_rule(status="unconfirmed", provenance="inferred")]))
    assert kept[0].severity == "nit" and "unconfirmed" in kept[0].notes[0]


def test_team_finding_citing_unknown_rejected_or_tool_enforced_rule_is_dropped():
    prof = ConventionProfile(rules=[_rule(id="r1", status="rejected"), _rule(id="r2", enforced_by="ruff")])
    for source in ("made-up", "r1", "r2"):
        f = finding(layer="team", convention_source=source)
        kept, dropped = apply_convention_layering([f], prof)
        assert not kept and dropped, source


def test_universal_findings_are_not_capped():
    kept, _ = apply_convention_layering([finding(severity="high")], ConventionProfile())
    assert kept[0].severity == "high"


# ---- merge ------------------------------------------------------------------------------------------
def test_same_dedupe_key_merges_across_lenses_and_keeps_both_rationales():
    a = finding(lens="accuracy", severity="medium", dedupe_key="k", why_it_matters="wrong totals")
    b = finding(lens="llm-evals", severity="high", dedupe_key="k", why_it_matters="unverified claim", title="Different words entirely here")
    [m] = merge_duplicates([a, b])
    assert m.severity == "high" and m.lenses == ["accuracy", "llm-evals"]
    assert m.rationales == {"accuracy": "wrong totals", "llm-evals": "unverified claim"}
    assert m.confidence > 0.8  # independent agreement raises confidence


def test_nearby_similar_findings_merge_but_distant_different_ones_do_not():
    a = finding(lens="accuracy")
    b = finding(lens="scalability", title="Division by zero when xs is empty", claim="calc divides by len(xs) unguarded")
    far = finding(lens="accuracy", title="Totally unrelated cache problem", claim="stale cache entries",
                  evidence=[Evidence(file="other.py", line=90, quote="x")])
    merged = merge_duplicates([a, b, far])
    assert len(merged) == 2


def test_opposing_tradeoff_stances_are_never_merged_and_surface_as_a_conflict():
    big = finding(lens="accuracy", title="Chunks too small", tradeoff=Tradeoff(axis="chunk_size", stance="larger"), dedupe_key="chunks")
    small = finding(lens="rag-scale", title="Chunks too large", tradeoff=Tradeoff(axis="chunk_size", stance="smaller"), dedupe_key="chunks")
    merged = merge_duplicates([big, small])
    assert len(merged) == 2
    for i, f in enumerate(merged, start=1):
        f.id = f"F-{i:03d}"
    [conflict] = detect_conflicts(merged)
    assert conflict.axis == "chunk_size" and {p["stance"] for p in conflict.positions} == {"larger", "smaller"}


# ---- end to end ------------------------------------------------------------------------------------
def _run(repo_factory, reports, static=(), max_inline=12, **kw):
    snap = snap_for(repo_factory, kw.pop("files", None), kw.pop("root", None))
    stack, topo = detect_stack(snap), detect_topology(snap)
    opts = ReviewOptions(source=str(snap.root), max_inline=max_inline, **kw)
    status = {r.lens: {"pack": "general"} for r in reports}
    return synthesize(snap, reports, list(static), ConventionProfile(), topo, stack, opts, status)


def test_ranking_budget_and_ids(repo_factory):
    fs = [finding(title=f"Issue {i} with distinct wording {i * 7}", claim=f"claim {i} words {i * 13}", severity="medium", confidence=0.6 + i / 100,
                  evidence=[Evidence(file="app.py", line=5, quote="return total / len(xs)")]) for i in range(5)]
    fs.append(finding(title="Critical thing", claim="very different text zzz", severity="critical", dedupe_key="c"))
    low_conf = finding(title="Maybe", claim="speculative qqq", confidence=0.2, severity="high", dedupe_key="lc")
    review = _run(repo_factory, [LensReport(lens="accuracy", findings=[*fs, low_conf])], max_inline=2)
    assert review.findings[0].severity == "critical" and review.findings[0].id == "F-001"
    assert [f.id for f in review.findings] == sorted(f.id for f in review.findings)
    assert sum(f.inline for f in review.findings) == 2
    assert not next(f for f in review.findings if f.title == "Maybe").inline  # below min confidence


def test_repository_wide_findings_never_consume_inline_slots(repo_factory):
    absent = finding(title="No tests here at all", claim="nothing", severity="high", evidence=[Evidence(kind="absence", file="(repo)", quote="no retries anywhere")])
    review = _run(repo_factory, [LensReport(lens="accuracy", findings=[absent])])
    assert not review.findings[0].inline


def test_failed_and_unrun_lenses_are_reported_not_hidden(repo_factory):
    reports = [LensReport(lens="accuracy", status="error", detail="boom"), LensReport(lens="approach", status="not_run", detail="no file")]
    review = _run(repo_factory, reports)
    assert review.lens_status["accuracy"]["status"] == "error" and review.lens_status["approach"]["status"] == "not_run"
    assert review.findings == []


def test_objective_statement_comes_from_the_objective_lens(repo_factory):
    reports = [LensReport(lens="objective", objective_statement="Divide numbers safely.")]
    assert _run(repo_factory, reports).objective_statement == "Divide numbers safely."


def test_output_is_deterministic_regardless_of_input_order(repo_factory):
    fs = [finding(title=f"Distinct issue number {i}", claim=f"unique words {i} {i * 31}", severity=sev, dedupe_key=f"k{i}")
          for i, sev in enumerate(["high", "low", "medium", "medium", "nit", "critical"])]
    root = repo_factory({"app.py": APP})  # same repo every time: the head sha is part of the hash by design
    hashes = set()
    for seed in range(6):
        shuffled = list(fs)
        random.Random(seed).shuffle(shuffled)
        hashes.add(_run(repo_factory, [LensReport(lens="accuracy", findings=shuffled)], root=root).content_hash())
    assert len(hashes) == 1
