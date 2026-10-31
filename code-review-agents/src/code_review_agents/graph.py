"""The orchestration graph.

LangChain-style code is for leaves (one LLM call); LangGraph is for the tree. This pipeline is all tree:
conditional routing (which packs apply), parallel fan-out (lens agents), shared state and a gate where
a human can answer questions. So the LLM calls are leaves inside `lens`, and everything else is plain,
deterministic, testable Python.

    START -> scout -> conventions -> plan --(Send per lens, in parallel)--> lens -> synthesize -> emit -> END
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .conventions.gate import AskFn, resolve_profile
from .lenses.catalog import LensSpec, plan_lenses
from .lenses.context import LensContext
from .lenses.runner import LensRunner
from .models import ConventionProfile, Finding, LensReport, Review, ReviewOptions
from .outputs import write_outputs
from .scout.snapshot import Snapshot, load_snapshot
from .scout.stack import StackInfo, detect_stack
from .scout.static_checks import run_static_checks
from .scout.topology import Topology, detect_topology
from .synthesis.merge import synthesize


class ReviewState(TypedDict, total=False):
    options: ReviewOptions
    snap: Snapshot
    stack: StackInfo
    topo: Topology
    static_findings: list[Finding]
    profile: ConventionProfile
    lens_specs: list[LensSpec]
    lens_status: dict[str, dict]
    lens_reports: Annotated[list[LensReport], operator.add]  # reducer: parallel lenses append
    review: Review
    outputs: dict[str, str]


def build_graph(runner: LensRunner, ask: AskFn | None = None, http_get: Any = None):
    def scout(state: ReviewState) -> dict:
        opts = state["options"]
        snap = load_snapshot(opts.source, opts.base)
        stack = detect_stack(snap)
        topo = detect_topology(snap)
        return {"snap": snap, "stack": stack, "topo": topo, "static_findings": run_static_checks(snap, stack, topo)}

    def conventions(state: ReviewState) -> dict:
        return {"profile": resolve_profile(state["snap"], state["options"], ask=ask, http_get=http_get)}

    def plan(state: ReviewState) -> dict:
        specs, status = plan_lenses(state["topo"], state["options"].lenses)
        return {"lens_specs": specs, "lens_status": status}

    def fan_out(state: ReviewState):
        sends = [
            Send(
                "lens",
                {
                    "spec": spec,
                    "snap": state["snap"],
                    "stack": state["stack"],
                    "topo": state["topo"],
                    "static_findings": state["static_findings"],
                    "profile": state["profile"],
                    "options": state["options"],
                },
            )
            for spec in state["lens_specs"]
        ]
        return sends or "synthesize"

    def lens(payload: dict) -> dict:
        spec: LensSpec = payload["spec"]
        ctx = LensContext(
            spec=spec, snap=payload["snap"], stack=payload["stack"], topo=payload["topo"],
            static_findings=payload["static_findings"], profile=payload["profile"], options=payload["options"],
        )
        try:
            report = runner.run(ctx)
        except Exception as exc:  # one failing lens must not sink the whole review
            report = LensReport(lens=spec.name, status="error", detail=f"{type(exc).__name__}: {str(exc)[:300]}")
        return {"lens_reports": [report]}

    def synth(state: ReviewState) -> dict:
        reports = sorted(state.get("lens_reports", []), key=lambda r: r.lens)  # parallel completion order is arbitrary
        review = synthesize(
            state["snap"], reports, state["static_findings"], state["profile"],
            state["topo"], state["stack"], state["options"], state["lens_status"],
        )
        return {"review": review}

    def emit(state: ReviewState) -> dict:
        return {"outputs": write_outputs(state["review"], state["options"].out_dir)}

    g = StateGraph(ReviewState)
    g.add_node("scout", scout)
    g.add_node("conventions", conventions)
    g.add_node("plan", plan)
    g.add_node("lens", lens)
    g.add_node("synthesize", synth)
    g.add_node("emit", emit)
    g.add_edge(START, "scout")
    g.add_edge("scout", "conventions")
    g.add_edge("conventions", "plan")
    g.add_conditional_edges("plan", fan_out, ["lens", "synthesize"])
    g.add_edge("lens", "synthesize")
    g.add_edge("synthesize", "emit")
    g.add_edge("emit", END)
    return g.compile()
