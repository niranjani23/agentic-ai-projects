"""The convention gate.

Principle: never ask what you could have read. After harvesting (declared), inferring
(from code) and mining (from review history), only the *ambiguous remainder* becomes a
question, and each question is a one-line confirmation with evidence, never an open
interview. A CI bot cannot interview anyone, so three modes exist:

  interactive  ask on the terminal
  answers      read answers from a YAML/JSON file (version it with the repo)
  auto         ask nothing; every candidate stays "unconfirmed" and can only nudge (never block)

Whatever is unanswered is listed in the review as "Open questions for the team".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..models import ConventionProfile, ConventionRule, ReviewOptions
from ..scout.snapshot import Snapshot
from .harvest import declared_fingerprint, harvest_declared, infer_python
from .history import mine_review_history

HIGH_ADHERENCE = 0.92
MIN_SAMPLE_FOR_BATCH = 10
BATCH_ID = "batch-consistent"

AskFn = Callable[["Question"], str]


@dataclass
class Question:
    id: str
    prompt: str
    rule_ids: list[str]
    options: tuple[str, ...] = ("enforce", "ignore", "skip")


def _rule_hash(rule: ConventionRule) -> str:
    return hashlib.sha256(f"{rule.id}|{rule.statement}".encode()).hexdigest()[:10]


def build_profile(snap: Snapshot, options: ReviewOptions, http_get=None) -> ConventionProfile:
    declared, extras = harvest_declared(snap)
    declared_ids = {r.id for r in declared}
    inferred = infer_python(snap, declared_ids) if snap.code_paths("python") else []
    mined: list[ConventionRule] = []
    history: dict = {}
    if options.mine_history:
        import os

        mined, history = mine_review_history(options.mine_history, token=os.environ.get("GITHUB_TOKEN"), http_get=http_get)

    topics_declared = {r.topic for r in declared if r.enforced_by}
    rules = declared + [r for r in inferred if not (r.topic == "style" and "style" in topics_declared and r.id.startswith("line-length"))] + mined

    return ConventionProfile(
        repo=snap.remote or str(snap.root.name),
        fingerprint=declared_fingerprint(snap),
        rules=rules,
        ignore_paths=extras.get("ignore_paths", []),
        legacy_paths=extras.get("legacy_paths", []),
        codeowners=extras.get("codeowners", []),
        history=history,
    )


def _example_text(rule: ConventionRule) -> str:
    parts = []
    if rule.evidence:
        parts.append("e.g. " + ", ".join(rule.evidence[:2]))
    if rule.deviations:
        parts.append("exceptions: " + ", ".join(rule.deviations[:2]))
    return (" (" + "; ".join(parts) + ")") if parts else ""


def build_questions(profile: ConventionProfile) -> list[Question]:
    questions: list[Question] = []
    batch: list[ConventionRule] = []
    for r in profile.rules:
        if r.status != "candidate":
            continue
        if r.provenance == "inferred" and (r.adherence or 0) >= HIGH_ADHERENCE and r.sample >= MIN_SAMPLE_FOR_BATCH:
            batch.append(r)
            continue
        if r.provenance == "inferred":
            pct = f"{round((r.adherence or 0) * 100)}% ({round((r.adherence or 0) * r.sample)}/{r.sample})"
            prompt = f"{r.topic}: {pct} of the code follows \"{r.statement}\"{_example_text(r)}. Is this a deliberate team convention, or incidental?"
        else:
            prompt = f"{r.topic}: {r.statement}{_example_text(r)}. Should the reviewer enforce this?"
        questions.append(Question(id=f"q-{r.id}", prompt=prompt, rule_ids=[r.id]))
    if batch:
        listing = "; ".join(f"{r.statement} ({round((r.adherence or 0) * 100)}%)" for r in batch)
        questions.insert(0, Question(id=BATCH_ID, prompt=f"These {len(batch)} patterns are followed almost everywhere: {listing}. Enforce them as team conventions?", rule_ids=[r.id for r in batch]))
    return questions


def _normalize(answer: str) -> str:
    a = (answer or "").strip().lower()
    if a in {"y", "yes", "enforce", "true", "1"}:
        return "enforce"
    if a in {"n", "no", "ignore", "false", "0"}:
        return "ignore"
    return "skip"


def apply_answers(profile: ConventionProfile, questions: list[Question], answers: dict[str, str]) -> ConventionProfile:
    default = answers.get("*")
    for q in questions:
        raw = answers.get(q.id, default)
        if raw is None:
            continue
        decision = _normalize(raw)
        profile.answers[q.id] = decision
        for rid in q.rule_ids:
            rule = profile.rule(rid)
            if not rule:
                continue
            if decision == "enforce":
                rule.status, rule.provenance = "confirmed", "confirmed"
            elif decision == "ignore":
                rule.status = "rejected"
            else:
                rule.status = "unconfirmed"
    return profile


def _carry_over_cached(profile: ConventionProfile, cached: ConventionProfile) -> None:
    """Re-use earlier answers for rules whose id and statement are unchanged."""
    prior = {r.id: r for r in cached.rules}
    for r in profile.rules:
        old = prior.get(r.id)
        if old and old.statement == r.statement and old.status in {"confirmed", "rejected"} and r.status == "candidate":
            r.status = old.status
            r.provenance = old.provenance if old.status == "confirmed" else r.provenance


def load_profile(path: Path) -> ConventionProfile | None:
    try:
        return ConventionProfile.model_validate_json(path.read_text())
    except (OSError, ValueError):
        return None


def save_profile(profile: ConventionProfile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.model_dump_json(indent=2))


def resolve_profile(
    snap: Snapshot,
    options: ReviewOptions,
    ask: AskFn | None = None,
    http_get=None,
) -> ConventionProfile:
    profile = build_profile(snap, options, http_get=http_get)

    cache_path = Path(options.profile_path) if options.profile_path else None
    if cache_path and (cached := load_profile(cache_path)):
        _carry_over_cached(profile, cached)

    questions = build_questions(profile)
    answers: dict[str, str] = {}
    if options.gate == "answers" and options.answers_file:
        raw = Path(options.answers_file).read_text()
        answers = {str(k): str(v) for k, v in (yaml.safe_load(raw) or {}).items()}
    elif options.gate == "interactive":
        ask_fn = ask or _terminal_ask
        for q in questions:
            answers[q.id] = ask_fn(q)

    apply_answers(profile, questions, answers)

    # Anything still a candidate stays usable as a nudge but is explicitly "unconfirmed".
    open_prompts: list[str] = []
    for q in questions:
        was_open = False
        for rid in q.rule_ids:
            rule = profile.rule(rid)
            if rule and rule.status == "candidate":
                rule.status = "unconfirmed"
                was_open = True
        if was_open:
            open_prompts.append(q.prompt)
    profile.open_questions = open_prompts

    if cache_path:
        save_profile(profile, cache_path)
    return profile


def _terminal_ask(q: Question) -> str:  # pragma: no cover - interactive
    print(f"\n? {q.prompt}")
    return input(f"  [{'/'.join(q.options)}] (default skip): ").strip() or "skip"
