"""Build the shared context pack each lens receives.

The scout does the exploring once; lenses read this pack instead of re-discovering the repo
N times (which costs N x tokens and yields N slightly different understandings of it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from ..models import ConventionProfile, Finding, ReviewOptions
from ..scout.redact import is_sensitive_path, redact_secrets
from ..scout.snapshot import Snapshot, is_vendored_or_generated
from ..scout.stack import StackInfo
from ..scout.structure import compute_structure, render_structure
from ..scout.topology import Topology
from .catalog import LensSpec

_SKIP_NAMES = {"package-lock.json", "yarn.lock", "poetry.lock", "uv.lock", "Pipfile.lock"}
_DOC_RE = re.compile(r"(^|/)(readme|contributing|design|architecture|plan|claude|agents)[^/]*\.(md|txt|rst)$", re.I)


@dataclass
class LensContext:
    spec: LensSpec
    snap: Snapshot
    stack: StackInfo
    topo: Topology
    static_findings: list[Finding]
    profile: ConventionProfile
    options: ReviewOptions


def _candidate(snap: Snapshot, path: str) -> bool:
    info = snap.files[path]
    return (
        info.is_text
        and not is_vendored_or_generated(path)
        and not is_sensitive_path(path)
        and PurePosixPath(path).name not in _SKIP_NAMES
        and info.lang in {"python", "typescript", "javascript", "go", "rust", "java", "kotlin", "ruby", "csharp", "cpp", "c", "shell", "sql", "markdown", "text", "yaml", "toml", "json"}
        and info.size <= 200_000
    )


def _score(ctx: LensContext, path: str) -> float:
    info = ctx.snap.files[path]
    text = ctx.snap.read(path) or ""
    score = 0.0
    if info.lang in {"python", "typescript", "javascript", "go", "rust", "java"}:
        score += 5
    if ctx.spec.doc_heavy and _DOC_RE.search(path):
        score += 8
    elif _DOC_RE.search(path):
        score += 1
    if path in ctx.stack.entrypoints:
        score += 3
    if ctx.snap.changed and path in ctx.snap.changed:
        score += 6
    for hint in ctx.spec.file_hints:
        rx = re.compile(hint, re.I)
        if rx.search(path):
            score += 2
        score += min(4, len(rx.findall(text))) * 0.75
    if info.lang in {"json", "yaml", "toml"}:
        score -= 3
    return score - info.lines / 5000  # tie-break toward smaller files


def _numbered(path: str, text: str, start: int, limit_chars: int) -> tuple[str, int]:
    out: list[str] = []
    used = 0
    last = start - 1
    for i, line in enumerate(text.splitlines(), start=1):
        row = f"{i:>5}| {redact_secrets(line)}"
        if used + len(row) + 1 > limit_chars:
            break
        out.append(row)
        used += len(row) + 1
        last = i
    return "\n".join(out), last


def render_context(ctx: LensContext) -> tuple[str, list[str]]:
    """Returns (context text, repo-relative paths whose source is included)."""
    snap, stack, topo, profile = ctx.snap, ctx.stack, ctx.topo, ctx.profile
    header: list[str] = []

    tree = [f"  {p} ({snap.files[p].lines} lines)" for p in sorted(snap.files) if _candidate(snap, p)][:200]
    header.append("<repo_map>\n" + "\n".join(tree) + "\n</repo_map>")
    header.append(
        "<stack>\n"
        f"languages (lines): {stack.languages}\nframeworks: {', '.join(stack.frameworks) or 'none detected'}\n"
        f"entry points: {', '.join(stack.entrypoints) or 'none detected'}\nserved by: {', '.join(stack.web_serving) or 'n/a'}\n"
        f"tests present: {stack.has_tests}  CI present: {stack.has_ci}\n</stack>"
    )
    if snap.code_paths("python"):
        header.append("<structure note=\"deterministic metrics; leads to confirm, not findings\">\n" + render_structure(compute_structure(snap)) + "\n</structure>")
    header.append(f"<topology>\nRAG: {'applicable' if topo.rag_applicable else 'NOT applicable'} - {topo.rag_reason}\nLLM pipeline: {topo.agentic_reason}\n</topology>")

    if ctx.static_findings:
        rows = [f"- [{f.severity}] {f.title} ({f.primary().file}:{f.primary().line})" for f in ctx.static_findings]
        header.append("<deterministic_findings note=\"already reported; do not repeat\">\n" + "\n".join(rows) + "\n</deterministic_findings>")

    conv = []
    for r in profile.rules:
        if r.enforced_by or r.status == "rejected":
            continue
        tag = "CONFIRMED" if r.status == "confirmed" else "UNCONFIRMED (candidate, do not treat as team rule)"
        conv.append(f"- {r.id} [{tag}; {r.provenance}]: {r.statement}")
    enforced = [f"{r.id} ({r.enforced_by})" for r in profile.tool_enforced()]
    header.append("<conventions>\n" + ("\n".join(conv) or "(none)") + "\n</conventions>")
    header.append("<tool_enforced note=\"a linter/formatter owns these; stay silent\">\n" + (", ".join(enforced) or "(none)") + "\n</tool_enforced>")
    if snap.changed is not None:
        header.append("<changed_files note=\"PR mode: prioritise these; the rest is context\">\n" + "\n".join(sorted(snap.changed)) + "\n</changed_files>")

    head_text = "\n\n".join(header)
    remaining = max(0, ctx.options.context_chars - len(head_text))

    ranked = sorted((p for p in snap.files if _candidate(snap, p)), key=lambda p: (-_score(ctx, p), p))
    blocks: list[str] = []
    included: list[str] = []
    for path in ranked:
        if remaining < 400:
            break
        text = snap.read(path)
        if not text:
            continue
        total = text.count("\n") + 1
        body, last = _numbered(path, text, 1, remaining - 200)
        if not body:
            continue
        note = "" if last >= total else f"\n[truncated after line {last} of {total}; use read_file for the rest]"
        block = f'<repo_file path="{path}" lines="1-{last}">\n{body}{note}\n</repo_file>'
        blocks.append(block)
        included.append(path)
        remaining -= len(block)

    omitted = [p for p in ranked if p not in included]
    tail = ""
    if omitted:
        tail = "\n\n<not_included note=\"over budget; reachable with read_file\">\n" + "\n".join(omitted[:60]) + "\n</not_included>"
    return head_text + "\n\n" + "\n\n".join(blocks) + tail, included
