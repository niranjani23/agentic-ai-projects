"""Lens runners.

A runner turns (lens, context) into a LensReport. Two implementations share one interface:

  AnthropicLensRunner - a bounded tool_use loop: the model may call read_file/grep a few times,
                        then must call report_findings. Output is schema-validated and retried once.
  ReplayLensRunner    - loads previously recorded findings from <dir>/<lens>.json. Used for CI
                        without an API key, deterministic regression tests, and for importing
                        findings produced by another agent.

Because the graph only depends on the interface, the LLM is a leaf and the orchestration is testable
without any network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from ..models import Finding, LensReport
from .catalog import render_system_prompt
from .context import LensContext, render_context
from .tools import GREP_TOOL, READ_FILE_TOOL, RepoTools


class LensRunner(Protocol):
    def run(self, ctx: LensContext) -> LensReport: ...


class LensError(RuntimeError):
    pass


_SEVERITIES = ["critical", "high", "medium", "low", "nit"]

REPORT_TOOL = {
    "name": "report_findings",
    "description": "Report this lens's findings. Call exactly once when finished.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "2-4 sentences: what you examined and your overall judgement from this lens."},
            "objective_statement": {"type": "string", "description": "Only for the objective lens: what the repo is trying to achieve, in your own words."},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "layer": {"type": "string", "enum": ["universal", "org", "team"]},
                        "severity": {"type": "string", "enum": _SEVERITIES},
                        "confidence": {"type": "number", "description": "0-1"},
                        "title": {"type": "string", "description": "One line stating the defect, not the topic."},
                        "claim": {"type": "string", "description": "What is wrong, with the failure scenario (input -> wrong output)."},
                        "why_it_matters": {"type": "string"},
                        "suggested_fix": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "file": {"type": "string"},
                                    "line": {"type": "integer"},
                                    "end_line": {"type": "integer"},
                                    "quote": {"type": "string", "description": "Verbatim text from that line, <=160 chars."},
                                },
                                "required": ["file", "line", "quote"],
                            },
                        },
                        "convention_source": {"type": "string", "description": "Rule id from the profile; required when layer=team."},
                        "tradeoff": {
                            "type": "object",
                            "properties": {"axis": {"type": "string"}, "stance": {"type": "string"}},
                            "required": ["axis", "stance"],
                        },
                        "dedupe_key": {"type": "string"},
                    },
                    "required": ["severity", "confidence", "title", "claim", "why_it_matters", "evidence"],
                },
            },
        },
        "required": ["summary", "findings"],
    },
}


def parse_report(lens: str, payload: dict[str, Any], model: str = "", tool_calls: int = 0) -> LensReport:
    """Validate a raw report payload into a LensReport (raises ValidationError)."""
    findings = []
    for raw in payload.get("findings", []):
        item = {k: v for k, v in raw.items() if v not in (None, "")}
        findings.append(Finding(lens=lens, origin="llm", **item))
    return LensReport(
        lens=lens,
        status="ok",
        summary=payload.get("summary", ""),
        objective_statement=payload.get("objective_statement") or None,
        findings=findings,
        model=model,
        tool_calls=tool_calls,
    )


def _tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


class AnthropicLensRunner:
    def __init__(
        self,
        client: Any = None,
        model: str = "claude-sonnet-5-5",
        max_tool_calls: int = 8,
        max_tokens: int = 8192,
        temperature: float | None = 0.0,
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.max_tool_calls = max_tool_calls
        self.max_tokens = max_tokens
        self.temperature = temperature

    def run(self, ctx: LensContext) -> LensReport:
        context_text, _ = render_context(ctx)
        system = render_system_prompt(ctx.spec, self.max_tool_calls)
        repo_tools = RepoTools(ctx.snap)
        tools = [READ_FILE_TOOL, GREP_TOOL, REPORT_TOOL]
        messages: list[dict] = [
            {"role": "user", "content": context_text + "\n\nReview the repository through your lens, then call report_findings."}
        ]
        used = 0
        invalid_reports = 0

        for _ in range(self.max_tool_calls + 4):
            kwargs: dict[str, Any] = dict(model=self.model, max_tokens=self.max_tokens, system=system, tools=tools, messages=messages)
            if self.temperature is not None:
                kwargs["temperature"] = self.temperature
            if used >= self.max_tool_calls or invalid_reports:
                kwargs["tool_choice"] = {"type": "tool", "name": "report_findings"}

            response = self.client.messages.create(**kwargs)
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
            if not calls:
                messages.append({"role": "user", "content": "Call report_findings now with your findings."})
                continue

            results: list[dict] = []
            for block in calls:
                if block.name == "report_findings":
                    try:
                        return parse_report(ctx.spec.name, block.input, model=self.model, tool_calls=used)
                    except (ValidationError, TypeError) as exc:
                        invalid_reports += 1
                        if invalid_reports > 1:
                            raise LensError(f"{ctx.spec.name}: report failed schema validation twice: {exc}") from exc
                        results.append(_tool_result(block.id, f"Invalid report, fix and call report_findings again: {exc}", is_error=True))
                elif block.name in ("read_file", "grep"):
                    if used >= self.max_tool_calls:
                        results.append(_tool_result(block.id, "Tool budget exhausted; call report_findings now.", is_error=True))
                    else:
                        used += 1
                        results.append(_tool_result(block.id, repo_tools.dispatch(block.name, block.input)))
                else:
                    results.append(_tool_result(block.id, f"Unknown tool {block.name}", is_error=True))
            messages.append({"role": "user", "content": results})

        raise LensError(f"{ctx.spec.name}: model never called report_findings")


class ReplayLensRunner:
    """Loads findings recorded earlier. Missing file means the lens was not run, and the review says so."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def run(self, ctx: LensContext) -> LensReport:
        path = self.directory / f"{ctx.spec.name}.json"
        if not path.exists():
            return LensReport(lens=ctx.spec.name, status="not_run", detail=f"no recorded findings at {path.name}")
        try:
            payload = json.loads(path.read_text())
            return parse_report(ctx.spec.name, payload, model=payload.get("model", "replay"))
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            return LensReport(lens=ctx.spec.name, status="error", detail=f"invalid recorded findings: {exc}")
