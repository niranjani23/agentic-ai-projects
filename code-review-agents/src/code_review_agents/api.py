"""Programmatic entry point: `run_review(options)`."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .conventions.gate import AskFn
from .graph import build_graph
from .lenses.runner import AnthropicLensRunner, LensRunner, ReplayLensRunner
from .models import Review, ReviewOptions

_GH_SLUG = re.compile(r"^(?:https?://github\.com/)?([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$")


def resolve_source(source: str, work_dir: str | Path) -> str:
    """A local directory is used as is; `owner/repo` or a GitHub URL is shallow-cloned into work_dir."""
    if Path(source).is_dir():
        return str(Path(source).resolve())
    m = _GH_SLUG.match(source)
    if not m:
        raise FileNotFoundError(f"{source!r} is neither a directory nor a GitHub repo reference (owner/repo)")
    owner, repo = m.groups()
    dest = Path(work_dir) / "_clones" / f"{owner}__{repo}"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--depth", "200", f"https://github.com/{owner}/{repo}", str(dest)],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {proc.stderr.strip()[:300]}")
    return str(dest)


def make_runner(options: ReviewOptions) -> LensRunner:
    if options.runner == "replay":
        if not options.replay_dir:
            raise ValueError("runner 'replay' needs replay_dir")
        return ReplayLensRunner(options.replay_dir)
    return AnthropicLensRunner(model=options.model, max_tool_calls=options.max_tool_calls)


def run_review(
    options: ReviewOptions,
    runner: LensRunner | None = None,
    ask: AskFn | None = None,
    http_get: Any = None,
) -> Review:
    options = options.model_copy(update={"source": resolve_source(options.source, options.out_dir)})
    graph = build_graph(runner or make_runner(options), ask=ask, http_get=http_get)
    state = graph.invoke({"options": options, "lens_reports": []})
    return state["review"]
