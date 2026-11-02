# Design and feature spec

## 1. Purpose and scope

Given a GitHub repo or a pull request, the system reviews it with specialist sub-agents and returns one document: a verdict first, then specific comments grouped by three dimensions that apply to every codebase:

| Dimension | The question it answers | Lenses |
|---|---|---|
| **Product** | Is this the right thing, built the right way? | Objective & fit, Approach & architecture |
| **Engineering quality** | Will this codebase survive change? | Design patterns & abstractions, Extensibility & modularity, Robustness & error handling, Maintainability & readability, Testing & observability |
| **Operations** | Does it hold up in use? | Scalability & cost, Accuracy & correctness, Security, privacy & governance |

Two further packs run only when the scout finds evidence they apply: **LLM / agent design** (the code calls a model or runs a tool loop) and **RAG** (the code contains a retrieval pipeline). Domain packs are additions to the core review, not its center.

- **Two modes.** Repo audit reviews the whole tree. PR review (`--base REF`) puts inline comments on changed lines and uses the rest of the repo as context.
- **Measures before it judges.** Structure metrics (function size and complexity, coupling, import cycles) are computed deterministically and handed to the design lenses as leads.
- **Asks only what it cannot read.** Conventions are harvested from config, inferred from code and mined from past review comments. Humans are asked only about the ambiguous remainder.
- **Three outputs.** A Markdown review, a JSON, and a GitHub review payload.
- **Non-goals.** It does not replace linters or CI, does not auto-fix code, and never posts unless explicitly told to.

## 2. Rule layers and provenance

A security hole and a disliked naming style must never carry the same weight, or engineers learn to ignore the agent.

| Layer | Covers | Can it block? |
|---|---|---|
| Universal | Correctness, security, data loss, concurrency, and design defects with a demonstrated cost | Yes: critical to low |
| Org | Compliance, logging and PII policy shared across teams | In the model; no org profile mechanism yet |
| Team | House conventions: error style, naming, test style, structure | No. Capped at low when confirmed, nit when unconfirmed |

Each convention carries a provenance: **declared**, **inferred** (with an adherence rate), **mined** (from review history) or **confirmed** (a human said yes). A team finding must cite a rule id that exists in the profile. A finding citing an unknown, rejected or linter-enforced rule is dropped.

The line between "design defect" and "team taste" matters most for the engineering lenses. A design finding is universal only when it names a concrete cost in this code (files touched per change, a bug it caused, a test it prevents). Otherwise it is a convention or it is left out.

## 3. Convention gate

Never ask what could have been read. Sources, in order:

1. **Declared:** `.review-rules.yaml`; bullets under convention-style headings in `REVIEW.md`, `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`; `pyproject.toml` (ruff, black, mypy, isort); `setup.cfg`/`.flake8`; `.editorconfig`; `.pre-commit-config.yaml`; `package.json`; `tsconfig.json`; `CODEOWNERS`. Tool-enforced rules are marked and never commented on.
2. **Inferred (Python):** return annotations, docstrings, error signalling (returned error values vs raised), logging vs print, relative vs absolute imports, quote style, line length, naming, test framework. Each has an adherence rate plus example and exception locations.
3. **Mined:** merged-PR review comments, clustered by topic. A topic needs at least 3 comments across 2 PRs.
4. **Asked:** only the remainder. 92%+ adherence with 10+ samples is batched into one yes/no question; 40-92% gets a one-line question with examples; below 40% is treated as no convention.

| Mode | Behaviour | Unanswered rules |
|---|---|---|
| `interactive` | asks in the terminal | unconfirmed |
| `answers` | reads a YAML file kept in the repo | unconfirmed |
| `auto` | asks nothing (CI) | unconfirmed, capped at nit |

Confirmed answers are cached in the profile and reused only while a rule's id and statement are unchanged. Unanswered questions are printed in the review.

## 4. Orchestration

The scout explores once; every lens reads the shared context pack. Otherwise N lens agents would re-explore the repo at N times the cost and reach N slightly different understandings of it.

The orchestration is a LangGraph tree (`graph.py`): LLM calls are leaves inside the `lens` node; everything else is deterministic Python. LangGraph provides conditional routing (which packs apply), parallel fan-out (`Send`, one task per lens) and shared state.

```
START -> scout -> conventions -> plan --(Send per lens, parallel)--> lens -> synthesize -> emit -> END
```

The scout produces: a file map, the stack, the topology (LLM calls? RAG pipeline?), deterministic findings, and **structure metrics** (section 6). Lens agents get two read-only tools, `read_file` and `grep` (budget 8 calls), and must finish with `report_findings`, which is schema-validated and retried once. A lens that fails or never reports is marked `error` or `not_run`; the rest still complete.

## 5. Lens catalog

17 lenses: 10 always run, 7 are conditional.

| Dimension | Lens | What it checks | Runs when |
|---|---|---|---|
| Product | Objective & fit | restates the objective; docs vs code; inputs accepted but ignored | always |
| Product | Approach & architecture | proportionate system design; component boundaries and data flow; documented vs implemented design | always |
| Engineering | Design patterns & abstractions | patterns in use and whether they earn their keep; missing abstractions with a concrete cost (growing if/elif dispatch, loose dicts, copy-paste variants); anti-patterns (god functions, hidden globals, temporal coupling, leaky abstractions, flag arguments); SOLID where it bites | always |
| Engineering | Extensibility & modularity | traces the 2-3 most likely next changes and counts what each touches; seams and open/closed violations; config vs code; dependency direction, cycles, fan-in/out; implicit vs explicit contracts | always |
| Engineering | Robustness & error handling | validation at trust boundaries; one consistent error strategy; failure isolation and partial results; resource cleanup; timeouts, retries, idempotency; unchecked invariants | always |
| Engineering | Maintainability & readability | complexity hot spots; duplication; misleading names and magic numbers; typed vs implicit data shapes; dead code and stale docs | always |
| Engineering | Testing & observability | what is untested that matters; what in the design blocks testing; debuggability after the fact; reproducibility | always |
| Operations | Scalability & cost | what breaks first at 10x/100x; concurrency and isolation; external-call counts | always |
| Operations | Accuracy & correctness | wrong results with a failing input; label vs computation; edge cases | always |
| Operations | Security, privacy & governance | secrets; what leaves the machine; trust boundaries; doc claims | always |
| LLM / agent | Agent & tool-loop design | stop reasons; tool contracts; model-typed data vs cached truth | LLM calls found |
| LLM / agent | LLM grounding & evaluation | claims checked against data; evals; fallbacks | LLM calls found |
| RAG | Corpus governance & ingestion | provenance and ACLs at ingest and at query; re-index path | embedding + store/retriever found |
| RAG | Retrieval quality | chunking; embedding prefixes; hybrid search; rank fusion; post-filtering | same |
| RAG | Generation integrity | context order and overflow; corpus as untrusted input; refusal path | same |
| RAG | Evaluation architecture | retrieval and generation scored separately; golden-set provenance; judge | same |
| RAG | Scale, cost & observability | ANN params; re-embedding; cache keys; logged chunk ids | same |

Two rules keep the engineering lenses honest:
- **No pattern for its own sake.** A lens may recommend a pattern only when it removes a demonstrated cost in this code, and should say so when the simple version is the right call.
- **No speculative generality.** The extensibility lens names the likely extensions from evidence (the README, the plan, the data) and counts their cost today. It does not propose plugin systems for changes nothing points to.

A conditional pack never runs without evidence, and the review prints that evidence.

## 6. Structure metrics (deterministic)

"This module is too coupled" is an opinion; "this module imports 7 internal modules" is a fact a reader can check. The scout measures, the lenses interpret.

| Metric | Per | Flag threshold (a lead, not a defect) |
|---|---|---|
| Length (lines) | function | >= 60 |
| Cyclomatic complexity | function | >= 10 |
| Parameters | function | >= 5 |
| Nesting depth | function | >= 4 |
| Methods | class | >= 15 |
| Fan-in / fan-out (internal imports) | module | reported, not flagged |
| Import cycles (strongly connected components) | module graph | any cycle becomes a finding |

Import cycles become a deterministic finding (extensibility, medium). Everything else goes into the lens context pack as `<structure>` and into the review's "Structure at a glance" section. Python only today.

## 7. Synthesis rules (plain code, no LLM)

1. **Verify every evidence anchor** (file, line, verbatim quote) against the real file; snap a miscounted line to the line holding the quote; drop findings with no verifiable evidence (they are listed in the appendix).
2. **Apply suppressions:** `cra: ignore[lens]` comments, ignored paths, untouched legacy code in PR mode.
3. **Layer conventions** as in section 2.
4. **Merge duplicates across lenses** on a shared key or on nearby lines with similar wording; keep every lens's rationale; independent agreement raises confidence.
5. **Surface tradeoffs:** findings with opposite stances on one axis are never merged or resolved; they are listed for a human.
6. **Rank:** severity first; within a tier, wrong results and data exposure outrank design and hygiene.
7. **Choose inline comments (the precision budget, default 12).** Eligible: confident, not a nit, anchored on a real line. Then: (a) every critical and high finding; (b) top up each dimension to 2 comments, preferring lenses not yet represented; (c) fill the rest tier by tier, rotating across dimensions. Without (b) and (c), correctness findings fill every slot and design feedback never reaches the PR.

The content hash of a review is identical across input orderings.

## 8. Output

`review.md`, `review.json`, `convention_profile.json`, `pr_review_payload.json`.

The review: (1) **Summary**: verdict, restated objective, counts, top findings, scorecard by dimension and lens, structure at a glance, tradeoffs; (2) **Detailed comments** grouped Product, Engineering quality, Operations, then the conditional packs, each finding with evidence, why it matters and a fix; (3) **Conventions applied** and open questions; (4) **Appendix**: nits, dropped findings, run details including which model produced the lens findings.

GitHub: `--pr owner/repo#N` re-maps comments onto the PR's real diff lines (GitHub rejects a whole review if one comment is off-diff). Findings outside the diff go in the review body. Nothing is posted without `--post`. For a repo with no PR, `cra baseline` creates two local branches (`review-baseline` = one empty commit; `review-full` = that commit plus a snapshot of the source) so a PR between them shows every file as added.

## 9. Trust boundaries

- Repo text is data: it arrives in `<repo_file>` blocks and every lens is told to ignore instructions found there.
- Secrets never reach a model, log or document: sensitive files are excluded from the context and refused by the read tool; secret-shaped strings are redacted; a committed secret is reported by file, line and variable name only. Credentials embedded in a git remote URL are stripped.
- Lens tools are read-only, confined to tracked files in the repo root, size-capped and budgeted.
- Nothing is posted by default.
- Model-provided anchors are never trusted; every quote is re-verified.

## 10. Validation (what is and is not established)

**Established**
- 100 tests, no network or API key needed: scout, structure metrics, conventions, synthesis (including the inline budget), lens runner (scripted client), graph, GitHub (fake `gh`; real git for baseline branches), CLI.
- A mutation check on 7 critical behaviours (secret redaction, convention capping, anchor verification, PR diff mapping, RAG gating, path confinement, tradeoff merging): 6 caught; the 7th is an equivalent mutant because two independent guards cover path confinement.
- Two real runs on `niranjani23/multi-agent-battery-perf-analysis` @ `79dea22`, both posted to GitHub:
  - **v1** (lens prompts v1, 8 lenses): 34 findings, 12 inline comments, posted on PR #1.
  - **v2** (lens prompts v2, 12 lenses incl. the 4 new engineering lenses): 9 deterministic + 38 recorded lens findings became 41 after verification and merging (1 critical, 4 high, 23 medium, 10 low, 3 nit; 0 dropped; 6 merged across lenses). Posted with `--max-inline 16` so all four engineering lenses appear inline.
- Structure metrics on that repo agree with a manual read: `find_gap_drivers` complexity 26, `generate_pdf` 275 lines, the orchestrator the most coupled module (imports 7), no import cycles.
- Most accuracy and agent-design findings were reproduced by executing the repo's own functions (`examples/battery-perf-analysis/reproductions`).
- GitHub posting works end to end: draft PRs opened, reviews posted, every inline comment accepted on a real diff line and read back. GitHub echoes diff lines in each comment's `diff_hunk`, so a committed secret on a commented line is visible there even though the tool never writes it.

**Not established**
- The lens agents have **not** been run against the live Anthropic API (no key in the build environment; the key committed in the reviewed repo was deliberately not used). `AnthropicLensRunner` is tested only against a scripted client. The lens findings in both runs were authored by Claude from reading the code and loaded through the replay runner. They exercise everything downstream of the lenses but say nothing about live precision or recall.
- The engineering-lens prompts in particular are new and unevaluated; design feedback is the area most prone to generic advice, which is why their prompts demand a demonstrated cost.
- The RAG lens prompts have not been run on a real RAG repo. Convention inference and structure metrics cover Python only. Merge thresholds and metric thresholds are untuned.

## 11. Roadmap and open questions

1. Run the lenses live; build a golden set of past PRs with the comments humans left; score precision and recall per lens and per team, and run one PR N times to measure drift.
2. Execution-based verification: let a lens attach a runnable repro that synthesis executes in a sandbox (done by hand for the first runs).
3. Structure metrics and convention inference for TypeScript, Go and Java; duplication detection; churn x complexity hot spots from git history.
4. A feedback loop: record dismissed and resolved comments; drop rules and lower lenses that are dismissed repeatedly.
5. An org-level profile; a GitHub Action / App wrapper; a LangGraph `interrupt()`-based gate for server deployments.

Open questions: should the default be diff review or repo audit; who reads the doc (author, tech lead, client), since that decides tone and which dimension leads; is 12 the right default inline budget, and should the per-dimension reserve be configurable per team?
