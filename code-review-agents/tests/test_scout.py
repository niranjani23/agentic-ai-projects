from __future__ import annotations

import json

from code_review_agents.scout.redact import find_secrets, is_env_file, is_placeholder, redact_secrets
from code_review_agents.scout.snapshot import load_snapshot
from code_review_agents.scout.stack import detect_stack
from code_review_agents.scout.static_checks import run_static_checks
from code_review_agents.scout.topology import detect_topology

FAKE_KEY = "sk-ant-api03-" + "Ab1" * 20  # shaped like a real key, assembled so no literal key sits in source


def _titles(findings):
    return [f.title for f in findings]


def test_snapshot_uses_tracked_files_only(repo_factory, tmp_path):
    root = repo_factory({"a.py": "x = 1\n", ".gitignore": "ignored.txt\n"})
    (root / "ignored.txt").write_text("nope")  # untracked
    snap = load_snapshot(root)
    assert "a.py" in snap.files and "ignored.txt" not in snap.files
    assert snap.head_sha and snap.is_git


def test_resolve_refuses_paths_outside_root(repo_factory):
    snap = load_snapshot(repo_factory({"a.py": "x = 1\n"}))
    assert snap.resolve("../../etc/passwd") is None
    assert snap.resolve("a.py") is not None


# ---- secrets ---------------------------------------------------------------------------------
def test_secret_patterns_and_placeholders():
    assert find_secrets(f"key={FAKE_KEY}")
    assert not find_secrets("key=sk-ant-...")
    assert not find_secrets("key=sk-ant-your-key-here-xxxxxxxxxxxxxxxx")
    assert is_placeholder("changeme") and not is_placeholder(FAKE_KEY)
    assert is_env_file(".env") and is_env_file("cfg/.env.local") and not is_env_file(".env.example")


def test_redact_removes_the_value():
    out = redact_secrets(f"token = '{FAKE_KEY}' # ok")
    assert FAKE_KEY not in out and "<redacted:Anthropic API key>" in out


def test_committed_env_is_critical_and_value_never_leaks(repo_factory):
    root = repo_factory({".env": f"ANTHROPIC_API_KEY={FAKE_KEY}\nDEBUG=\n", "app.py": "x = 1\n"})
    snap = load_snapshot(root)
    findings = run_static_checks(snap, detect_stack(snap))
    secret = next(f for f in findings if f.dedupe_key == "secret:.env")
    assert secret.severity == "critical"
    assert secret.evidence[0].quote == "ANTHROPIC_API_KEY=<redacted>" and secret.evidence[0].redacted
    blob = json.dumps([f.model_dump(mode="json") for f in findings])
    assert FAKE_KEY not in blob and "sk-ant-api03" not in blob


def test_env_with_only_placeholders_is_not_reported(repo_factory):
    root = repo_factory({".env": "ANTHROPIC_API_KEY=your-key-here\n", "app.py": "x = 1\n"})
    snap = load_snapshot(root)
    assert not [f for f in run_static_checks(snap, detect_stack(snap)) if f.dedupe_key and f.dedupe_key.startswith("secret:")]


def test_secret_in_source_file_is_found_and_redacted(repo_factory):
    root = repo_factory({"cfg.py": f'API = "{FAKE_KEY}"\n'})
    snap = load_snapshot(root)
    f = next(f for f in run_static_checks(snap, detect_stack(snap)) if f.dedupe_key == "secret:cfg.py")
    assert f.evidence[0].line == 1 and FAKE_KEY not in f.evidence[0].quote and "<redacted" in f.evidence[0].quote


# ---- hygiene ---------------------------------------------------------------------------------
def test_tracked_artifacts_and_missing_gitignore(repo_factory):
    root = repo_factory({"pkg/__pycache__/m.cpython-312.pyc": "x", "pkg/m.py": "x = 1\n"})
    snap = load_snapshot(root)
    f = next(f for f in run_static_checks(snap, detect_stack(snap)) if f.dedupe_key == "repo-hygiene")
    assert f.severity == "medium" and "no .gitignore" in f.title.lower()


def test_gitignore_present_downgrades_to_low(repo_factory):
    root = repo_factory({".gitignore": "*.log\n", "a.log": "noise", "m.py": "x = 1\n"})
    snap = load_snapshot(root)
    f = next(f for f in run_static_checks(snap, detect_stack(snap)) if f.dedupe_key == "repo-hygiene")
    assert f.severity == "low"


def test_no_tests_reported_only_for_non_trivial_code(repo_factory):
    small = load_snapshot(repo_factory({"a.py": "x = 1\n"}))
    assert "No automated tests" not in _titles(run_static_checks(small, detect_stack(small)))
    big = load_snapshot(repo_factory({"a.py": "x = 1\n" * 150}))
    assert "No automated tests" in _titles(run_static_checks(big, detect_stack(big)))
    with_tests = load_snapshot(repo_factory({"a.py": "x = 1\n" * 150, "tests/test_a.py": "def test_a(): pass\n"}))
    assert "No automated tests" not in _titles(run_static_checks(with_tests, detect_stack(with_tests)))


def test_readme_path_drift(repo_factory):
    root = repo_factory({"README.md": "```bash\npip install -r requirements.txt\n```\n", "app/requirements.txt": "pandas\n", "app/m.py": "x = 1\n"})
    snap = load_snapshot(root)
    f = next(f for f in run_static_checks(snap, detect_stack(snap)) if f.dedupe_key == "readme-path:README.md")
    assert "app/requirements.txt" in f.claim


# ---- import-order bug -------------------------------------------------------------------------
ENV_PKG = {
    "pkg/__init__.py": "",
    "pkg/base.py": 'import os\nMODEL = os.environ.get("MY_MODEL", "default")\n',
    "pkg/orch.py": "from dotenv import load_dotenv\nfrom .base import MODEL\n\nload_dotenv()\n",
}


def test_env_read_before_load_dotenv_is_detected(repo_factory):
    snap = load_snapshot(repo_factory(ENV_PKG))
    f = next(f for f in run_static_checks(snap, detect_stack(snap)) if f.dedupe_key == "env-order:MY_MODEL")
    assert [e.file for e in f.evidence] == ["pkg/base.py", "pkg/orch.py", "pkg/orch.py"]
    assert f.evidence[2].quote == "load_dotenv()"


def test_load_dotenv_first_is_clean(repo_factory):
    files = dict(ENV_PKG)
    files["pkg/orch.py"] = "from dotenv import load_dotenv\n\nload_dotenv()\nfrom .base import MODEL\n"
    snap = load_snapshot(repo_factory(files))
    assert not [f for f in run_static_checks(snap, detect_stack(snap)) if (f.dedupe_key or "").startswith("env-order")]


def test_env_read_inside_function_is_not_flagged(repo_factory):
    files = dict(ENV_PKG)
    files["pkg/base.py"] = 'import os\n\ndef model():\n    return os.environ.get("MY_MODEL", "default")\n'
    snap = load_snapshot(repo_factory(files))
    assert not [f for f in run_static_checks(snap, detect_stack(snap)) if (f.dedupe_key or "").startswith("env-order")]


# ---- shared mutable state ---------------------------------------------------------------------
def test_shared_state_is_high_under_a_web_framework(repo_factory):
    files = {
        "app.py": "import streamlit as st\nfrom tools import run\nst.write(run())\n",
        "tools.py": "_state = {}\n\ndef run():\n    _state['x'] = 1\n    return _state\n",
    }
    snap = load_snapshot(repo_factory(files))
    f = next(f for f in run_static_checks(snap, detect_stack(snap)) if f.dedupe_key == "shared-module-state")
    assert f.severity == "high" and "streamlit" in f.claim


def test_constants_and_unmutated_containers_are_ignored(repo_factory):
    snap = load_snapshot(repo_factory({"m.py": "CONFIG = {}\n_cache = {}\n\ndef f():\n    return _cache\n"}))
    assert not [f for f in run_static_checks(snap, detect_stack(snap)) if f.dedupe_key == "shared-module-state"]


# ---- topology: a RAG lens must never fire on a non-RAG repo ----------------------------------
RAG_REPO = {
    "rag.py": (
        "from sentence_transformers import SentenceTransformer\n"
        "model = SentenceTransformer('all-MiniLM-L6-v2')\n"
        "def search(q, k=5):\n"
        "    return db.execute('SELECT id FROM chunks ORDER BY emb <=> %s LIMIT %s', (model.encode(q), k))\n"
        "def retrieve(q): return search(q)\n"
    )
}


def test_rag_detected_when_embedding_and_store_present(repo_factory):
    topo = detect_topology(load_snapshot(repo_factory(RAG_REPO)))
    assert topo.rag_applicable and {"embed", "store"} <= set(topo.rag_stages)


def test_rag_not_detected_for_plain_llm_app(repo_factory):
    files = {"agent.py": "import anthropic\nc = anthropic.Anthropic()\nr = c.messages.create(model='x', max_tokens=1, messages=[])\nprint(r.stop_reason)\n"}
    topo = detect_topology(load_snapshot(repo_factory(files)))
    assert not topo.rag_applicable and topo.agentic_applicable
    assert "No RAG pipeline detected" in topo.rag_reason


def test_retrieval_words_alone_do_not_make_rag(repo_factory):
    files = {"s.py": "top_k = 5\ndef retriever(x):\n    return x[:top_k]\n"}
    assert not detect_topology(load_snapshot(repo_factory(files))).rag_applicable


def test_credentials_embedded_in_the_git_remote_never_reach_the_snapshot(repo_factory):
    import subprocess

    root = repo_factory({"a.py": "x = 1\n"})
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "https://x-access-token:ghp_SECRETSECRET1234@github.com/o/r.git"], check=True)
    remote = load_snapshot(root).remote
    assert remote == "https://github.com/o/r.git" and "SECRET" not in remote and "@" not in remote
