"""Render a Review as the review document: high-level first, then specific comments by lens."""

from __future__ import annotations

from ..lenses.catalog import ALL_LENSES
from ..models import SEVERITY_ORDER, ConventionRule, Finding, Review

LENS_ORDER = list(ALL_LENSES)
SEV_LABEL = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low", "nit": "Nit"}


def _code(text: str) -> str:
    text = " ".join(text.split())
    fence = "``" if "`" in text else "`"
    return f"{fence}{text}{fence}"


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _where(f: Finding) -> str:
    ev = f.primary()
    if ev.kind == "absence" or ev.file == "(repo)":
        return "repository-wide"
    return f"{ev.file}:{ev.line}" if ev.line else ev.file


def _title(lens: str) -> str:
    return ALL_LENSES[lens].title if lens in ALL_LENSES else lens


def _card(f: Finding) -> str:
    lens_line = f"{_title(f.lens)}" + (f" (also: {', '.join(_title(x) for x in f.lenses if x != f.lens)})" if len(f.lenses) > 1 else "")
    out = [f"#### {f.id} · {SEV_LABEL[f.severity]} · {f.title}", ""]
    out.append(f"_{lens_line} · confidence {f.confidence:.2f} · {'deterministic check' if f.origin == 'static' else 'LLM lens'} · {f.layer} layer_")
    out.append("")
    for ev in f.evidence[:5]:
        if ev.kind == "absence":
            out.append(f"- Searched repository-wide: {ev.quote}")
        elif ev.line:
            out.append(f"- `{ev.file}:{ev.line}`" + (f": {_code(ev.quote)}" if ev.quote else ""))
        else:
            out.append(f"- `{ev.file}`")
    out += ["", f"**What is wrong.** {f.claim}", ""]
    if len(f.rationales) > 1:
        out.append("**Why it matters, by lens.**")
        out += [f"- {_title(k)}: {v}" for k, v in sorted(f.rationales.items())]
        out.append("")
    else:
        out += [f"**Why it matters.** {f.why_it_matters}", ""]
    if f.suggested_fix:
        out += [f"**Suggested fix.** {f.suggested_fix}", ""]
    if f.notes:
        out += ["<sub>" + " · ".join(f.notes) + "</sub>", ""]
    return "\n".join(out)


def _counts(findings: list[Finding]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f.severity == s) for s in SEVERITY_ORDER}


def _rule_row(r: ConventionRule) -> str:
    adh = f"{round(r.adherence * 100)}% of {r.sample}" if r.adherence is not None else "n/a"
    by = f" (enforced by {r.enforced_by})" if r.enforced_by else ""
    return f"| {_cell(r.statement)}{by} | {r.provenance} | {r.status} | {adh} | {_cell(r.source)} |"


def render_markdown(review: Review) -> str:
    min_conf = review.run.get("min_confidence", 0.5)
    main = [f for f in review.findings if f.severity != "nit" and f.confidence >= min_conf]
    minor = [f for f in review.findings if f not in main]
    counts = _counts(review.findings)
    repo_name = review.repo.rstrip("/").removesuffix(".git").split("/")[-1] or review.repo
    sha = (review.head_sha or "unknown")[:7]

    o: list[str] = []
    o.append(f"# Code review: {repo_name}")
    o.append("")
    o.append(f"`{sha}` · mode: {review.mode} · {review.run.get('generated_at', '')[:10]} · content hash `{review.content_hash()}`")
    o.append("")

    # ---- 1. Summary -------------------------------------------------------
    o += ["## 1. Summary", ""]
    o.append(f"**Verdict.** {review.verdict}")
    o.append("")
    if review.objective_statement:
        o += [f"**What this is trying to do.** {review.objective_statement}", ""]
    else:
        o += ["**What this is trying to do.** _The objective lens did not run, so no restatement is available._", ""]
    o.append("**Findings.** " + " · ".join(f"{counts[s]} {s}" for s in SEVERITY_ORDER) + f" ({len(review.findings)} total after verification and de-duplication).")
    o.append("")

    top = main[:8]
    if top:
        o += ["### Top findings", "", "| ID | Severity | Lens | Finding | Where |", "|---|---|---|---|---|"]
        for f in top:
            o.append(f"| {f.id} | {SEV_LABEL[f.severity]} | {_cell(_title(f.lens))} | {_cell(f.title)} | {_cell(_where(f))} |")
        o.append("")

    o += ["### Scorecard by lens", "", "| Lens | Status | Findings | Critical | High | Medium | Low / nit |", "|---|---|---|---|---|---|---|"]
    for lens in LENS_ORDER:
        st = review.lens_status.get(lens)
        if not st:
            continue
        mine = [f for f in review.findings if f.lens == lens]
        c = _counts(mine)
        status = st.get("status", "?")
        if status == "skipped":
            o.append(f"| {_cell(_title(lens))} | skipped | - | - | - | - | - |")
        elif status in {"not_run", "error"}:
            o.append(f"| {_cell(_title(lens))} | {status} | - | - | - | - | - |")
        else:
            o.append(f"| {_cell(_title(lens))} | ran | {len(mine)} | {c['critical']} | {c['high']} | {c['medium']} | {c['low'] + c['nit']} |")
    o.append("")

    if review.conflicts:
        o += ["### Tradeoffs needing a human decision", "", "These are genuine tensions between lenses. The reviewer did not pick a side.", ""]
        for c in review.conflicts:
            o.append(f"- **{c.axis}**")
            for p in c.positions:
                o.append(f"  - _{p['stance']}_ ({', '.join(p['finding_ids'])}; lenses: {', '.join(p['lenses'])})")
        o.append("")

    # ---- 2. Detailed comments ----------------------------------------------
    o += ["## 2. Detailed comments", ""]
    for lens in LENS_ORDER:
        st = review.lens_status.get(lens)
        if not st or st.get("status") == "skipped":
            continue
        o += [f"### {_title(lens)}", ""]
        if st.get("status") in {"not_run", "error"}:
            o += [f"_This lens did not produce findings ({st.get('status')}: {st.get('detail', '')})._", ""]
            continue
        if st.get("summary"):
            o += [st["summary"], ""]
        mine = [f for f in main if f.lens == lens]
        if not mine:
            o += ["_No findings above the confidence threshold._", ""]
        for f in mine:
            o.append(_card(f))

    skipped = {k: v for k, v in review.lens_status.items() if v.get("status") == "skipped"}
    rag_skipped = {k: v for k, v in skipped.items() if v.get("pack") == "rag"}
    if rag_skipped:
        reason = next(iter(rag_skipped.values())).get("detail", "")
        o += ["### RAG assessment", "", f"The RAG pack was **not applied**. {reason}", "",
              "A RAG lens never runs without evidence of a RAG pipeline, so this review makes no claims about retrieval, chunking or RAG evals. "
              "Skipped lenses: " + ", ".join(_title(k) for k in rag_skipped) + ".", ""]
    other_skipped = {k: v for k, v in skipped.items() if v.get("pack") != "rag"}
    if other_skipped:
        o += ["_Also skipped: " + "; ".join(f"{_title(k)} ({v.get('detail', '')})" for k, v in other_skipped.items()) + "._", ""]

    # ---- 3. Conventions -----------------------------------------------------
    o += ["## 3. Conventions applied", ""]
    o.append("Team conventions can only nudge: they are capped below defect severity, and inferred rules stay `unconfirmed` until a human confirms them.")
    o.append("")
    if review.profile.rules:
        o += ["| Convention | Provenance | Status | Adherence | Source |", "|---|---|---|---|---|"]
        o += [_rule_row(r) for r in review.profile.rules]
        o.append("")
    else:
        o += ["_No conventions were declared or inferred._", ""]
    hist = review.profile.history
    if hist:
        o += [f"Review history: {hist.get('prs_scanned', 0)} merged PR(s) scanned" + (f" ({hist['note']})" if hist.get("note") else "") + ".", ""]
    if review.profile.open_questions:
        o += ["### Open questions for the team", "", "Answer these (see `--gate answers`) and the next run will enforce what you confirm.", ""]
        o += [f"{i}. {q}" for i, q in enumerate(review.profile.open_questions, start=1)]
        o.append("")

    # ---- 4. Appendix -------------------------------------------------------
    o += ["## 4. Appendix", ""]
    if minor:
        o += ["### Nits and low-confidence findings", ""]
        for f in minor:
            o.append(f"- **{f.id}** ({SEV_LABEL[f.severity]}, confidence {f.confidence:.2f}) {f.title}: `{_where(f)}`")
        o.append("")
    o += ["### Verification", "", f"{len(review.dropped)} candidate finding(s) were dropped because their evidence could not be verified or they broke a convention rule.", ""]
    if review.dropped:
        o += ["| Lens | Finding | Reason |", "|---|---|---|"]
        o += [f"| {_cell(d.lens)} | {_cell(d.title)} | {_cell(d.reason)} |" for d in review.dropped]
        o.append("")
    lens_models = sorted({v["model"] for v in review.lens_status.values() if v.get("status") == "ok" and v.get("model")})
    o += ["### Run details", "",
          f"- tool version {review.run.get('tool_version')}, lens prompts v{review.run.get('lens_version')}, runner `{review.run.get('runner')}`",
          f"- lens findings produced by: {'; '.join(lens_models) if lens_models else 'no lens ran'}",
          f"- stack: {', '.join(review.stack.get('frameworks', [])) or 'n/a'}; RAG: {'detected' if review.topology.get('rag_applicable') else 'not detected'}; LLM pipeline: {'detected' if review.topology.get('agentic_applicable') else 'not detected'}",
          f"- inline-comment budget {review.run.get('max_inline')}, minimum confidence {min_conf}", ""]
    return "\n".join(o)
