# Code review: multi-agent-battery-perf-analysis

`79dea22` · mode: audit · 2026-11-02 · content hash `65aad922c992baff`

## 1. Summary

**Verdict.** Action required: 1 critical and 4 high-severity issue(s) found.

**What this is trying to do.** Take one week of a battery's dispatch data (historical operation versus a perfect-foresight benchmark), quantify the revenue gap, attribute it to a primary and a secondary driver, and return two evidence-grounded recommendations as a UI view and a PDF report. Three LLM sub-agents do this, with pandas tools doing the numeric work.

**Findings.** 1 critical · 4 high · 23 medium · 10 low · 3 nit (41 total after verification and de-duplication).

### Top findings

| ID | Severity | Lens | Finding | Where |
|---|---|---|---|---|
| F-001 | Critical | Security, privacy & governance | `.env` is committed and holds what looks like a live Anthropic API key | .env:1 |
| F-002 | High | Accuracy & correctness | Recommendation text runs off the page in the PDF | battery_agent/report/pdf_generator.py:371 |
| F-003 | High | Agent & tool-loop design | Tools prefer JSON typed by the model over the authoritative cached results | battery_agent/tools/analysis_tools.py:290 |
| F-004 | High | LLM grounding & evaluation | The evidence validator accepts any text containing a common word, while every PDF promises grounding | battery_agent/tools/rec_tools.py:51 |
| F-005 | High | Scalability & cost | Per-run state lives in module-level dicts shared by every caller | battery_agent/tools/analysis_tools.py:15 |
| F-006 | Medium | Security, privacy & governance | Raw CSV rows are sent to the model, contradicting the documented privacy design | README.md:77 |
| F-007 | Medium | Accuracy & correctness | The battery id in the report comes from the upload's temp-file name | battery_agent/orchestrator.py:140 |
| F-008 | Medium | Accuracy & correctness | `ANTHROPIC_MODEL` is read from the environment at import time, before `load_dotenv()` runs | battery_agent/agents/base.py:14 |

### Scorecard by lens

| Dimension | Lens | Status | Findings | Critical | High | Medium | Low / nit |
|---|---|---|---|---|---|---|---|
| Product | Objective & fit | ran | 2 | 0 | 0 | 1 | 1 |
| Product | Approach & architecture | ran | 1 | 0 | 0 | 0 | 1 |
| Engineering | Design patterns & abstractions | ran | 1 | 0 | 0 | 1 | 0 |
| Engineering | Extensibility & modularity | ran | 2 | 0 | 0 | 2 | 0 |
| Engineering | Robustness & error handling | ran | 2 | 0 | 0 | 2 | 0 |
| Engineering | Maintainability & readability | ran | 3 | 0 | 0 | 2 | 1 |
| Engineering | Testing & observability | ran | 6 | 0 | 0 | 2 | 4 |
| Operations | Scalability & cost | ran | 2 | 0 | 1 | 1 | 0 |
| Operations | Accuracy & correctness | ran | 12 | 0 | 1 | 7 | 4 |
| Operations | Security, privacy & governance | ran | 4 | 1 | 0 | 1 | 2 |
| LLM / agent | Agent & tool-loop design | ran | 4 | 0 | 1 | 3 | 0 |
| LLM / agent | LLM grounding & evaluation | ran | 2 | 0 | 1 | 1 | 0 |
| RAG | RAG: corpus governance & ingestion | skipped | - | - | - | - | - |
| RAG | RAG: retrieval quality | skipped | - | - | - | - | - |
| RAG | RAG: generation integrity | skipped | - | - | - | - | - |
| RAG | RAG: evaluation architecture | skipped | - | - | - | - | - |
| RAG | RAG: scalability, cost & observability | skipped | - | - | - | - | - |

### Structure at a glance

Measured, not judged: 46 functions in 14 Python modules; 11 exceed a threshold (lines >= 60, complexity >= 10, parameters >= 5, nesting >= 4); import cycles: 0. The design lenses use these as leads.

| Function | Where | Lines | Complexity | Params | Nesting |
|---|---|---|---|---|---|
| `find_gap_drivers` | battery_agent/tools/analysis_tools.py:280 | 97 | 26 | 4 | 2 |
| `generate_pdf` | battery_agent/report/pdf_generator.py:125 | 275 | 17 | 1 | 2 |
| `run_analysis` | battery_agent/orchestrator.py:36 | 154 | 17 | 3 | 2 |
| `main` | battery_agent/main.py:15 | 152 | 12 | 0 | 2 |
| `analyze_soc` | battery_agent/tools/analysis_tools.py:222 | 56 | 11 | 0 | 2 |
| `generate_recommendations` | battery_agent/tools/rec_tools.py:15 | 66 | 10 | 4 | 2 |

Most coupled modules (imported by / imports): `battery_agent/orchestrator.py` 2 / 7; `battery_agent/tools/analysis_tools.py` 4 / 1; `battery_agent/agents/recommendations.py` 1 / 3; `battery_agent/agents/analysis.py` 1 / 2.

## 2. Detailed comments

### Product: is this the right thing, built the right way?

#### Objective & fit

The purpose is clear and the README states it well. Two documented behaviours do not work as described: the custom analysis question never reaches any agent, and the 'model never sees CSV rows' claim is false (see Governance). One analytic scope question is open: half of the loaded scenario/schedule combinations are never analysed.

##### F-020 · Medium · The custom analysis question is accepted but never used

_Objective & fit · confidence 0.95 · LLM lens · universal layer_

- `battery_agent/orchestrator.py:38`: `user_query: str = "Analyze the battery performance data and provide recommendations.",`
- `battery_agent/orchestrator.py:82`: `_log(f"[orchestrator] query: {user_query}")`
- `battery_agent/orchestrator.py:161`: `"user_query": user_query,`
- `battery_agent/agents/analysis.py:49`: `def run(`
- `README.md:36`: `Optionally customise the analysis question.`

**What is wrong.** `user_query` is only logged and copied into the report metadata. `data_prep.run`, `analysis.run` and `recommendations.run` take no query parameter, so the UI text box (README line 36) and the `--query` flag (README line 50) change nothing. Example: a user asks why Tuesday evening lost money and receives the same generic report.

**Why it matters.** A prominent control that silently does nothing makes the user believe the analysis was steered by their question.

**Suggested fix.** Either remove the field and the flag, or pass `user_query` into the Analysis and Recommendation initial messages and have the report state how the question was addressed.

#### Approach & architecture

Clean layering (agents, tools, report) and a sound rule that the model works from JSON summaries. The main design question is whether three LLM agent loops earn their cost when the tool order is fixed by the prompts.

_No findings above the confidence threshold._

### Engineering quality: will this codebase survive change?

#### Design patterns & abstractions

The good parts are worth keeping: one shared tool_use loop (`run_agent_loop`) instead of three copies, thin agent modules that only hold a prompt and a call, and tools that return JSON rather than prose. No heavier pattern is needed for three agents. The weaknesses are at the seams: each tool is defined three times by hand, and the parts talk through module-level globals instead of explicit inputs.

##### F-025 · Medium · Each tool is written three times by hand, and the copies have already drifted

_Design patterns & abstractions · confidence 0.85 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:383`: `TOOL_DEFINITIONS = [`
- `battery_agent/tools/analysis_tools.py:466`: `"required": ["revenue_summary", "high_price_analysis", "dispatch_comparison", "soc_analysis"],`
- `battery_agent/tools/analysis_tools.py:281`: `revenue_summary: dict | None = None,`
- `battery_agent/tools/data_tools.py:280`: `elif name == "validate_schema":`

**What is wrong.** Every tool exists as a Python function, a hand-written JSON schema in `TOOL_DEFINITIONS`, and a branch in an `if/elif` `execute_tool` dispatcher, in each of three modules. Nothing keeps them in sync, and they already disagree: the schema marks all four `find_gap_drivers` inputs as required while the function treats them as optional and silently prefers them over the cached results (the root of finding 'Tools prefer JSON typed by the model'). The dispatchers also index `inputs[...]` without validation, which is why a malformed tool call raises `KeyError`.

**Why it matters.** Every new or changed tool needs three coordinated edits, and a mismatch is not caught by anything until the model trips over it at run time.

**Suggested fix.** Use one small registry: a `@tool` decorator that records the function and derives its input schema from the type hints (or a pydantic model per tool), validates inputs before calling, and dispatches by dictionary lookup. That removes the three `execute_tool` functions and makes schema/implementation drift impossible.

#### Extensibility & modularity

The three most likely next changes are a new analysis step, a second battery or market, and a new output format. Today each touches many files, because the step list, the scenario names and the report shape are spelled out by hand in several places. No import cycles; the orchestrator is the only highly coupled module (imports 7 internal modules), which is expected for an orchestrator.

##### F-023 · Medium · A second battery, market or schema requires editing every analysis function

_Extensibility & modularity (also: Approach & architecture) · confidence 0.80 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:172`: `hist = df[(df["SCENARIO_NAME"] == "historical") & (df["SCHEDULE_TYPE"] == "cleared")].copy()`
- `battery_agent/tools/analysis_tools.py:228`: `hist = df[(df["SCENARIO_NAME"] == "historical") & (df["SCHEDULE_TYPE"] == "cleared")]`
- `battery_agent/tools/analysis_tools.py:119`: `hist_stats = combo_stats("historical", "cleared")`
- `battery_agent/orchestrator.py:140`: `battery_id = "BLYTHB1"`
- `battery_agent_plan.md:226`: `schema validator checks column names dynamically; no hardcoded assumptions in prompts`

**What is wrong.** The baseline and benchmark combinations (`historical/cleared`, `perfect/cleared`) are written literally in five places in `analysis_tools.py`, the required columns are a module constant, and the battery id defaults to `BLYTHB1`. Comparing `expected` schedules, or a market that names scenarios differently, means editing each function.

Also raised by the Approach & architecture lens: The design principle says the validator checks column names dynamically with no hard-coded assumptions. The code hard-codes the required columns, the `historical/cleared` and `perfect/cleared` combinations in every analysis tool, and a default battery id of `BLYTHB1`.

**Why it matters, by lens.**
- Approach & architecture: A second battery, market or column naming needs code changes in several places, and a wrong default id can reach a report unnoticed.
- Extensibility & modularity: The plan names generalisation as a design principle, but the code makes the first new dataset an edit-everywhere change.

**Suggested fix.** Put the schema, the baseline and benchmark combination names, the thresholds and the battery id in one config object passed through the run context. Analysis functions take the baseline/benchmark frames as arguments instead of filtering by name.

##### F-024 · Medium · Adding one analysis step means edits in about seven places across six files

_Extensibility & modularity · confidence 0.80 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:383`: `TOOL_DEFINITIONS = [`
- `battery_agent/tools/analysis_tools.py:472`: `def execute_tool(name: str, inputs: dict) -> dict:`
- `battery_agent/agents/analysis.py:22`: `You MUST call tools in this exact order:`
- `battery_agent/orchestrator.py:133`: `revenue_summary = analysis_tools._results.get("revenue_summary", {})`
- `battery_agent/report/pdf_generator.py:329`: `story.append(Paragraph("Performance Gap — Root Cause Analysis", s["section"]))`

**What is wrong.** For a new metric (for example a degradation-cost analysis) you must write the function, add its schema to `TOOL_DEFINITIONS`, add a branch to `execute_tool`, add it to the tool order in the Analysis prompt and its initial message, widen `find_gap_drivers` if it should influence the drivers, pull its result out of `_results` in the orchestrator, and add display code to the PDF, the Streamlit page and the CLI. Nothing tells you if you missed one.

**Why it matters.** This is the change the project will most often need, and its cost grows with every step added.

**Suggested fix.** Combine the tool registry from the design-patterns finding with an ordered list of analysis steps defined once in code. Have the orchestrator run that list, and have the report render whichever sections are present in the results, so a new step is one function plus one entry.

#### Robustness & error handling

The code does think about failure (the data step is meant to fail fast, and every stage is wrapped), but the strategy is inconsistent and success is decided by whether a string is empty. As a result, a failure early in the pipeline can still produce a report marked 'ok'.

##### F-018 · Medium · Two opposite error strategies: tool bodies swallow every exception, dispatchers let them crash the run

_Robustness & error handling · confidence 0.85 · LLM lens · universal layer_

- `battery_agent/tools/data_tools.py:71`: `except Exception as exc:`
- `battery_agent/tools/data_tools.py:72`: `return {"status": "error", "message": str(exc)}`
- `battery_agent/tools/analysis_tools.py:92`: `return {"status": "error", "message": str(exc)}`
- `battery_agent/tools/data_tools.py:279`: `return load_csv(inputs["file_path"])`

**What is wrong.** Nine tool functions wrap their whole body in `except Exception` and return `{'status': 'error', 'message': str(exc)}`, so a programming bug (a typo'd column, a `None` format) reaches the model as if it were a data problem, with no traceback. Meanwhile argument handling in the dispatchers and the recommendation validator are not wrapped, so a missing key raises straight through the agent loop and fails the entire run (reproduced as `KeyError` and `AttributeError`).

**Why it matters.** Real bugs are hidden where they should be loud, and recoverable input mistakes are fatal where they should be reported back to the model.

**Suggested fix.** Separate expected from unexpected errors. Validate tool inputs up front and return a structured `is_error` result for bad input or bad data; let genuine exceptions propagate to one handler in the loop that logs the traceback and returns a tool error. Remove the blanket `except Exception` from the tool bodies.

##### F-019 · Medium · A failed stage can still produce a report marked status 'ok'

_Robustness & error handling · confidence 0.70 · LLM lens · universal layer_

- `battery_agent/orchestrator.py:93`: `if not data_manifest:`
- `battery_agent/orchestrator.py:109`: `if not analysis_summary:`
- `battery_agent/orchestrator.py:125`: `_log(f"[orchestrator] recommendations received: status={recs_result.get('status', '?')}")`
- `battery_agent/orchestrator.py:155`: `"status": "ok",`
- `battery_agent/agents/recommendations.py:86`: `return rec_tools._results.get("recommendations") or {"status": "text_only", "text": text_result}`

**What is wrong.** The orchestrator decides that an agent succeeded if its text is non-empty. If validation fails, the Data Prep agent is told to stop and 'report the error clearly', which is non-empty text, so the Analysis agent runs anyway; its tools then all return 'No clean data available', and it writes another non-empty summary. If no valid recommendations were stored, the recommendation agent returns `status: text_only`, which the orchestrator only logs. The final report is built with `status: ok` regardless. Exactly how far this cascades depends on the model's wording, so it was reasoned from the code, not reproduced.

**Why it matters.** The user can receive a confident-looking report for a run whose input was rejected, which defeats the fail-fast design the README describes.

**Suggested fix.** Decide success from structured results, not text: check the stored `validate_schema` and `clean_data` results before starting analysis, require the four analysis results before recommendations, and set the report status from those checks (with `text_only` treated as a failure or a clearly marked partial result).

#### Maintainability & readability

Most functions are short and clearly named, and the modules map cleanly onto the README's architecture. Maintenance cost concentrates in two oversized functions and in result data passed around as untyped nested dicts whose key strings are repeated across producers and renderers.

##### F-022 · Medium · Results travel as untyped nested dicts whose keys are retyped in four places

_Maintainability & readability (also: Extensibility & modularity) · confidence 0.90 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:83`: `"historical_cleared_total_revenue": _round(hist_rev),`
- `battery_agent/report/pdf_generator.py:171`: `hist_rev = rev_summary.get("historical_cleared_total_revenue")`
- `battery_agent/ui/app.py:173`: `hist_rev = rev.get("historical_cleared_total_revenue")`
- `battery_agent/tools/analysis_tools.py:357`: `"total_gap_pct": _round(gap_pct),`
- `battery_agent/orchestrator.py:178`: `pdf_bytes = pdf_generator.generate_pdf(report)`

**What is wrong.** Keys such as `historical_cleared_total_revenue` and `gap_pct` are written in the tool that produces them and again in the PDF generator, the Streamlit page and the CLI. The same number even has two names (`gap_pct` in the revenue summary, `total_gap_pct` in the gap drivers). A renamed or misspelled key does not fail; it silently becomes `None` or 'N/A' in one output.

Also raised by the Extensibility & modularity lens: The orchestrator calls the PDF generator directly, and the PDF, the Streamlit page and the CLI each read the raw report dict and format it themselves. A JSON API, an HTML page or a Slack summary would each need yet another copy of the key lookups and the None handling.

**Why it matters, by lens.**
- Extensibility & modularity: Output formats are a likely extension for a reporting tool, and today each one multiplies the formatting bugs (the CLI already crashes where the UI does not).
- Maintainability & readability: Every change to an analysis result is a search-and-replace across modules with no type checker able to help, and inconsistencies show up only in front of users.

**Suggested fix.** Define the results as dataclasses or pydantic models (RevenueSummary, DispatchComparison, GapDrivers, Report) returned by the tools and consumed by the renderers; serialize to JSON only at the LLM boundary.

##### F-026 · Medium · The two hardest-to-change functions mix several responsibilities

_Maintainability & readability · confidence 0.80 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:280`: `def find_gap_drivers(`
- `battery_agent/tools/analysis_tools.py:329`: `primary_factor = "suboptimal_dispatch_timing"`
- `battery_agent/report/pdf_generator.py:125`: `def generate_pdf(report: dict) -> bytes:`

**What is wrong.** `find_gap_drivers` (97 lines, cyclomatic complexity 26, the highest in the repo) extracts eleven values from four nested dicts, applies the driver decision rule, and writes the English evidence sentences, all in one function. `generate_pdf` is 275 lines that both reformat data and lay out every section. The decision rule, which is where the sign bug lives, cannot be tested without also generating prose.

**Why it matters.** These are the functions most likely to need changes (new drivers, new report sections), and their size is why bugs like the `abs()` sign error are easy to miss.

**Suggested fix.** Split `find_gap_drivers` into a pure scoring function that returns numbers and a separate narration step; give `generate_pdf` one small function per section. Add tests for the scoring function first.

##### F-036 · Low · The same filters and thresholds are restated instead of named once

_Maintainability & readability · confidence 0.80 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:184`: `eps = 1e-6 # threshold for "non-zero" energy`
- `battery_agent/tools/analysis_tools.py:244`: `perf_discharged = perf[perf["DISCHARGE_ENERGY"] > 1e-6]`
- `battery_agent/tools/analysis_tools.py:103`: `threshold = float(df["PRICE_ENERGY"].quantile(0.75))`

**What is wrong.** The baseline filter expression is written out five times, the 'non-zero energy' epsilon is a named local in one function and a bare `1e-6` in another, and the high-price percentile is computed independently in two tools. These copies will drift (the two thresholds already can).

**Why it matters.** A change to any of these definitions must be found and repeated by hand.

**Suggested fix.** Add helpers such as `baseline(df)` / `benchmark(df)` and module-level named constants (`ENERGY_EPS`, `HIGH_PRICE_QUANTILE`, `LOW_SOC_PCT`), or put them in the run config.

#### Testing & observability

The deterministic checks report the absence of tests and CI. Behavioural problems found beyond those: results vanish in the UI, and agent traces are not kept.

##### F-027 · Medium · Generated artifacts are committed and there is no .gitignore

_Testing & observability · confidence 0.90 · deterministic check · universal layer_

- `battery_agent/__pycache__/orchestrator.cpython-314.pyc`
- `battery_agent/agents/__pycache__/__init__.cpython-314.pyc`
- `battery_agent/agents/__pycache__/analysis.cpython-314.pyc`
- Searched repository-wide: no .gitignore at the repo root

**What is wrong.** 12 tracked file(s) are generated artifacts (compiled Python bytecode). There is no `.gitignore`, so nothing stops further artifacts, local config or `.env` files from being committed.

**Why it matters.** Bytecode and caches add noisy diffs and stale files, and a missing .gitignore is the usual root cause of committed secrets.

**Suggested fix.** Add a language-appropriate `.gitignore` (Python: `__pycache__/`, `*.pyc`, `.env`, `.venv/`), then `git rm -r --cached` the artifacts.

##### F-028 · Medium · Results disappear after any widget interaction in the Streamlit app

_Testing & observability · confidence 0.85 · LLM lens · universal layer_

- `battery_agent/ui/app.py:95`: `if uploaded_file and not run_btn:`
- `battery_agent/ui/app.py:108`: `st.stop()`
- `battery_agent/ui/app.py:113`: `if run_btn and uploaded_file:`
- `battery_agent/ui/app.py:293`: `st.download_button(`

**What is wrong.** Streamlit re-runs the whole script on every widget interaction, and a button is true only in the run where it was clicked. The report is rendered only inside `if run_btn and uploaded_file` and is never stored in `st.session_state` (it is not used anywhere in the app). Clicking Download, or editing the question box, re-runs the script with `run_btn` false, reaches `st.stop()` at line 108, and the results vanish; the user must pay for a new 30-90 second LLM run. This follows from Streamlit's execution model and was not exercised in a browser here.

**Why it matters.** The most natural next action after seeing the report destroys it.

**Suggested fix.** Store the report in `st.session_state` when the run completes and render from there; show the Run button only to start or re-run.

##### F-037 · Low · No CI configuration

_Testing & observability · confidence 0.90 · deterministic check · universal layer_

- Searched repository-wide: no .github/workflows, .gitlab-ci.yml, .circleci, Jenkinsfile or azure-pipelines.yml

**What is wrong.** No CI workflow (GitHub Actions, GitLab CI, CircleCI, Jenkins, Azure Pipelines) is present.

**Why it matters.** Nothing runs tests, linting or secret scanning on pushes and pull requests.

**Suggested fix.** Add a minimal workflow that installs dependencies, runs the tests, and runs a secret scanner on every PR.

##### F-038 · Low · Agent traces are not persisted, so a bad recommendation cannot be debugged afterwards

_Testing & observability · confidence 0.70 · LLM lens · universal layer_

- `battery_agent/agents/base.py:77`: `calling {block.name}({json.dumps(block.input)[:120]}...)`
- `battery_agent/ui/app.py:129`: `visible = log_lines[-30:]`

**What is wrong.** Progress goes to a callback that the UI keeps as the last 30 lines, tool inputs are truncated to 120 characters in the log, and neither model responses nor tool results are stored. There is no run id tying a PDF to the calls that produced it.

**Why it matters.** When a recommendation is wrong, there is nothing to replay or diff against.

**Suggested fix.** Write a per-run JSONL (run id, messages, tool inputs and outputs, token usage) next to the report.

### Operations: does it hold up in use?

#### Scalability & cost

At one week of 5-minute data (about 1,150 rows) compute and memory are not a concern. What limits scale is concurrent-run isolation (reported by the deterministic check) and the long chain of sequential LLM calls.

##### F-005 · High · Per-run state lives in module-level dicts shared by every caller

_Scalability & cost (also: Design patterns & abstractions) · confidence 0.90 · deterministic check · universal layer_

- `battery_agent/tools/analysis_tools.py:15`: `_results: dict = {}`
- `battery_agent/tools/data_tools.py:24`: `_state: dict = {`
- `battery_agent/tools/rec_tools.py:10`: `_results: dict = {}`
- `battery_agent/tools/analysis_tools.py:19`: `return data_tools._state.get("clean_df")`
- `battery_agent/orchestrator.py:133`: `revenue_summary = analysis_tools._results.get("revenue_summary", {})`

**What is wrong.** 3 module-level mutable container(s) are written during a run: `_results` (battery_agent/tools/analysis_tools.py:15); `_state` (battery_agent/tools/data_tools.py:24); `_results` (battery_agent/tools/rec_tools.py:10). The repo is served by streamlit, which runs concurrent sessions in one process, so two simultaneous runs share these dicts: one run's reset can wipe another's data, and results can cross between users.

Also raised by the Design patterns & abstractions lens: The analysis tools reach into `data_tools._state` for the DataFrame, the orchestrator and the recommendation tool read private `_results` dicts of another module, and each agent clears another module's cache before running. These are side channels: no function signature shows what a step needs or produces, and the call order is the only thing making them correct (temporal coupling).

**Why it matters, by lens.**
- Design patterns & abstractions: Beyond the concurrency risk, this is what makes the tools hard to test in isolation and hard to reuse: you cannot call `compare_dispatch(df)` on a DataFrame; you must first run the loading tools to populate a global.
- Scalability & cost: Hidden global state makes runs non-reentrant. It also couples agents and tools through side channels instead of explicit inputs, which makes them harder to test and reason about.

**Suggested fix.** Create a run-scoped context object (a dataclass holding the DataFrame and cached results) and pass it through orchestrator -> agents -> tools, so each run owns its state.

##### F-021 · Medium · About a dozen strictly sequential LLM round trips per run, none parallelisable

_Scalability & cost (also: Approach & architecture) · confidence 0.75 · LLM lens · universal layer_

- `battery_agent/ui/app.py:137`: `this may take 30–90 seconds`
- `battery_agent/agents/base.py:56`: `calling model...`
- `battery_agent/agents/data_prep.py:25`: `You MUST call tools in this exact order:`
- `battery_agent/agents/analysis.py:22`: `You MUST call tools in this exact order:`
- `battery_agent_plan.md:26`: `exactly what the rubric rewards`

**What is wrong.** Each agent loop re-sends its whole history every turn. By the prompts' own tool lists, Data Prep needs at least five calls (four tools plus the final answer), Analysis at least six, and Recommendations at least two. They cannot run in parallel because they share module-level state, so batching several batteries or days means multiplying a 30-90 second run (the UI's own estimate).

Also raised by the Approach & architecture lens: The Data Prep and Analysis agents are told to call a fixed sequence of argument-free tools in a fixed order, so the model makes no decisions there. Each step costs a round trip with a growing message history, and the order is enforced only by prompt text, although the README calls it 'enforced' (line 84). The only step that needs a model is turning the evidence into two recommendations. The plan says the assignment rewards an agentic design, so this may be deliberate; the tradeoff should still be explicit.

**Why it matters, by lens.**
- Approach & architecture: Extra cost, latency and nondeterminism with no accuracy benefit, and one malformed turn can derail an otherwise deterministic computation.
- Scalability & cost: Cost and latency grow linearly with the number of batteries analysed; 100 batteries is on the order of 1,300 sequential model calls.

**Suggested fix.** Make the deterministic stages plain code (see the approach finding), keep one model call for the narrative, and pass a run-scoped context so runs can execute concurrently.

#### Accuracy & correctness

Most findings below were reproduced by running the repository's own functions on synthetic data (no LLM involved). The numeric tools are readable, but several edge cases produce wrong or misleading numbers without any warning, and the PDF truncates the main deliverable.

##### F-002 · High · Recommendation text runs off the page in the PDF

_Accuracy & correctness · confidence 0.95 · LLM lens · universal layer_

- `battery_agent/report/pdf_generator.py:371`: `["Action", rec.get("action", "")],`
- `battery_agent/report/pdf_generator.py:377`: `rec_tbl = Table(rec_tbl_data, colWidths=[1.4 * inch, 5.2 * inch])`

**What is wrong.** The recommendation table passes plain strings as cell values; ReportLab only wraps `Paragraph` flowables. Reproduced with realistic 300-character recommendation text: each field renders as one line cut off at the page edge (text extends to x=613 on a 612 pt page, and the visible text stops mid-sentence). The two recommendations are the product's main deliverable.

**Why it matters.** A reader of the PDF cannot see the reasoning, expected benefit or tradeoff for either recommendation.

**Suggested fix.** Wrap each cell in `Paragraph(escape(text), style)` using a style that wraps, and add a test that renders a long recommendation and checks that no text block extends past the right margin.

##### F-007 · Medium · The battery id in the report comes from the upload's temp-file name

_Accuracy & correctness · confidence 0.90 · LLM lens · universal layer_

- `battery_agent/orchestrator.py:140`: `battery_id = "BLYTHB1"`
- `battery_agent/orchestrator.py:150`: `if len(part) >= 5 and any(c.isdigit() for c in part):`
- `battery_agent/ui/app.py:118`: `with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:`

**What is wrong.** The Streamlit app saves uploads to a randomly named temp file, and the orchestrator derives the battery id from the file stem. Reproduced: `tmp5r7cim8i.csv` becomes `TMP5R7CIM8I` in the PDF title and subtitle. Any file without a part of five or more characters containing a digit is labelled `BLYTHB1`, whichever battery it is.

**Why it matters.** Every report produced through the UI carries a wrong or meaningless identifier at the top.

**Suggested fix.** Read the id from a column in the data, or take it as an explicit input, and fail loudly when it cannot be determined.

##### F-008 · Medium · `ANTHROPIC_MODEL` is read from the environment at import time, before `load_dotenv()` runs

_Accuracy & correctness · confidence 0.85 · deterministic check · universal layer_

- `battery_agent/agents/base.py:14`: `MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")`
- `battery_agent/orchestrator.py:19`: `from .agents import data_prep, analysis, recommendations`
- `battery_agent/orchestrator.py:23`: `load_dotenv()`

**What is wrong.** `battery_agent/orchestrator.py` calls `load_dotenv()` at line 23, but at line 19 it already imported `battery_agent/agents/base.py`, which reads `ANTHROPIC_MODEL` at module level (line 14). The value is frozen at import, so a `ANTHROPIC_MODEL` defined only in a `.env` file is ignored; only a variable exported in the shell takes effect.

**Why it matters.** Documentation says the setting can be overridden via the environment, but the override silently does nothing when it lives in `.env`, which is how most local setups configure it.

**Suggested fix.** Read `ANTHROPIC_MODEL` lazily inside a function (e.g. `def _model(): return os.environ.get(...)`), or call `load_dotenv()` in the entry points before any package import.

##### F-009 · Medium · The primary-driver rule counts negative 'missed revenue' as missed discharge

_Accuracy & correctness · confidence 0.85 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:317`: `hp_fraction = abs(missed_hp_rev / gap) if gap else 0`
- `battery_agent/tools/analysis_tools.py:319`: `if hp_fraction > 0.5:`
- `battery_agent/tools/analysis_tools.py:325`: `f"leaving ${missed_hp_rev:.2f} unrealised."`
- `battery_agent/tools/analysis_tools.py:363`: `"secondary_driver": {`

**What is wrong.** `abs()` discards the sign. If historical earned more than perfect inside the high-price intervals, `missed_hp_rev` is negative, yet a magnitude above half the gap still selects 'missed discharge during high price periods' and the evidence string says 'leaving $-80.00 unrealised'. Reproduced by calling `find_gap_drivers` with missed revenue -80 and a gap of 100. The 0.5 cut-off is unexplained, and neither driver is reconciled with the total gap; the secondary driver has no revenue impact at all.

**Why it matters.** The headline root cause of the report can be the opposite of what the data shows, and the recommendations are built on it.

**Suggested fix.** Use the signed ratio and require positive missed revenue for the high-price branch. Report a revenue impact for both drivers, check that they do not exceed the gap, and show the unattributed remainder.

##### F-010 · Medium · Numbers are coerced after validation, so bad values vanish without a warning

_Accuracy & correctness · confidence 0.85 · LLM lens · universal layer_

- `battery_agent/tools/data_tools.py:85`: `null_counts = {`
- `battery_agent/tools/data_tools.py:138`: `df = df.dropna(subset=["START_DATETIME"])`
- `battery_agent/tools/data_tools.py:147`: `df[col] = pd.to_numeric(df[col], errors="coerce")`

**What is wrong.** `validate_schema` counts nulls on the raw frame, but `clean_data` later runs `pd.to_numeric(errors='coerce')` on five numeric columns and only counts dropped rows for the datetime column. Reproduced: six REVENUE cells containing 'TBD' pass validation (status ok, no nulls), `clean_data` reports `rows_dropped` 0, and six revenues become NaN. Because those cells were charging costs, historical revenue rose from 1,560 to 1,680 and the reported gap fell from 53.6% to 50.0%, with no warning anywhere.

**Why it matters.** A data-entry problem silently changes the headline gap figure.

**Suggested fix.** Coerce first, then count NaN per column and fail (or warn loudly with counts) before analysis. Report rows affected per column in the data manifest.

##### F-011 · Medium · Missing scenario rows become $0 and report a 100% gap instead of an error

_Accuracy & correctness · confidence 0.80 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:75`: `hist_rev = hist.get("total_revenue") or 0.0`
- `battery_agent/tools/data_tools.py:92`: `scenarios = sorted(df["SCENARIO_NAME"].dropna().unique().tolist()) if "SCENARIO_NAME" in df.columns else []`

**What is wrong.** `compute_revenue_summary` treats an absent combination as zero revenue. Reproduced with no `historical/cleared` rows: `validate_schema` returns ok (it lists the scenarios found but never checks the required ones exist), the report states a 100.0% gap of $3,360, while `compare_dispatch` on the same data returns an error. The tools disagree about whether the input is usable.

**Why it matters.** A malformed or partial upload yields a confident but meaningless headline number.

**Suggested fix.** Have `validate_schema` require the two benchmark combinations and return an error naming the missing one.

##### F-012 · Medium · Duplicate timestamps double-count intervals because nothing checks uniqueness

_Accuracy & correctness · confidence 0.80 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:178`: `merged = hist[["START_DATETIME", "CHARGE_ENERGY", "DISCHARGE_ENERGY", "REVENUE"]].merge(`
- `battery_agent/tools/analysis_tools.py:180`: `on="START_DATETIME",`
- `battery_agent/tools/data_tools.py:138`: `df = df.dropna(subset=["START_DATETIME"])`

**What is wrong.** `compare_dispatch` joins historical and perfect on `START_DATETIME` alone, and no step deduplicates or asserts that (scenario, schedule, timestamp) is unique. Reproduced: duplicating three historical rows raised `intervals_compared` from 24 to 27 and inflated every event count derived from the join.

**Why it matters.** Re-exported or concatenated CSVs commonly contain duplicates, and the counts feed the driver attribution.

**Suggested fix.** Assert uniqueness in `clean_data` (error, or dedupe with a warning) and use `merge(..., validate='one_to_one')`.

##### F-013 · Medium · 'Missed discharge revenue' is the benchmark's revenue, not the revenue that was missed

_Accuracy & correctness · confidence 0.75 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:214`: `"missed_discharge_revenue": _round(merged.loc[missed_discharge_mask, "REVENUE_perf"].sum()),`
- `battery_agent/tools/analysis_tools.py:334`: `primary_impact = _round(missed_dis_rev)`

**What is wrong.** The value sums `REVENUE_perf` over intervals where perfect discharged and historical did not. It never subtracts what historical earned or paid in the same intervals. Reproduced: when historical charged during a price spike (revenue -300 per interval) the function reports 1,800 while the true perfect-minus-historical difference is 2,700. The figure is used as the primary driver's impact and quoted as 'missed revenue' in its evidence text.

**Why it matters.** The size of the main driver is misreported whenever historical was active in those intervals.

**Suggested fix.** Sum `REVENUE_perf - REVENUE_hist` over the missed-discharge intervals, and name the field for what it measures.

##### F-031 · Low · The CLI crashes when the benchmark revenue is zero

_Accuracy & correctness · confidence 0.80 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:78`: `gap_pct = _round((gap / perf_rev * 100) if perf_rev else None)`
- `battery_agent/main.py:127`: `rev.get('gap_pct', 'N/A'):.1f`

**What is wrong.** `gap_pct` is None when perfect revenue is 0, and the CLI formats it with `:.1f`. Reproduced: `TypeError: unsupported format string passed to NoneType.__format__`. The Streamlit view guards this case; the CLI does not.

**Why it matters.** The run finishes, then crashes before saving the PDF.

**Suggested fix.** Format optional numbers through one helper that renders 'N/A' for None, shared by the CLI, the UI and the PDF.

##### F-032 · Low · Two different 'high price' thresholds can be mixed in one analysis

_Accuracy & correctness · confidence 0.70 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:95`: `def identify_high_price_intervals(threshold: float | None = None) -> dict:`
- `battery_agent/tools/analysis_tools.py:103`: `threshold = float(df["PRICE_ENERGY"].quantile(0.75))`
- `battery_agent/tools/analysis_tools.py:235`: `threshold = float(df["PRICE_ENERGY"].quantile(0.75))`

**What is wrong.** `identify_high_price_intervals` accepts a threshold from the model, while `analyze_soc` always uses the 75th percentile. Reproduced: with threshold 100 the first reports 100 and the second 300 in the same run, and `find_gap_drivers` combines both.

**Why it matters.** The SOC statistics and the high-price statistics then describe different sets of intervals.

**Suggested fix.** Compute the threshold once, store it in the run context, and have every tool read it from there.

##### F-033 · Low · SOC thresholds assume percent units and nothing validates the range

_Accuracy & correctness · confidence 0.55 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:240`: `low_soc_threshold = 5.0`
- `battery_agent/tools/analysis_tools.py:258`: `"pct_time_below_20": _round(((hist["SOC"] < 20).sum() / len(hist) * 100) if len(hist) else None),`

**What is wrong.** The 5% depletion cut-off and the 20%/80% bands assume SOC is a percentage. Reproduced with SOC as a 0-1 fraction: every interval is reported as below 20% and 6 intervals as SOC-constrained. The README documents percent, so this only bites on a different export.

**Why it matters.** A unit change produces a plausible-looking but wrong SOC story.

**Suggested fix.** Validate that SOC lies in 0-100 (or detect a 0-1 range) in `validate_schema` and name the threshold constants.

#### Security, privacy & governance

The deterministic checks already report the committed credential and the personal config file. Beyond those: a documented privacy guarantee is not true, and uploaded data is left on disk.

##### F-001 · Critical · `.env` is committed and holds what looks like a live Anthropic API key

_Security, privacy & governance · confidence 0.90 · deterministic check · universal layer_

- `.env:1`: `ANTHROPIC_API_KEY=<redacted>`

**What is wrong.** `.env` is tracked by git and sets 1 variable(s): `ANTHROPIC_API_KEY`. The value's format matches a real Anthropic API key, OpenAI API key (checked by pattern only; the value is not reproduced anywhere in this report). It appears in 2 commit(s) of git history.

**Why it matters.** Anyone who can read the repository can use the credential; on a public repo that is everyone, and automated scanners harvest keys from public GitHub quickly. Deleting the file later does not help, because the value stays in git history.

**Suggested fix.** 1) Revoke and rotate the key now and check the provider's usage log for misuse. 2) `git rm --cached` the file and add it to `.gitignore`. 3) Commit a `.env.example` with placeholder values. 4) Purge history (e.g. `git filter-repo`) only after rotating; rotation is the fix, the rewrite is hygiene.

##### F-006 · Medium · Raw CSV rows are sent to the model, contradicting the documented privacy design

_Security, privacy & governance · confidence 0.95 · LLM lens · universal layer_

- `README.md:77`: `tools return JSON summaries only; Claude never sees CSV rows.`
- `battery_agent/tools/data_tools.py:59`: `preview = df.head(5).copy()`
- `battery_agent/tools/data_tools.py:69`: `"preview": preview.to_dict(orient="records"),`
- `battery_agent/agents/base.py:84`: `"content": json.dumps(result),`
- `battery_agent/orchestrator.py:7`: `The orchestrator never touches raw CSV rows directly.`

**What is wrong.** `load_csv` returns the first five rows, all eight columns, in its tool result, and the agent loop serialises every tool result into the model's context. The README and the orchestrator docstring both say the model never sees CSV rows. Reproduced: the result contains a `preview` of 5 records.

**Why it matters.** Operational data leaves the machine for the LLM provider. Whether five rows matter depends on the data, but the documented guarantee is wrong, and anyone relying on it would approve something they should not.

**Suggested fix.** Drop `preview` (schema and dtypes are enough for the agent), or make it explicit and opt-in, and correct the README and docstring either way.

##### F-029 · Low · Uploaded files are written to the temp directory and never removed

_Security, privacy & governance · confidence 0.90 · LLM lens · universal layer_

- `battery_agent/ui/app.py:118`: `with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:`

**What is wrong.** Uploads are saved with `delete=False` and nothing in the package ever unlinks them (no `unlink`, `remove`, `rmtree` or `TemporaryDirectory` anywhere).

**Why it matters.** On a shared host, users' operational data accumulates on disk under predictable temp paths.

**Suggested fix.** Wrap the run in `try/finally` and unlink the file, or use `tempfile.TemporaryDirectory()`.

##### F-030 · Low · `.claude/settings.local.json` is machine-local config but is committed

_Security, privacy & governance · confidence 0.85 · deterministic check · universal layer_

- `.claude/settings.local.json:4`: `"Bash(mkdir -p \"/c/Users/`
- `.claude/settings.local.json:5`: `"Bash(mkdir -p \"/c/Users/`
- `.claude/settings.local.json:8`: `"Bash(pip install:*)",`

**What is wrong.** `.claude/settings.local.json` is a per-user settings file. It contains absolute paths from the author's machine and wildcard shell-command approvals.

**Why it matters.** It leaks the author's local directory layout, and wildcard approvals (e.g. `pip install:*`) would be pre-approved for anyone who opens the repo with the same tool.

**Suggested fix.** Remove it from git, add `*.local.json` / `.claude/settings.local.json` to `.gitignore`, and commit a reviewed shared `settings.json` if team-wide permissions are wanted.

### LLM / agent design (runs when the code calls an LLM)

#### Agent & tool-loop design

The shared tool_use loop is small and readable. It has gaps in its exit paths and error handling, and the hand-off from model to tools lets model-typed JSON override the authoritative cached results.

##### F-003 · High · Tools prefer JSON typed by the model over the authoritative cached results

_Agent & tool-loop design (also: LLM grounding & evaluation) · confidence 0.95 · LLM lens · universal layer_

- `battery_agent/tools/analysis_tools.py:290`: `rs = revenue_summary or _results.get("revenue_summary", {})`
- `battery_agent/tools/rec_tools.py:28`: `gd = gap_drivers or analysis_tools._results.get("gap_drivers", {})`
- `battery_agent/tools/analysis_tools.py:466`: `"required": ["revenue_summary", "high_price_analysis", "dispatch_comparison", "soc_analysis"],`
- `battery_agent/agents/recommendations.py:69`: `"Call generate_recommendations with both recommendations, the gap_drivers output, "`
- `battery_agent/tools/rec_tools.py:71`: `"supporting_evidence": {`

**What is wrong.** `find_gap_drivers` requires the model to re-type four JSON objects as arguments, and the `or` expression prefers whatever the model supplied over the cached tool outputs. Reproduced: passing `gap_dollars` 9999 produced `total_gap_dollars` 9999 while the cached revenue summary used for the PDF's revenue table still said 1,800, so one report can show two different gaps. The Recommendation agent is worse placed: its only input is the Analysis agent's prose summary, yet it is told to pass the `gap_drivers`, `dispatch_comparison` and `soc_analysis` objects, so it must invent them from prose, and `generate_recommendations` stores them as supporting evidence.

Also raised by the LLM grounding & evaluation lens: `generate_recommendations` builds its `supporting_evidence` block from the gap-driver, dispatch and SOC objects the model passes in, falling back to the cache only when they are empty. Since the Recommendation agent has no way to obtain the real objects, what is stored as evidence is whatever it typed.

**Why it matters, by lens.**
- Agent & tool-loop design: It breaks the system's central promise that conclusions come from tool output: a mis-typed or invented number flows straight into the report.
- LLM grounding & evaluation: The audit trail that is supposed to justify each recommendation is itself unverified.

**Suggested fix.** Tools take no model-supplied data. Read only from the run context, remove these parameters from the schemas (or ignore them), and check in code that prerequisite tools ran.

##### F-014 · Medium · A malformed tool call or tool exception crashes the whole run

_Agent & tool-loop design · confidence 0.90 · LLM lens · universal layer_

- `battery_agent/agents/base.py:78`: `result = tool_executor(block.name, block.input)`
- `battery_agent/tools/data_tools.py:279`: `return load_csv(inputs["file_path"])`
- `battery_agent/tools/rec_tools.py:43`: `missing = REQUIRED_REC_FIELDS - set(rec.keys())`

**What is wrong.** The loop calls the tool executor with no try/except. Reproduced: `execute_tool('load_csv', {})` raises `KeyError` and `generate_recommendations` with string items raises `AttributeError`; neither is caught, so the exception unwinds to the orchestrator and the whole analysis is reported as failed. Tool results also never set `is_error`, so the model cannot tell a failure from data.

**Why it matters.** A single bad argument from the model costs the user the entire run instead of a correctable error.

**Suggested fix.** Wrap the executor call, and return a `tool_result` with `is_error: true` and a message the model can act on.

##### F-015 · Medium · Loop exits at the iteration cap or on max_tokens return empty or truncated text

_Agent & tool-loop design · confidence 0.90 · LLM lens · universal layer_

- `battery_agent/agents/base.py:91`: `unexpected stop_reason`
- `battery_agent/agents/base.py:95`: `return _extract_text(messages[-1]["content"]) if messages else ""`
- `battery_agent/agents/base.py:22`: `if hasattr(block, "type") and block.type == "text":`

**What is wrong.** Reproduced: when the cap is hit the last message is a user turn whose content is a list of dicts, and `_extract_text` only looks for an attribute `.type`, so it returns an empty string even though the log says 'returning partial result'. A `max_tokens` stop (limit 4096) falls into the generic branch and returns possibly truncated text, which the orchestrator accepts as a complete summary because it only tests for emptiness.

**Why it matters.** A truncated analysis summary is passed on as evidence for the recommendations.

**Suggested fix.** Handle each stop reason explicitly: raise on the cap, and on `max_tokens` either continue or fail loudly.

##### F-016 · Medium · The prompt says to stop on critical nulls, but the tool reports success

_Agent & tool-loop design · confidence 0.80 · LLM lens · universal layer_

- `battery_agent/agents/data_prep.py:28`: `If validate_schema returns errors (missing columns or critical nulls), stop immediately and`
- `battery_agent/tools/data_tools.py:111`: `"message": "Schema valid." if not critical_nulls else f"Schema valid but {len(critical_nulls)} columns have nulls.",`

**What is wrong.** The Data Prep prompt says to stop immediately if `validate_schema` returns critical nulls. The tool returns `status: ok` with a message 'Schema valid but N columns have nulls' (reproduced with six null revenues), so whether the pipeline stops depends on the model's reading of a message string. Nothing in code stops it.

**Why it matters.** The safeguard exists only as prose, so it fires inconsistently.

**Suggested fix.** Return `status: error` for critical nulls, or have the orchestrator inspect the structured result and halt.

#### LLM grounding & evaluation

Grounding is claimed in three places (README, system prompt, PDF footer) but enforced by a keyword check, and there are no evals for any LLM stage.

##### F-004 · High · The evidence validator accepts any text containing a common word, while every PDF promises grounding

_LLM grounding & evaluation · confidence 0.90 · LLM lens · universal layer_

- `battery_agent/tools/rec_tools.py:51`: `evidence_keywords = ["dispatch", "soc", "price", "charge", "discharge", "revenue", "gap", "interval"]`
- `battery_agent/tools/rec_tools.py:52`: `if not any(kw in reasoning_lower for kw in evidence_keywords):`
- `battery_agent/report/pdf_generator.py:394`: `"No recommendations were generated without supporting evidence.",`
- `battery_agent/agents/recommendations.py:26`: `You MUST NOT invent numbers, claim facts not in the analysis, or give generic advice.`

**What is wrong.** A recommendation passes if its reasoning contains any of eight everyday words ('price', 'charge', 'gap', ...). Reproduced: a recommendation claiming 'this will add $48,000 per day' is accepted because the reasoning contains 'charge'. `expected_benefit` and `tradeoff` are not checked at all. Every PDF nevertheless prints that all findings are grounded in quantitative tool outputs and that no recommendation was generated without supporting evidence.

**Why it matters.** The strongest claim the product makes to its reader is not backed by anything that would catch a fabricated number.

**Suggested fix.** Extract every number in the reasoning and expected benefit and require each to match, within a tolerance, a value in the cached analysis results. Reject or flag unmatched numbers, and soften the footer until that check exists.

##### F-017 · Medium · No eval or regression set for any LLM stage, and the numeric core is untested

_LLM grounding & evaluation (also: Testing & observability) · confidence 0.99 · deterministic check · universal layer_

- Searched repository-wide: no tests/ directory and no test_*.py, *_test.py or *.spec.* files

**What is wrong.** Nothing can tell you whether a prompt edit or a model change made the recommendations worse. The pandas tools are deterministic and could be pinned with a golden CSV; the LLM stages need a small golden set that records the expected primary driver, number-matching checks and a cost/latency log per run.

Also raised by the Testing & observability lens: No test directory or test files exist among 34 tracked files, for about 2354 lines of code.

**Why it matters, by lens.**
- LLM grounding & evaluation: Every change to a prompt or the model id is a blind change.
- Testing & observability: There is no safety net for refactors, and no way to prove the numeric logic stays correct.

**Suggested fix.** Add a golden-CSV test for the tools, a few recorded end-to-end cases with expected drivers, and a check on the number-matching validator proposed above.

### RAG assessment

The RAG pack was **not applied**. No RAG pipeline detected (needs embedding plus a vector store or retriever). Weak signals: none.

A RAG lens never runs without evidence of a RAG pipeline, so this review makes no claims about retrieval, chunking or RAG evals. Skipped lenses: RAG: corpus governance & ingestion, RAG: retrieval quality, RAG: generation integrity, RAG: evaluation architecture, RAG: scalability, cost & observability.

## 3. Conventions applied

Team conventions can only nudge: they are capped below defect severity, and inferred rules stay `unconfirmed` until a human confirms them.

| Convention | Provenance | Status | Adherence | Source |
|---|---|---|---|---|
| Functions declare return type annotations | inferred | unconfirmed | 94% of 46 | inferred from code |
| Public functions have docstrings | inferred | unconfirmed | 46% of 33 | inferred from code |
| Failures are returned as error values (e.g. {"status": "error"}) instead of raised | inferred | unconfirmed | 94% of 16 | inferred from code |
| Diagnostics are written with print() | inferred | unconfirmed | 100% of 7 | inferred from code |
| Imports between modules of this package are relative | inferred | unconfirmed | 85% of 13 | inferred from code |
| String literals use double quotes | inferred | unconfirmed | 90% of 1234 | inferred from code |
| Functions are snake_case | inferred | unconfirmed | 100% of 46 | inferred from code |
| Lines stay within 100 columns | inferred | unconfirmed | 98% of 2340 | inferred from code |

### Open questions for the team

Answer these (see `--gate answers`) and the next run will enforce what you confirm.

1. These 4 patterns are followed almost everywhere: Functions declare return type annotations (94%); Failures are returned as error values (e.g. {"status": "error"}) instead of raised (94%); Functions are snake_case (100%); Lines stay within 100 columns (98%). Enforce them as team conventions?
2. docs: 46% (15/33) of the code follows "Public functions have docstrings" (e.g. battery_agent/agents/analysis.py:49, battery_agent/agents/base.py:27; exceptions: battery_agent/main.py:15, battery_agent/main.py:56). Is this a deliberate team convention, or incidental?
3. logging: 100% (7/7) of the code follows "Diagnostics are written with print()" (e.g. battery_agent/main.py:101, battery_agent/main.py:116). Is this a deliberate team convention, or incidental?
4. imports: 85% (11/13) of the code follows "Imports between modules of this package are relative" (e.g. battery_agent/agents/analysis.py:12, battery_agent/agents/analysis.py:13; exceptions: battery_agent/main.py:96, battery_agent/ui/app.py:114). Is this a deliberate team convention, or incidental?
5. style: 90% (1117/1234) of the code follows "String literals use double quotes". Is this a deliberate team convention, or incidental?

## 4. Appendix

### Nits and low-confidence findings

- **F-034** (Low, confidence 0.40) Naive timestamps are assumed to be UTC: `battery_agent/tools/data_tools.py:131`
- **F-035** (Low, confidence 0.45) Expected-schedule rows are loaded and cleaned but never analysed: `battery_agent/tools/analysis_tools.py:172`
- **F-039** (Nit, confidence 0.80) Helper functions lack return annotations, unlike most of the codebase: `battery_agent/report/pdf_generator.py:176`
- **F-040** (Nit, confidence 0.95) Two identical READMEs will drift apart: `README.md:1`
- **F-041** (Nit, confidence 0.90) README setup command points to a path that does not exist from the README's directory: `README.md:14`

### Verification

0 candidate finding(s) were dropped because their evidence could not be verified or they broke a convention rule.

### Run details

- tool version 0.1.0, lens prompts v2, runner `replay`
- lens findings produced by: recorded (authored by Claude from reading the code; not an API run)
- stack: anthropic, numpy, pandas, reportlab, streamlit; RAG: not detected; LLM pipeline: detected
- inline-comment budget 16, minimum confidence 0.5
