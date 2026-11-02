"""Synthesis: turn many lenses' raw findings into one trustworthy list.

Fan-out amplifies plausible-sounding noise: six agents confidently wrong in six directions reads as a
wall of authority. Synthesis is where that is stopped, and it is plain code (no LLM) so the same inputs
always give the same output.

Order of operations:
  1. verify evidence anchors against the real files; repair line numbers; drop what cannot be verified
  2. apply inline suppressions and ignored/legacy paths
  3. apply convention layering (team rules can only nudge; unknown/rejected/tool-enforced rules are dropped)
  4. merge duplicates across lenses
  5. surface genuine tradeoffs as conflicts (never silently pick a winner)
  6. rank, assign stable ids, apply the precision budget, compute the verdict
"""

from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timezone

from .. import __version__
from ..models import (
    SEVERITY_RANK,
    Conflict,
    ConventionProfile,
    DroppedFinding,
    Evidence,
    Finding,
    LensReport,
    Review,
    ReviewOptions,
    cap_severity,
    more_severe,
)
from ..lenses.catalog import ALL_LENSES, LENS_VERSION
from ..scout.redact import redact_secrets
from ..scout.snapshot import Snapshot
from ..scout.structure import compute_structure
from ..scout.stack import _CI_RE, _TEST_RE, StackInfo
from ..scout.topology import Topology

_SUPPRESS = re.compile(r"cra:\s*ignore(?:\[([^\]]*)\])?", re.I)
_STOP = set("the and for with that this from are was not but have has its into than then when where which while will would should could can may use used using code file files function functions line lines".split())


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


# ---------------------------------------------------------------------------
# 1. Evidence anchors
# ---------------------------------------------------------------------------
def _verify_absence(snap: Snapshot, ev: Evidence) -> bool | None:
    """True/False for absence claims we can re-check; None when we cannot."""
    q = ev.quote.lower()
    if "test" in q and ("no tests" in q or "no test" in q):
        return not any(_TEST_RE.search(p) for p in snap.files)
    if ".gitignore" in q:
        return ".gitignore" not in snap.files
    if "workflow" in q or "ci" in q.split():
        return not any(_CI_RE.search(p) for p in snap.files)
    return None


def _check_evidence(snap: Snapshot, ev: Evidence, window: int = 3) -> tuple[Evidence | None, str]:
    """Return (possibly repaired evidence, note) or (None, reason)."""
    if ev.kind == "absence":
        verdict = _verify_absence(snap, ev)
        if verdict is False:
            return None, f"absence claim is false: {ev.quote!r}"
        if ev.file != "(repo)" and not snap.exists(ev.file):
            return None, f"file does not exist: {ev.file}"
        return ev, "" if verdict else "absence claim not machine-verified"

    if not snap.exists(ev.file):
        return None, f"file does not exist: {ev.file}"
    lines = snap.lines(ev.file)
    if ev.line == 0 and not ev.quote:
        return ev, ""  # file-level evidence
    if not lines:
        return None, f"{ev.file} is not readable text"
    if not 1 <= ev.line <= len(lines):
        return None, f"line {ev.line} is outside {ev.file} ({len(lines)} lines)"
    if not ev.quote.strip():
        return None, "evidence has no quote to check"

    if ev.redacted:
        key = ev.quote.split("=", 1)[0].strip() if ev.quote.endswith("<redacted>") else None
        actual = lines[ev.line - 1]
        if key is not None:
            ok = actual.split("=", 1)[0].strip().removeprefix("export ").strip() == key
        else:
            ok = _norm(ev.quote) in _norm(redact_secrets(actual))
        return (ev, "") if ok else (None, f"redacted quote does not match {ev.file}:{ev.line}")

    needle = _norm(ev.quote)

    # Models often miscount lines, and GitHub needs the exact line for an inline comment. So snap to the
    # nearest line that really contains the quote, whether it is off by one or by a hundred.
    matches = [i + 1 for i, line in enumerate(lines) if needle and needle in _norm(line)]
    if ev.line in matches:
        return ev, ""
    if matches:
        nearest = min(matches, key=lambda n: abs(n - ev.line))
        return ev.model_copy(update={"line": nearest}), f"line corrected from {ev.line} to {nearest}"

    # A quote may legitimately span a line break; accept it if it sits in the window around the claimed line.
    lo, hi = max(0, ev.line - 1 - window), min(len(lines), ev.line + window)
    if needle and needle in _norm(" ".join(lines[lo:hi])):
        return ev, ""
    return None, f"quote not found in {ev.file}: {ev.quote[:60]!r}"


def validate_anchors(findings: list[Finding], snap: Snapshot) -> tuple[list[Finding], list[DroppedFinding]]:
    kept: list[Finding] = []
    dropped: list[DroppedFinding] = []
    for f in findings:
        good: list[Evidence] = []
        notes: list[str] = []
        reasons: list[str] = []
        for ev in f.evidence:
            fixed, note = _check_evidence(snap, ev)
            if fixed is None:
                reasons.append(note)
            else:
                good.append(fixed)
                if note:
                    notes.append(note)
        if not good:
            dropped.append(DroppedFinding(title=f.title, lens=f.lens, reason="unverifiable evidence: " + "; ".join(reasons)))
            continue
        if reasons:
            notes.append(f"{len(reasons)} evidence item(s) removed as unverifiable")
        update: dict = {"evidence": good, "notes": [*f.notes, *notes]}
        if any("not machine-verified" in n for n in notes):
            update["confidence"] = round(f.confidence * 0.85, 3)
        kept.append(f.model_copy(update=update))
    return kept, dropped


# ---------------------------------------------------------------------------
# 2. Suppressions and ignored paths
# ---------------------------------------------------------------------------
def _suppressed_at(snap: Snapshot, f: Finding) -> int | None:
    """Line number of a `cra: ignore[...]` comment covering this finding's primary evidence, if any."""
    ev = f.primary()
    if ev.kind != "line" or not ev.line:
        return None
    for n in (ev.line, ev.line - 1):
        m = _SUPPRESS.search(snap.line(ev.file, n) or "")
        if m and (not m.group(1) or any(t.strip() in {f.lens, f.dedupe_key, f.convention_source} for t in m.group(1).split(","))):
            return n
    return None


def apply_suppressions(findings: list[Finding], snap: Snapshot, profile: ConventionProfile) -> tuple[list[Finding], list[DroppedFinding]]:
    kept: list[Finding] = []
    dropped: list[DroppedFinding] = []
    for f in findings:
        if (n := _suppressed_at(snap, f)) is not None:
            dropped.append(DroppedFinding(title=f.title, lens=f.lens, reason=f"suppressed inline at {f.primary().file}:{n}"))
            continue
        files = [e.file for e in f.evidence if e.file != "(repo)"]
        if files and profile.ignore_paths and all(any(fnmatch.fnmatch(p, g) for g in profile.ignore_paths) for p in files):
            dropped.append(DroppedFinding(title=f.title, lens=f.lens, reason="all evidence is under an ignored path"))
            continue
        if files and snap.changed is not None and profile.legacy_paths:
            legacy_only = all(any(fnmatch.fnmatch(p, g) for g in profile.legacy_paths) and p not in snap.changed for p in files)
            if legacy_only:
                dropped.append(DroppedFinding(title=f.title, lens=f.lens, reason="untouched legacy code (PR mode)"))
                continue
        kept.append(f)
    return kept, dropped


# ---------------------------------------------------------------------------
# 3. Convention layering
# ---------------------------------------------------------------------------
def apply_convention_layering(findings: list[Finding], profile: ConventionProfile) -> tuple[list[Finding], list[DroppedFinding]]:
    kept: list[Finding] = []
    dropped: list[DroppedFinding] = []
    for f in findings:
        if f.layer != "team":
            kept.append(f)
            continue
        rule = profile.rule(f.convention_source) if f.convention_source else None
        if rule is None:
            dropped.append(DroppedFinding(title=f.title, lens=f.lens, reason=f"team-layer finding cites unknown rule {f.convention_source!r}"))
        elif rule.status == "rejected":
            dropped.append(DroppedFinding(title=f.title, lens=f.lens, reason=f"rule `{rule.id}` was rejected by the team"))
        elif rule.enforced_by:
            dropped.append(DroppedFinding(title=f.title, lens=f.lens, reason=f"`{rule.id}` is enforced by {rule.enforced_by}; the agent stays quiet"))
        else:
            cap = rule.severity_cap if rule.status == "confirmed" else "nit"
            label = "confirmed" if rule.status == "confirmed" else "unconfirmed, inferred"
            kept.append(
                f.model_copy(
                    update={
                        "severity": cap_severity(f.severity, cap),
                        "notes": [*f.notes, f"team convention `{rule.id}` ({label}); a nudge, not a defect"],
                    }
                )
            )
    return kept, dropped


# ---------------------------------------------------------------------------
# 4. Merge duplicates
# ---------------------------------------------------------------------------
def _tokens(f: Finding) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]{3,}", f"{f.title} {f.claim}".lower()) if t not in _STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


# Within one severity tier, wrong results and data exposure outrank hygiene. Confidence is a
# poor importance signal (a certain "no tests" is not more important than a likely wrong number).
LENS_PRIORITY = [
    "governance", "accuracy", "rag-corpus", "rag-retrieval", "rag-generation", "agent-design", "llm-evals",
    "robustness", "rag-evals", "objective", "scalability", "rag-scale", "extensibility", "design-patterns",
    "approach", "maintainability", "reliability",
]


def _lens_rank(f: Finding) -> int:
    ranks = [LENS_PRIORITY.index(x) for x in (f.lenses or [f.lens]) if x in LENS_PRIORITY]
    return min(ranks) if ranks else len(LENS_PRIORITY)


def _sort_key(f: Finding) -> tuple:
    ev = f.primary()
    return (SEVERITY_RANK[f.severity], _lens_rank(f), -f.confidence, f.lens, ev.file, ev.line, f.title)


def should_merge(a: Finding, b: Finding) -> bool:
    if a.tradeoff and b.tradeoff and a.tradeoff.axis == b.tradeoff.axis and a.tradeoff.stance != b.tradeoff.stance:
        return False  # opposing sides of a tradeoff are the point; never merge them
    if a.dedupe_key and a.dedupe_key == b.dedupe_key:
        return True
    near = any(x.file == y.file and x.line and y.line and abs(x.line - y.line) <= 8 for x in a.evidence for y in b.evidence)
    j = _jaccard(_tokens(a), _tokens(b))
    return (near and j >= 0.30) or j >= 0.65


def merge_duplicates(findings: list[Finding]) -> list[Finding]:
    ordered = sorted(findings, key=_sort_key)
    clusters: list[list[Finding]] = []
    for f in ordered:
        for cluster in clusters:
            if should_merge(cluster[0], f):
                cluster.append(f)
                break
        else:
            clusters.append([f])

    merged: list[Finding] = []
    for cluster in clusters:
        base = cluster[0]
        if len(cluster) == 1:
            merged.append(base.model_copy(update={"lenses": [base.lens]}))
            continue
        lenses = sorted({m.lens for m in cluster})
        rationales = {m.lens: m.why_it_matters for m in cluster}
        seen: set[tuple[str, int]] = set()
        evidence: list[Evidence] = []
        for m in cluster:
            for e in m.evidence:
                if e.key() not in seen:
                    seen.add(e.key())
                    evidence.append(e)
        extra = [
            f"Also raised by the {ALL_LENSES[m.lens].title if m.lens in ALL_LENSES else m.lens} lens: {m.claim}"
            for m in cluster[1:]
            if _jaccard(_tokens(base), _tokens(m)) < 0.6 and m.lens != base.lens
        ][:2]
        severity = base.severity
        for m in cluster[1:]:
            severity = more_severe(severity, m.severity)
        merged.append(
            base.model_copy(
                update={
                    "lenses": lenses,
                    "rationales": rationales,
                    "evidence": evidence[:5],
                    "confidence": round(min(0.99, max(m.confidence for m in cluster) + 0.05 * (len(lenses) - 1)), 3),
                    "claim": base.claim + ("\n\n" + "\n\n".join(extra) if extra else ""),
                    "suggested_fix": base.suggested_fix or next((m.suggested_fix for m in cluster if m.suggested_fix), ""),
                    "origin": "static" if any(m.origin == "static" for m in cluster) else base.origin,
                    "severity": severity,
                    "notes": sorted({n for m in cluster for n in m.notes}),
                }
            )
        )
    return merged


# ---------------------------------------------------------------------------
# 5/6. Conflicts, ranking, budget, verdict
# ---------------------------------------------------------------------------
def detect_conflicts(findings: list[Finding]) -> list[Conflict]:
    by_axis: dict[str, dict[str, list[Finding]]] = {}
    for f in findings:
        if f.tradeoff:
            by_axis.setdefault(f.tradeoff.axis, {}).setdefault(f.tradeoff.stance, []).append(f)
    out = []
    for axis, stances in sorted(by_axis.items()):
        if len(stances) >= 2:
            out.append(
                Conflict(
                    axis=axis,
                    positions=[
                        {"stance": s, "finding_ids": [f.id for f in fs], "lenses": sorted({f.lens for f in fs})}
                        for s, fs in sorted(stances.items())
                    ],
                )
            )
    return out


PER_DIMENSION_RESERVE = 2


def _dimension(f: Finding) -> str:
    spec = ALL_LENSES.get(f.lens)
    return spec.dimension if spec else "other"


def select_inline(findings: list[Finding], budget: int, min_confidence: float) -> None:
    """Choose which findings become inline PR comments (the precision budget).

    Eligible: confident, not a nit, and anchored on a real line (repository-wide findings stay in the document).
    Selection, in order, until the budget runs out:
      1. every eligible critical and high finding;
      2. topping up EACH dimension to PER_DIMENSION_RESERVE (severe findings count) with its best remaining findings (product, engineering,
         operations, agentic, rag), so correctness findings cannot crowd design and maintainability out of the PR;
      3. the rest tier by tier, rotating across dimensions within a severity tier.
    `findings` must already be ranked.
    """
    for f in findings:
        f.inline = False
    eligible = [
        f for f in findings
        if f.severity != "nit" and f.confidence >= min_confidence and any(e.kind == "line" and e.line for e in f.evidence)
    ]
    chosen: list[Finding] = []

    def take(f: Finding) -> None:
        if len(chosen) < budget and f not in chosen:
            chosen.append(f)

    per_dim: dict[str, int] = {}
    for f in eligible:
        if f.severity in {"critical", "high"}:
            take(f)
            per_dim[_dimension(f)] = per_dim.get(_dimension(f), 0) + 1  # severe findings count toward their dimension's reserve
    for prefer_new_lens in (True, False):  # within a dimension, spread slots across lenses before doubling up
        for f in eligible:
            d = _dimension(f)
            if f in chosen or per_dim.get(d, 0) >= PER_DIMENSION_RESERVE:
                continue
            if prefer_new_lens and any(c.lens == f.lens for c in chosen):
                continue
            if len(chosen) < budget:
                per_dim[d] = per_dim.get(d, 0) + 1
                take(f)
    # Fill: severity tier by tier; inside a tier, rotate across dimensions (best first in each) instead of
    # pure rank order, so equally severe design and correctness findings share the remaining slots.
    from ..models import SEVERITY_ORDER

    for sev in SEVERITY_ORDER:
        buckets: dict[str, list[Finding]] = {}
        for f in eligible:
            if f.severity == sev and f not in chosen:
                buckets.setdefault(_dimension(f), []).append(f)
        while any(buckets.values()) and len(chosen) < budget:
            for dim in list(buckets):
                if buckets[dim] and len(chosen) < budget:
                    seen = {c.lens for c in chosen}
                    pick = next((f for f in buckets[dim] if f.lens not in seen), buckets[dim][0])  # new lens first
                    buckets[dim].remove(pick)
                    take(pick)
    for f in chosen:
        f.inline = True


def verdict_for(findings: list[Finding]) -> str:
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITY_RANK}
    if counts["critical"]:
        return f"Action required: {counts['critical']} critical and {counts['high']} high-severity issue(s) found."
    if counts["high"]:
        return f"Needs attention: {counts['high']} high-severity issue(s) found."
    if counts["medium"]:
        return f"Reasonable, with improvements recommended: {counts['medium']} medium-severity issue(s)."
    return "Looks sound: only low-severity findings and nits."


def synthesize(
    snap: Snapshot,
    reports: list[LensReport],
    static_findings: list[Finding],
    profile: ConventionProfile,
    topo: Topology,
    stack: StackInfo,
    options: ReviewOptions,
    lens_status: dict[str, dict],
) -> Review:
    raw: list[Finding] = list(static_findings)
    objective = ""
    for r in reports:
        entry = lens_status.setdefault(r.lens, {"pack": "?"})
        entry.update(status=r.status, detail=r.detail, findings=len(r.findings), tool_calls=r.tool_calls, summary=r.summary, model=r.model)
        if r.status == "ok":
            raw.extend(r.findings)
        if r.lens == "objective" and r.objective_statement:
            objective = r.objective_statement

    dropped: list[DroppedFinding] = []
    for step in (
        lambda fs: validate_anchors(fs, snap),
        lambda fs: apply_suppressions(fs, snap, profile),
        lambda fs: apply_convention_layering(fs, profile),
    ):
        raw, d = step(raw)
        dropped.extend(d)

    merged = sorted(merge_duplicates(raw), key=_sort_key)
    for i, f in enumerate(merged, start=1):
        f.id = f"F-{i:03d}"
    select_inline(merged, options.max_inline, options.min_confidence)

    return Review(
        repo=snap.remote or snap.root.name,
        head_sha=snap.head_sha,
        mode=options.mode,
        objective_statement=objective,
        verdict=verdict_for(merged),
        lens_status=lens_status,
        findings=merged,
        conflicts=detect_conflicts(merged),
        dropped=dropped,
        profile=profile,
        topology=topo.to_dict(),
        structure=compute_structure(snap).to_dict() if snap.code_paths("python") else {},
        stack=stack.to_dict(),
        run={
            "tool_version": __version__,
            "lens_version": LENS_VERSION,
            "model": options.model,
            "runner": options.runner,
            "max_inline": options.max_inline,
            "min_confidence": options.min_confidence,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
