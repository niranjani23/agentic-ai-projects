from __future__ import annotations

import json
import subprocess
import threading

from code_review_agents.api import run_review
from code_review_agents.cli import main
from code_review_agents.github.poster import (
    GitHub,
    build_review_payload,
    create_baseline_branches,
    parse_commentable_lines,
    parse_pr_ref,
    post_pr_review,
)
from code_review_agents.models import Evidence, Finding, LensReport, Review, ReviewOptions

from .conftest import SAMPLE_APP, make_repo

PLAIN = {"app.py": SAMPLE_APP}
RAG = {
    "rag.py": (
        "from sentence_transformers import SentenceTransformer\nm = SentenceTransformer('x')\n"
        "def s(q):\n    return db.execute('SELECT 1 ORDER BY e <=> %s', (m.encode(q),))\n"
        "def retrieve(q): return s(q)\n"
    )
}


class RecordingRunner:
    """A lens runner that records which lenses were invoked, from which threads."""

    def __init__(self, fail_on: str | None = None):
        self.seen: list[str] = []
        self.threads: set[int] = set()
        self.fail_on = fail_on
        self._lock = threading.Lock()

    def run(self, ctx):
        with self._lock:
            self.seen.append(ctx.spec.name)
            self.threads.add(threading.get_ident())
        if ctx.spec.name == self.fail_on:
            raise RuntimeError("model exploded")
        return LensReport(lens=ctx.spec.name, summary=f"{ctx.spec.name} ok", model="fake")


def _review(root, runner, tmp_path, **kw):
    return run_review(ReviewOptions(source=str(root), out_dir=str(tmp_path / "out"), **kw), runner=runner)


# ---- graph -------------------------------------------------------------------------------------
def test_general_lenses_run_and_rag_pack_is_skipped_for_a_plain_repo(repo_factory, tmp_path):
    runner = RecordingRunner()
    review = _review(repo_factory(PLAIN), runner, tmp_path)
    assert set(runner.seen) == {
        "objective", "approach",                                                                # product
        "design-patterns", "extensibility", "robustness", "maintainability", "reliability",    # engineering
        "scalability", "accuracy", "governance",                                               # operations
    }
    for lens in ("rag-corpus", "rag-retrieval", "rag-generation", "rag-evals", "rag-scale"):
        assert review.lens_status[lens]["status"] == "skipped"
    assert "No RAG pipeline detected" in review.lens_status["rag-retrieval"]["detail"]


def test_rag_pack_runs_when_a_rag_pipeline_is_present(repo_factory, tmp_path):
    runner = RecordingRunner()
    _review(repo_factory(RAG), runner, tmp_path)
    assert {"rag-corpus", "rag-retrieval", "rag-generation", "rag-evals", "rag-scale"} <= set(runner.seen)


def test_lenses_fan_out_in_parallel(repo_factory, tmp_path):
    runner = RecordingRunner()
    _review(repo_factory(PLAIN), runner, tmp_path)
    assert len(runner.threads) >= 1  # LangGraph schedules Send tasks on its executor; at least one worker ran every lens
    assert len(runner.seen) == 10


def test_one_failing_lens_does_not_sink_the_review(repo_factory, tmp_path):
    runner = RecordingRunner(fail_on="accuracy")
    review = _review(repo_factory(PLAIN), runner, tmp_path)
    assert review.lens_status["accuracy"]["status"] == "error" and "model exploded" in review.lens_status["accuracy"]["detail"]
    assert review.lens_status["approach"]["status"] == "ok"
    assert (tmp_path / "out" / "review.md").exists()


def test_lens_subset_flag(repo_factory, tmp_path):
    runner = RecordingRunner()
    _review(repo_factory(PLAIN), runner, tmp_path, lenses=["accuracy"])
    assert runner.seen == ["accuracy"]


def test_all_artifacts_are_written_and_secret_free(repo_factory, tmp_path):
    key = "sk-ant-api03-" + "Qw7" * 20
    root = repo_factory({".env": f"ANTHROPIC_API_KEY={key}\n", "app.py": SAMPLE_APP})
    _review(root, RecordingRunner(), tmp_path)
    out = tmp_path / "out"
    names = {p.name for p in out.iterdir()}
    assert {"review.md", "review.json", "convention_profile.json", "pr_review_payload.json"} <= names
    for p in out.iterdir():
        assert key not in p.read_text(), p.name
    assert "critical" in (out / "review.md").read_text().lower()


def test_same_repo_gives_the_same_content_hash_twice(repo_factory, tmp_path):
    root = repo_factory(PLAIN)
    a = _review(root, RecordingRunner(), tmp_path)
    b = _review(root, RecordingRunner(), tmp_path)
    assert a.content_hash() == b.content_hash()


# ---- github ------------------------------------------------------------------------------------
PATCH = "@@ -1,3 +1,4 @@\n keep\n-removed\n+added one\n+added two\n keep2\n@@ -20,2 +30,2 @@\n ctx\n+new\n"


def test_commentable_lines_come_from_hunks():
    assert parse_commentable_lines(PATCH) == {1, 2, 3, 4, 30, 31}
    assert parse_commentable_lines(None) == set() and parse_commentable_lines("") == set()


def test_pr_ref_parsing():
    assert parse_pr_ref("o/r#12") == ("o", "r", 12)
    assert parse_pr_ref("https://github.com/o/r/pull/7") == ("o", "r", 7)


def _review_obj():
    inside = Finding(id="F-001", lens="accuracy", severity="high", confidence=0.9, title="In diff", claim="c", why_it_matters="w",
                     evidence=[Evidence(file="a.py", line=2, quote="x")], inline=True)
    outside = Finding(id="F-002", lens="accuracy", severity="medium", confidence=0.9, title="Not in diff", claim="c", why_it_matters="w",
                      evidence=[Evidence(file="a.py", line=99, quote="y")], inline=True)
    skipped = Finding(id="F-003", lens="accuracy", severity="low", confidence=0.9, title="Budgeted out", claim="c", why_it_matters="w",
                      evidence=[Evidence(file="a.py", line=1, quote="z")], inline=False)
    return Review(repo="o/r", head_sha="abc", verdict="Needs attention", findings=[inside, outside, skipped], lens_status={"rag-retrieval": {"status": "skipped"}})


def test_payload_only_anchors_comments_on_diff_lines():
    payload = build_review_payload(_review_obj(), commit_id="abc", commentable={"a.py": {1, 2, 3}})
    assert [(c["path"], c["line"], c["side"]) for c in payload["comments"]] == [("a.py", 2, "RIGHT")]
    assert "F-002" in payload["body"] and "could not be attached inline" in payload["body"]
    assert "Budgeted out" not in payload["body"] and payload["event"] == "COMMENT" and payload["commit_id"] == "abc"
    assert "RAG lenses skipped" in payload["body"]


class FakeGh(GitHub):
    def __init__(self):
        self.posted: list[tuple] = []
        self.calls: list[str] = []

    def get(self, path):
        raise AssertionError

    def pr_head_sha(self, owner, repo, number):
        self.calls.append("sha")
        return "deadbeef"

    def pr_files(self, owner, repo, number):
        self.calls.append("files")
        return {"a.py": PATCH}

    def post_review(self, owner, repo, number, payload):
        self.posted.append((owner, repo, number, payload))
        return {"id": 1}


def test_posting_is_off_by_default():
    gh = FakeGh()
    result = post_pr_review(_review_obj(), "o/r#3", post=False, gh=gh)
    assert result["posted"] is False and gh.posted == [] and result["payload"]["commit_id"] == "deadbeef"


def test_posting_requires_the_explicit_flag():
    gh = FakeGh()
    result = post_pr_review(_review_obj(), "o/r#3", post=True, gh=gh)
    assert result["posted"] and gh.posted[0][:3] == ("o", "r", 3)


def test_gh_client_paginates_and_flattens():
    seen = []

    def runner(args, stdin):
        seen.append(args)
        return json.dumps([[{"filename": "a.py", "patch": PATCH}], [{"filename": "b.py"}]])

    files = GitHub(runner).pr_files("o", "r", 1)
    assert set(files) == {"a.py", "b.py"} and files["b.py"] is None
    assert "--paginate" in seen[0] and "--slurp" in seen[0]


def test_baseline_branches_make_every_file_reviewable(tmp_path):
    root = make_repo(tmp_path / "r", {"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"})
    (root / "pkg" / "b.py").write_text("y = 3\n")
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@x.invalid", "commit", "-qam", "change"], check=True)
    info = create_baseline_branches(root)

    def git(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=True).stdout.strip()

    assert git("rev-parse", "--abbrev-ref", "HEAD") == "main"  # caller is returned to where they were
    assert git("ls-tree", "-r", "--name-only", info["base"]) == ""  # base is the empty tree
    assert git("diff", "--name-only", f"{info['base']}...{info['head']}").splitlines() == ["a.py", "pkg/b.py"]
    assert git("rev-parse", f"{info['head']}^{{tree}}") == git("rev-parse", "main^{tree}")  # head tree == source tree
    assert git("merge-base", info["base"], info["head"]) == info["base_sha"]  # shared history, so a PR is possible


# ---- cli ---------------------------------------------------------------------------------------
def test_cli_review_with_replay_runner(repo_factory, tmp_path, capsys):
    root = repo_factory(PLAIN)
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "accuracy.json").write_text(json.dumps({"summary": "s", "findings": [{
        "severity": "high", "confidence": 0.9, "title": "Division by zero on empty input", "claim": "no guard", "why_it_matters": "crash",
        "evidence": [{"file": "app.py", "line": 3, "quote": "return total / len(xs)"}]}]}))
    code = main(["review", str(root), "--runner", "replay", "--replay-dir", str(rec), "--out", str(tmp_path / "o")])
    out = capsys.readouterr().out
    assert code == 0 and "1 high" in out
    review = json.loads((tmp_path / "o" / "review.json").read_text())
    assert any(f["title"] == "Division by zero on empty input" for f in review["findings"])


def test_cli_post_flag_requires_a_pr(repo_factory, tmp_path, capsys):
    root = repo_factory(PLAIN)
    code = main(["review", str(root), "--runner", "replay", "--replay-dir", str(tmp_path), "--out", str(tmp_path / "o"), "--post"])
    assert code == 2 and "--post needs --pr" in capsys.readouterr().err


def test_cli_lens_prompt(repo_factory, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["lens-prompt", "accuracy", str(repo_factory(PLAIN))]) == 0
    out = capsys.readouterr().out
    assert "Accuracy & correctness" in out and "context pack:" in out
    assert main(["lens-prompt", "nonsense", str(repo_factory(PLAIN))]) == 2


def test_cli_errors_are_clean(tmp_path, capsys):
    assert main(["review", str(tmp_path / "missing"), "--runner", "replay", "--replay-dir", str(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err
