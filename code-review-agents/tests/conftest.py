from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_review_agents.conventions.gate import build_profile
from code_review_agents.lenses.catalog import ALL_LENSES
from code_review_agents.lenses.context import LensContext
from code_review_agents.models import ReviewOptions
from code_review_agents.scout.snapshot import load_snapshot
from code_review_agents.scout.stack import detect_stack
from code_review_agents.scout.static_checks import run_static_checks
from code_review_agents.scout.topology import detect_topology

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def make_repo(root: Path, files: dict[str, str]) -> Path:
    """A real git repo with one commit, so tracked-file logic is exercised for real."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A", "-f"], check=True)
    subprocess.run([*GIT, "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    return root


def make_ctx(root: Path, lens: str = "accuracy", **opts) -> LensContext:
    snap = load_snapshot(root)
    stack, topo = detect_stack(snap), detect_topology(snap)
    options = ReviewOptions(source=str(root), **opts)
    return LensContext(ALL_LENSES[lens], snap, stack, topo, run_static_checks(snap, stack, topo), build_profile(snap, options), options)


@pytest.fixture
def repo_factory(tmp_path):
    counter = {"n": 0}

    def factory(files: dict[str, str]) -> Path:
        counter["n"] += 1
        return make_repo(tmp_path / f"repo{counter['n']}", files)

    return factory


# ---- a scripted stand-in for the Anthropic client -------------------------------------------
def tool_use(name: str, payload: dict, id: str = "tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=payload, id=id)


class FakeClient:
    """Plays back scripted assistant turns; records every request so tests can assert on them."""

    def __init__(self, turns: list[list]):
        self.turns = list(turns)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot: the runner appends to this list later
        if not self.turns:
            raise AssertionError("fake client ran out of scripted turns")
        return SimpleNamespace(content=self.turns.pop(0), stop_reason="tool_use")


GOOD_REPORT = {
    "summary": "ok",
    "findings": [
        {
            "severity": "high", "confidence": 0.9, "title": "Division by zero on empty input",
            "claim": "calc() divides by len(xs) without a guard.", "why_it_matters": "crashes",
            "evidence": [{"file": "app.py", "line": 2, "quote": "return total / len(xs)"}],
        }
    ],
}

SAMPLE_APP = "def calc(xs):\n    total = sum(xs)\n    return total / len(xs)\n"
