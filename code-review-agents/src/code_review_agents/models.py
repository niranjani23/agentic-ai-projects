"""Shared data models.

Everything that crosses a graph-node boundary is a pydantic model so it can be
validated, serialized, replayed and diffed. Sub-agents return *structured
findings*, never prose, because prose cannot be deduplicated or anchor-checked.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["critical", "high", "medium", "low", "nit"]
Layer = Literal["universal", "org", "team"]
Origin = Literal["static", "llm"]

SEVERITY_ORDER: list[str] = ["critical", "high", "medium", "low", "nit"]
SEVERITY_RANK: dict[str, int] = {s: i for i, s in enumerate(SEVERITY_ORDER)}


def more_severe(a: str, b: str) -> str:
    """Return whichever severity is more severe."""
    return a if SEVERITY_RANK[a] <= SEVERITY_RANK[b] else b


def cap_severity(severity: str, cap: str) -> str:
    """Return `severity`, but never more severe than `cap`."""
    return severity if SEVERITY_RANK[severity] >= SEVERITY_RANK[cap] else cap


class Evidence(BaseModel):
    """A pointer into the reviewed repo that a reader can open and check.

    kind="line":    `quote` must appear on (or within a few lines of) `file:line`.
    kind="absence": the claim is that something is missing; `quote` says what
                    was searched for and is re-verified against the file map.
    redacted=True:  the line holds a secret; `quote` is `KEY=<redacted>` and is
                    checked by key name only. Secret values are never stored.
    """

    kind: Literal["line", "absence"] = "line"
    file: str
    line: int = 0
    end_line: int | None = None
    quote: str = ""
    redacted: bool = False

    @field_validator("file")
    @classmethod
    def _posix(cls, v: str) -> str:
        if v == "(repo)":
            return v
        v = v.replace("\\", "/")
        while v.startswith("./"):  # strip a literal "./" only; never eat the dot of ".env"
            v = v[2:]
        return v

    def key(self) -> tuple[str, int]:
        return (self.file, self.line)


class Tradeoff(BaseModel):
    """Marks a finding as one side of a genuine tension (e.g. chunk size)."""

    axis: str
    stance: str


class Finding(BaseModel):
    id: str = ""
    lens: str
    lenses: list[str] = Field(default_factory=list)
    layer: Layer = "universal"
    severity: Severity
    confidence: float = 0.7
    title: str
    claim: str
    why_it_matters: str
    suggested_fix: str = ""
    evidence: list[Evidence]
    convention_source: str | None = None
    tradeoff: Tradeoff | None = None
    origin: Origin = "llm"
    dedupe_key: str | None = None
    notes: list[str] = Field(default_factory=list)
    rationales: dict[str, str] = Field(default_factory=dict)
    inline: bool = True

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    @field_validator("evidence")
    @classmethod
    def _non_empty(cls, v: list[Evidence]) -> list[Evidence]:
        if not v:
            raise ValueError("a finding needs at least one piece of evidence")
        return v

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        return " ".join(v.split())

    def primary(self) -> Evidence:
        return self.evidence[0]


LensStatus = Literal["ok", "error", "not_run", "skipped"]


class LensReport(BaseModel):
    lens: str
    status: LensStatus = "ok"
    summary: str = ""
    objective_statement: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    detail: str = ""  # error text, or the reason a lens was skipped / not run
    model: str = ""
    tool_calls: int = 0


class Conflict(BaseModel):
    """Two lenses disagree about the same axis. Surfaced, never silently resolved."""

    axis: str
    positions: list[dict]  # [{"stance": str, "finding_ids": [str], "lenses": [str]}]


class DroppedFinding(BaseModel):
    title: str
    lens: str
    reason: str


class ConventionRule(BaseModel):
    id: str
    topic: str
    statement: str
    provenance: Literal["declared", "inferred", "mined", "confirmed"] = "inferred"
    status: Literal["confirmed", "candidate", "rejected", "unconfirmed"] = "candidate"
    adherence: float | None = None
    sample: int = 0
    evidence: list[str] = Field(default_factory=list)  # "path:line" examples
    deviations: list[str] = Field(default_factory=list)
    source: str = ""
    enforced_by: str | None = None  # a linter/formatter owns this; the agent stays quiet
    severity_cap: Severity = "low"


class ConventionProfile(BaseModel):
    repo: str = ""
    fingerprint: str = ""
    rules: list[ConventionRule] = Field(default_factory=list)
    ignore_paths: list[str] = Field(default_factory=list)
    legacy_paths: list[str] = Field(default_factory=list)
    codeowners: list[str] = Field(default_factory=list)
    history: dict = Field(default_factory=dict)  # review-history mining summary
    answers: dict[str, str] = Field(default_factory=dict)
    open_questions: list[str] = Field(default_factory=list)

    def rule(self, rule_id: str) -> ConventionRule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def tool_enforced(self) -> list[ConventionRule]:
        return [r for r in self.rules if r.enforced_by]


class ReviewOptions(BaseModel):
    source: str
    mode: Literal["audit", "pr"] = "audit"
    base: str | None = None
    out_dir: str = "review-out"
    runner: Literal["anthropic", "replay"] = "anthropic"
    replay_dir: str | None = None
    gate: Literal["interactive", "answers", "auto"] = "auto"
    answers_file: str | None = None
    profile_path: str | None = None
    model: str = "claude-sonnet-5-5"
    lenses: list[str] | None = None
    max_inline: int = 12
    min_confidence: float = 0.5
    max_tool_calls: int = 8
    context_chars: int = 140_000
    mine_history: str | None = None  # "owner/repo" to mine merged-PR review comments


class Review(BaseModel):
    repo: str
    head_sha: str | None = None
    mode: str = "audit"
    objective_statement: str = ""
    verdict: str = ""
    lens_status: dict[str, dict] = Field(default_factory=dict)  # lens -> {status, detail, pack}
    findings: list[Finding] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    dropped: list[DroppedFinding] = Field(default_factory=list)
    profile: ConventionProfile = Field(default_factory=ConventionProfile)
    topology: dict = Field(default_factory=dict)
    stack: dict = Field(default_factory=dict)
    run: dict = Field(default_factory=dict)  # models, versions, timestamps (excluded from content hash)

    def content_hash(self) -> str:
        """Stable hash of everything that should be identical across reruns."""
        payload = {
            "repo": self.repo,
            "head_sha": self.head_sha,
            "objective": self.objective_statement,
            "verdict": self.verdict,
            "lens_status": self.lens_status,
            "findings": [f.model_dump(mode="json") for f in self.findings],
            "conflicts": [c.model_dump(mode="json") for c in self.conflicts],
            "rules": [r.model_dump(mode="json") for r in self.profile.rules],
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]
