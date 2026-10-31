from __future__ import annotations

import json
import textwrap

import yaml

from code_review_agents.conventions.gate import BATCH_ID, build_profile, build_questions, resolve_profile
from code_review_agents.conventions.harvest import harvest_declared, infer_python
from code_review_agents.conventions.history import mine_review_history
from code_review_agents.models import ReviewOptions
from code_review_agents.scout.snapshot import load_snapshot

TYPED = "".join(f"def f{i}(x: int) -> int:\n    '''doc'''\n    return x\n\n" for i in range(12))
MIXED = TYPED + "def g(x):\n    return x\n"


def _opts(root, **kw):
    return ReviewOptions(source=str(root), **kw)


def test_declared_rules_from_review_rules_yaml(repo_factory):
    rules_yaml = yaml.safe_dump({
        "version": 1, "ignore_paths": ["vendor/**"], "legacy_paths": ["old/**"],
        "rules": [{"id": "result-types", "topic": "error-handling", "statement": "Domain code returns Result types", "severity_cap": "low"}],
    })
    snap = load_snapshot(repo_factory({".review-rules.yaml": rules_yaml, "a.py": "x = 1\n"}))
    rules, extras = harvest_declared(snap)
    r = next(r for r in rules if r.id == "result-types")
    assert r.provenance == "declared" and r.status == "confirmed"
    assert extras["ignore_paths"] == ["vendor/**"] and extras["legacy_paths"] == ["old/**"]


def test_tool_enforced_rules_come_from_pyproject(repo_factory):
    toml = '[tool.ruff]\nline-length = 100\n\n[tool.mypy]\nstrict = true\n'
    snap = load_snapshot(repo_factory({"pyproject.toml": toml, "a.py": "x = 1\n"}))
    rules, _ = harvest_declared(snap)
    by_id = {r.id: r for r in rules}
    assert by_id["line-length"].enforced_by == "ruff" and "100" in by_id["line-length"].statement
    assert by_id["type-checking"].enforced_by == "mypy"


def test_conventions_bullets_are_harvested_from_docs(repo_factory):
    md = textwrap.dedent("""\
        # Project

        ## Code conventions
        - Prefer early returns over nested conditionals
        - Every public function has a docstring

        ## Other
        - this bullet is outside a conventions section
    """)
    snap = load_snapshot(repo_factory({"CONTRIBUTING.md": md, "a.py": "x = 1\n"}))
    rules, _ = harvest_declared(snap)
    statements = [r.statement for r in rules]
    assert "Prefer early returns over nested conditionals" in statements
    assert not any("outside a conventions section" in s for s in statements)


def test_inference_reports_adherence_with_deviations(repo_factory):
    snap = load_snapshot(repo_factory({"m.py": MIXED}))
    rule = next(r for r in infer_python(snap, set()) if r.id == "type-annotations")
    assert rule.provenance == "inferred" and rule.status == "candidate"
    assert rule.sample == 13 and round(rule.adherence, 2) == 0.92
    assert rule.deviations == ["m.py:49"]


def test_declared_tooling_suppresses_the_inferred_duplicate(repo_factory):
    toml = '[tool.mypy]\nstrict = true\n'
    snap = load_snapshot(repo_factory({"pyproject.toml": toml, "m.py": TYPED}))
    declared, _ = harvest_declared(snap)
    assert "type-annotations" not in {r.id for r in infer_python(snap, {r.id for r in declared})}


def test_rules_below_forty_percent_are_not_conventions(repo_factory):
    code = "".join(f"def f{i}(x):\n    return x\n\n" for i in range(9)) + "def ok(x) -> int:\n    return 1\n"
    snap = load_snapshot(repo_factory({"m.py": code}))
    assert "type-annotations" not in {r.id for r in infer_python(snap, set())}


def test_auto_gate_never_confirms_anything(repo_factory):
    root = repo_factory({"m.py": TYPED})
    profile = resolve_profile(load_snapshot(root), _opts(root, gate="auto"))
    assert profile.rules and all(r.status in {"unconfirmed", "confirmed"} for r in profile.rules)
    assert not [r for r in profile.rules if r.provenance == "inferred" and r.status == "confirmed"]
    assert profile.open_questions


def test_high_adherence_rules_become_one_batch_question(repo_factory):
    root = repo_factory({"m.py": TYPED})
    profile = build_profile(load_snapshot(root), _opts(root))
    qs = build_questions(profile)
    batch = next(q for q in qs if q.id == BATCH_ID)
    assert "type-annotations" in batch.rule_ids and len(batch.rule_ids) >= 1


def test_answers_file_confirms_and_rejects(repo_factory, tmp_path):
    root = repo_factory({"m.py": TYPED})
    answers = tmp_path / "answers.yaml"
    answers.write_text(yaml.safe_dump({BATCH_ID: "enforce", "q-docstrings": "no"}))
    profile = resolve_profile(load_snapshot(root), _opts(root, gate="answers", answers_file=str(answers)))
    ta = profile.rule("type-annotations")
    assert ta.status == "confirmed" and ta.provenance == "confirmed"


def test_interactive_gate_uses_the_ask_callback(repo_factory):
    root = repo_factory({"m.py": TYPED})
    asked = []

    def ask(q):
        asked.append(q.id)
        return "yes"

    profile = resolve_profile(load_snapshot(root), _opts(root, gate="interactive"), ask=ask)
    assert asked and profile.rule("type-annotations").status == "confirmed"
    assert profile.open_questions == []


def test_confirmed_answers_survive_via_profile_cache(repo_factory, tmp_path):
    root = repo_factory({"m.py": TYPED})
    cache = tmp_path / "profile.json"
    first = resolve_profile(load_snapshot(root), _opts(root, gate="interactive", profile_path=str(cache)), ask=lambda q: "enforce")
    assert first.rule("type-annotations").status == "confirmed"
    second = resolve_profile(load_snapshot(root), _opts(root, gate="auto", profile_path=str(cache)))
    assert second.rule("type-annotations").status == "confirmed"  # remembered; nobody was asked again


def test_history_mining_needs_repeated_comments_across_prs():
    def fake_get(url):
        if "/pulls?state=closed" in url:
            return [{"number": n, "merged_at": "2026-01-01"} for n in (1, 2, 3)] + [{"number": 9, "merged_at": None}]
        n = int(url.split("/pulls/")[1].split("/")[0])
        comments = [{"body": "please add a test for this branch", "path": f"a{n}.py", "user": {"type": "User"}},
                    {"body": "Reviewed by bot, ignore this long text", "path": "b.py", "user": {"type": "Bot"}}]
        if n < 3:  # the style nit shows up in only 2 PRs
            comments.append({"body": "nit: spacing here is odd, fix formatting", "path": "b.py", "user": {"type": "User"}})
        return comments

    rules, summary = mine_review_history("o/r", http_get=fake_get)
    assert summary["prs_scanned"] == 3  # the unmerged PR is ignored
    ids = {r.id for r in rules}
    assert "mined-testing" in ids and all(r.provenance == "mined" for r in rules)  # 3 comments over 3 PRs
    assert "mined-style" not in ids  # 2 comments: below the threshold
    assert summary["comments_scanned"] == 5  # bot comments are not counted


def test_history_with_no_prs_degrades_gracefully():
    rules, summary = mine_review_history("o/r", http_get=lambda url: [])
    assert rules == [] and summary["prs_scanned"] == 0 and "note" in summary
    json.dumps(summary)
