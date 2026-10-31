# Design and feature spec

## 1. Purpose and scope

Given a GitHub repo or a pull request, the system reviews it with specialist sub-agents and returns one document: a high-level verdict first, then specific comments on objective, approach, scalability and accuracy. When the code contains a RAG system it also reviews retrieval, generation, governance and evals.

- **Two modes.** Repo audit reviews the whole tree. PR review (`--base REF`) puts inline comments on changed lines and uses the rest of the repo as context.
- **Asks only what it cannot read.** Conventions are harvested from config, inferred from code and mined from past review comments. Humans are asked only about the ambiguous remainder.
- **Three outputs.** A Markdown review, a JSON, and a GitHub review payload.
- **Non-goals.** It does not replace linters or CI, does not auto-fix code, and never posts unless explicitly told to.

## 2. Rule layers and provenance

A security hole and a disliked naming style must never carry the same weight, or engineers learn to ignore the agent.

| Layer | Covers | Can it block? |
|---|---|---|
| Universal | Correctness, security, data loss, concurrency | Yes: critical to low |
| Org | Compliance, logging and PII policy shared across teams | In the model; no org profile mechanism yet |
| Team | House conventions: error style, naming, test style, structure | No. Capped at low when confirmed, nit when unconfirmed |

Each convention carries a provenance: **declared**, **inferred** (with an adherence rate), **mined** (from review history) or **confirmed** (a human said yes). A team finding must cite a rule id that exists in the profile. A finding citing an unknown, rejected or linter-enforced rule is dropped.

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

Lens agents get two read-only tools, `read_file` and `grep` (budget 8 calls), and must finish with `report_findings`, which is schema-validated and retried once. A lens that fails or never reports is marked `error` or `not_run`; the rest still complete.

## 5. Lens catalog

| Pack | Lens | Checks | Runs when |
|---|---|---|---|
| General | Objective & fit | restates the objective; docs vs code; inputs accepted but ignored | always |
| General | Approach & architecture | proportionate design; hidden coupling; deterministic work run through a model | always |
| General | Scalability & cost | what breaks first at 10x/100x; concurrency; call counts | always |
| General | Accuracy & correctness | wrong results with a failing input; validation order; label vs computation | always |
| General | Security, privacy & governance | secrets; what leaves the machine; trust boundaries; doc claims | always |
| General | Reliability & observability | tests; failure handling; debuggability | always |
| Agentic | Agent & tool-loop design | stop reasons; tool contracts; model-typed data vs cached truth | LLM calls found |
| Agentic | LLM grounding & evaluation | claims checked against data; evals; fallbacks | LLM calls found |
| RAG | Corpus governance & ingestion | provenance and ACLs at ingest and at query; re-index path | embedding + store/retriever found |
| RAG | Retrieval quality | chunking; embedding prefixes; hybrid search; rank fusion; post-filtering | same |
| RAG | Generation integrity | context order and overflow; corpus as untrusted input; refusal path | same |
| RAG | Evaluation architecture | retrieval and generation scored separately; golden-set provenance; judge | same |
| RAG | Scale, cost & observability | ANN params; re-embedding; cache keys; logged chunk ids | same |

A RAG lens never runs without evidence of a RAG pipeline, and the review prints that evidence.

## 6. Synthesis rules (plain code, no LLM)

1. **Verify every evidence anchor** (file, line, verbatim quote) against the real file; snap a miscounted line to the line holding the quote; drop findings with no verifiable evidence (they are listed in the appendix).
2. **Apply suppressions:** `cra: ignore[lens]` comments, ignored paths, untouched legacy code in PR mode.
3. **Layer conventions** as in section 2.
4. **Merge duplicates across lenses** on a shared key or on nearby lines with similar wording; keep every lens's rationale; independent agreement raises confidence.
5. **Surface tradeoffs:** findings with opposite stances on one axis are never merged or resolved; they are listed for a human.
6. **Rank and budget:** within a severity tier, wrong results and data exposure outrank hygiene. Only confident, non-nit findings that point at a real line become inline comments (default cap 12).

The content hash of a review is identical across input orderings.

## 7. Output

`review.md`, `review.json`, `convention_profile.json`, `pr_review_payload.json`.

The review: (1) Summary: verdict, restated objective, counts, top findings, lens scorecard, tradeoffs; (2) Detailed comments per lens, each with evidence, why it matters and a fix; (3) Conventions applied and open questions; (4) Appendix: nits, dropped findings, run details including which model produced the lens findings.

GitHub: `--pr owner/repo#N` re-maps comments onto the PR's real diff lines (GitHub rejects a whole review if one comment is off-diff). Findings outside the diff go in the review body. Nothing is posted without `--post`. For a repo with no PR, `cra baseline` creates two local branches (`review-baseline` = one empty commit; `review-full` = that commit plus a snapshot of the source) so a PR between them shows every file as added.

## 8. Trust boundaries

- Repo text is data: it arrives in `<repo_file>` blocks and every lens is told to ignore instructions found there.
- Secrets never reach a model, log or document: sensitive files are excluded from the context and refused by the read tool; secret-shaped strings are redacted; a committed secret is reported by file, line and variable name only. Credentials embedded in a git remote URL are stripped.
- Lens tools are read-only, confined to tracked files in the repo root, size-capped and budgeted.
- Nothing is posted by default.
- Model-provided anchors are never trusted; every quote is re-verified.

## 9. Validation (what is and is not established)

**Established**
- 91 tests, no network or API key needed: scout, conventions, synthesis, lens runner (scripted client), graph, GitHub (fake `gh`; real git for baseline branches), CLI.
- A mutation check on 7 critical behaviours (secret redaction, convention capping, anchor verification, PR diff mapping, RAG gating, path confinement, tradeoff merging): 6 caught; the 7th is an equivalent mutant because two independent guards cover path confinement.
- A first real run on `niranjani23/multi-agent-battery-perf-analysis` @ `79dea22` (2,354 lines of Python): 9 deterministic findings, 28 recorded lens findings across 8 lenses, 34 findings after verification and merging (1 critical, 4 high, 16 medium, 10 low, 3 nit), 0 dropped. The RAG pack was correctly skipped.
- Most findings in that run were reproduced by executing the repo's own functions on synthetic data, and the PDF defect was confirmed by rendering the page and looking at it. See `examples/battery-perf-analysis/`.

**Not established**
- The lens agents have **not** been run against the live Anthropic API (no key was available in the build environment, and the key committed in the reviewed repo was deliberately not used). `AnthropicLensRunner` is tested only against a scripted client. The lens findings in the example run were authored by Claude from reading the code and loaded through the replay runner. They exercise synthesis, rendering and the PR flow, and show what the review looks like, but they say nothing about the precision or recall of live lens agents.
- No lens eval exists yet.
- The RAG lens prompts have not been run on a real RAG repo.
- Convention inference covers Python only. Merge thresholds are untuned.

## 10. Roadmap and open questions

1. Run the lenses live; build a golden set of past PRs with the comments humans left; score precision and recall per team, and run one PR N times to measure drift in the finding set.
2. Execution-based verification: let a lens attach a runnable repro that synthesis executes in a sandbox (done by hand in the first run).
3. A feedback loop: record dismissed and resolved comments; drop rules that are dismissed repeatedly.
4. An org-level profile; convention inference for TypeScript, Go and Java.
5. A GitHub Action / App wrapper, and a LangGraph `interrupt()`-based gate for server deployments.

Open questions: should the default be diff review or repo audit; who reads the doc (author, tech lead, client), since that decides tone and which lenses lead; and is 12 the right default inline budget?
