"""Deterministic checks that need no LLM.

If a rule can be decided by parsing, it is decided by parsing: that is cheaper,
reproducible and never hallucinates. Each check returns `Finding`s whose evidence
is later re-verified by the synthesis stage, same as an LLM finding.

Secret values are never copied into findings. Evidence for a secret is the
location plus the variable name (`KEY=<redacted>`).
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Iterator
from pathlib import PurePosixPath

from ..models import Evidence, Finding
from .redact import find_secrets, is_env_file, is_placeholder, parse_env_lines, redact_secrets
from .pyimports import _module_name, _resolve_imports
from .snapshot import Snapshot, _git, is_vendored_or_generated
from .structure import check_import_cycles
from .stack import StackInfo
from .topology import Topology


def _finding(**kw) -> Finding:
    kw.setdefault("origin", "static")
    kw.setdefault("confidence", 0.9)
    return Finding(**kw)


def _src(snap: Snapshot, path: str, line: int, limit: int = 140) -> str:
    return (snap.line(path, line) or "").strip()[:limit]


# ---------------------------------------------------------------------------
# 1. Committed secrets
# ---------------------------------------------------------------------------
def check_committed_secrets(snap: Snapshot, stack: StackInfo) -> list[Finding]:
    findings: list[Finding] = []
    env_files: set[str] = set()

    for path in sorted(snap.files):
        if not is_env_file(path):
            continue
        env_files.add(path)
        text = snap.read(path) or ""
        rows = [(n, k, v) for n, k, v in parse_env_lines(text) if not is_placeholder(v)]
        if not rows:
            continue
        kinds = sorted({kind for kind, _, _ in find_secrets(text)})
        commits = (_git(snap.root, "log", "--oneline", "--all", "--", path) or "").strip().splitlines()
        history = f" It appears in {len(commits)} commit(s) of git history." if commits else ""
        names = ", ".join(f"`{k}`" for _, k, _ in rows[:6])
        if kinds:
            sev, title = "critical", f"`{path}` is committed and holds what looks like a live {kinds[0]}"
            detail = f"The value's format matches a real {', '.join(kinds)} (checked by pattern only; the value is not reproduced anywhere in this report)."
        else:
            sev, title = "high", f"`{path}` is committed with non-empty values"
            detail = "No known credential format matched, but the file is meant to be local-only."
        findings.append(
            _finding(
                lens="governance",
                severity=sev,
                title=title,
                claim=f"`{path}` is tracked by git and sets {len(rows)} variable(s): {names}. {detail}{history}",
                why_it_matters=(
                    "Anyone who can read the repository can use the credential; on a public repo that is everyone, "
                    "and automated scanners harvest keys from public GitHub quickly. Deleting the file later does not "
                    "help, because the value stays in git history."
                ),
                suggested_fix=(
                    "1) Revoke and rotate the key now and check the provider's usage log for misuse. "
                    "2) `git rm --cached` the file and add it to `.gitignore`. "
                    "3) Commit a `.env.example` with placeholder values. "
                    "4) Purge history (e.g. `git filter-repo`) only after rotating; rotation is the fix, the rewrite is hygiene."
                ),
                evidence=[Evidence(file=path, line=n, quote=f"{k}=<redacted>", redacted=True) for n, k, _ in rows[:5]],
                dedupe_key=f"secret:{path}",
            )
        )

    # Secrets embedded in other tracked text files.
    for path, info in sorted(snap.files.items()):
        if path in env_files or not info.is_text or is_vendored_or_generated(path):
            continue
        text = snap.read(path)
        if not text:
            continue
        hits = find_secrets(text)
        if not hits:
            continue
        ev = []
        for kind, start, _ in hits[:3]:
            lineno = text.count("\n", 0, start) + 1
            ev.append(Evidence(file=path, line=lineno, quote=redact_secrets(snap.line(path, lineno) or "").strip()[:140], redacted=True))
        findings.append(
            _finding(
                lens="governance",
                severity="critical",
                title=f"`{path}` contains a hard-coded credential",
                claim=f"{len(hits)} secret-shaped value(s) ({', '.join(sorted({h[0] for h in hits}))}) appear in `{path}`.",
                why_it_matters="Credentials in source are readable by everyone with repo access and persist in history.",
                suggested_fix="Rotate the credential, load it from the environment or a secret manager, and purge history after rotation.",
                evidence=ev,
                dedupe_key=f"secret:{path}",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 2. Tracked build artifacts and missing .gitignore
# ---------------------------------------------------------------------------
_ARTIFACTS = [
    (re.compile(r"(^|/)__pycache__/|\.py[co]$"), "compiled Python bytecode"),
    (re.compile(r"(^|/)node_modules/"), "node_modules"),
    (re.compile(r"(^|/)\.DS_Store$"), ".DS_Store"),
    (re.compile(r"(^|/)(\.venv|venv)/"), "a virtualenv"),
    (re.compile(r"(^|/)\.idea/"), "IDE metadata"),
    (re.compile(r"\.log$"), "log files"),
]


def check_tracked_artifacts(snap: Snapshot, stack: StackInfo) -> list[Finding]:
    hits: list[tuple[str, str]] = []
    for path in sorted(snap.files):
        for pat, label in _ARTIFACTS:
            if pat.search(path):
                hits.append((path, label))
                break
    if not hits:
        return []
    labels = sorted({h[1] for h in hits})
    evidence = [Evidence(file=p, line=0, quote="") for p, _ in hits[:3]]
    claim = f"{len(hits)} tracked file(s) are generated artifacts ({', '.join(labels)})."
    severity = "low"
    if not stack.has_gitignore:
        severity = "medium"
        claim += " There is no `.gitignore`, so nothing stops further artifacts, local config or `.env` files from being committed."
        evidence.append(Evidence(kind="absence", file="(repo)", quote="no .gitignore at the repo root"))
    return [
        _finding(
            lens="reliability",
            severity=severity,
            title="Generated artifacts are committed and there is no .gitignore" if not stack.has_gitignore else "Generated artifacts are committed",
            claim=claim,
            why_it_matters="Bytecode and caches add noisy diffs and stale files, and a missing .gitignore is the usual root cause of committed secrets.",
            suggested_fix="Add a language-appropriate `.gitignore` (Python: `__pycache__/`, `*.pyc`, `.env`, `.venv/`), then `git rm -r --cached` the artifacts.",
            evidence=evidence,
            dedupe_key="repo-hygiene",
        )
    ]


# ---------------------------------------------------------------------------
# 3. Personal tool config committed
# ---------------------------------------------------------------------------
_USER_PATH = re.compile(r"(/Users/|/home/|/c/Users/|[A-Za-z]:\\\\?Users\\\\?)")


def check_personal_config(snap: Snapshot, stack: StackInfo) -> list[Finding]:
    out: list[Finding] = []
    for path in sorted(snap.files):
        name = PurePosixPath(path).name
        if not (name.endswith(".local.json") or ".local." in name):
            continue
        ev: list[Evidence] = []
        for i, line in enumerate(snap.lines(path), start=1):
            m = _USER_PATH.search(line)
            if m:
                # Quote only up to the path prefix: stays verbatim, never includes the username.
                ev.append(Evidence(file=path, line=i, quote=line.strip()[: line.strip().find(m.group(0)) + len(m.group(0))]))
            if len(ev) >= 2:
                break
        perms = [i for i, line in enumerate(snap.lines(path), start=1) if re.search(r"Bash\([^)]*:\*\)", line)]
        if not ev and not perms:
            continue
        if perms:
            ev.append(Evidence(file=path, line=perms[0], quote=_src(snap, path, perms[0])))
        out.append(
            _finding(
                lens="governance",
                severity="low",
                confidence=0.85,
                title=f"`{path}` is machine-local config but is committed",
                claim=(
                    f"`{path}` is a per-user settings file. It contains absolute paths from the author's machine"
                    + (" and wildcard shell-command approvals" if perms else "")
                    + "."
                ),
                why_it_matters="It leaks the author's local directory layout, and wildcard approvals (e.g. `pip install:*`) would be pre-approved for anyone who opens the repo with the same tool.",
                suggested_fix="Remove it from git, add `*.local.json` / `.claude/settings.local.json` to `.gitignore`, and commit a reviewed shared `settings.json` if team-wide permissions are wanted.",
                evidence=ev,
                dedupe_key="personal-config",
            )
        )
    return out


# ---------------------------------------------------------------------------
# 4. Environment read at import time, before load_dotenv()
# ---------------------------------------------------------------------------
def _walk_import_time(node: ast.AST) -> Iterator[ast.AST]:
    """Yield nodes that execute at import (module/class level), skipping function bodies."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _walk_import_time(child)


def _env_read(node: ast.AST) -> str | None:
    """Return the variable name (or '?') if `node` reads the process environment."""
    if isinstance(node, ast.Call):
        fn = ast.unparse(node.func)
        if fn in {"os.environ.get", "os.getenv", "environ.get", "getenv"}:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                return node.args[0].value
            return "?"
    if isinstance(node, ast.Subscript) and ast.unparse(node.value) in {"os.environ", "environ"}:
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            return node.slice.value
        return "?"
    return None


def check_env_before_dotenv(snap: Snapshot, stack: StackInfo) -> list[Finding]:
    py = snap.code_paths("python")
    index = {_module_name(p): p for p in py}
    trees = {p: snap.parse(p) for p in py}

    env_reads: dict[str, list[tuple[int, str]]] = {}
    for p, tree in trees.items():
        if tree is None:
            continue
        reads = [(n.lineno, v) for n in _walk_import_time(tree) if (v := _env_read(n)) is not None]
        if reads:
            env_reads[p] = reads

    findings: list[Finding] = []
    for p, tree in sorted(trees.items()):
        if tree is None:
            continue
        load_line = next(
            (n.lineno for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
             and ast.unparse(n.value.func).endswith("load_dotenv")),
            None,
        )
        if load_line is None:
            continue

        # Local modules imported *above* the load_dotenv() call, followed transitively.
        offenders: list[tuple[str, int, str, int]] = []  # (dep_path, read_line, var, import_line_in_p)
        for stmt in tree.body:
            if not isinstance(stmt, (ast.Import, ast.ImportFrom)) or stmt.lineno >= load_line:
                continue
            seen: set[str] = set()
            stack_ = list(_resolve_imports(snap, index, p, stmt))
            while stack_:
                dep = stack_.pop()
                if dep in seen or dep == p:
                    continue
                seen.add(dep)
                if dep in env_reads:
                    line, var = env_reads[dep][0]
                    offenders.append((dep, line, var, stmt.lineno))
                dep_tree = trees.get(dep)
                if dep_tree:
                    for s in dep_tree.body:
                        if isinstance(s, (ast.Import, ast.ImportFrom)):
                            stack_.extend(_resolve_imports(snap, index, dep, s))
        if not offenders:
            continue

        dep, read_line, var, import_line = sorted(offenders)[0]
        findings.append(
            _finding(
                lens="accuracy",
                severity="medium",
                confidence=0.85,
                title=f"`{var}` is read from the environment at import time, before `load_dotenv()` runs",
                claim=(
                    f"`{p}` calls `load_dotenv()` at line {load_line}, but at line {import_line} it already imported `{dep}`, "
                    f"which reads `{var}` at module level (line {read_line}). The value is frozen at import, so a `{var}` defined "
                    "only in a `.env` file is ignored; only a variable exported in the shell takes effect."
                ),
                why_it_matters="Documentation says the setting can be overridden via the environment, but the override silently does nothing when it lives in `.env`, which is how most local setups configure it.",
                suggested_fix=(
                    f"Read `{var}` lazily inside a function (e.g. `def _model(): return os.environ.get(...)`), or call "
                    "`load_dotenv()` in the entry points before any package import."
                ),
                evidence=[
                    Evidence(file=dep, line=read_line, quote=_src(snap, dep, read_line)),
                    Evidence(file=p, line=import_line, quote=_src(snap, p, import_line)),
                    Evidence(file=p, line=load_line, quote=_src(snap, p, load_line)),
                ],
                dedupe_key=f"env-order:{var}",
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 5. Shared mutable module state (matters most behind a concurrent server)
# ---------------------------------------------------------------------------
_MUTATORS = {"clear", "update", "append", "extend", "pop", "popitem", "setdefault", "add", "remove", "discard", "insert"}
_CONTAINER_CALLS = {"dict", "list", "set", "defaultdict", "OrderedDict", "deque"}


def _is_mutable_container(value: ast.AST) -> bool:
    if isinstance(value, (ast.Dict, ast.List, ast.Set)):
        return True
    return isinstance(value, ast.Call) and ast.unparse(value.func).split(".")[-1] in _CONTAINER_CALLS


def _base_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def check_shared_mutable_state(snap: Snapshot, stack: StackInfo) -> list[Finding]:
    containers: list[tuple[str, str, int]] = []
    mutated: dict[str, int] = {}
    uses_threads = False

    for path in snap.code_paths("python"):
        tree = snap.parse(path)
        if tree is None:
            continue
        for node in tree.body:
            names: list[str] = []
            value = None
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                names, value = [node.target.id], node.value
            if value is not None and _is_mutable_container(value):
                containers.extend((path, n, node.lineno) for n in names if not n.isupper())
        for node in ast.walk(tree):
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            elif isinstance(node, ast.Delete):
                targets = list(node.targets)
            for t in targets:
                if isinstance(t, ast.Subscript) and (n := _base_name(t.value)):
                    mutated[n] = mutated.get(n, 0) + 1
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATORS:
                if n := _base_name(node.func.value):
                    mutated[n] = mutated.get(n, 0) + 1
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                uses_threads = uses_threads or any(m.split(".")[0] in {"threading", "concurrent", "asyncio"} for m in mods)

    shared = [(p, n, ln) for p, n, ln in containers if mutated.get(n)]
    if not shared:
        return []

    if stack.web_serving:
        severity = "high"
        exposure = (
            f"The repo is served by {', '.join(stack.web_serving)}, which runs concurrent sessions in one process, so two "
            "simultaneous runs share these dicts: one run's reset can wipe another's data, and results can cross between users."
        )
    elif uses_threads:
        severity, exposure = "medium", "The code uses threads/async, so concurrent runs would share these dicts."
    else:
        severity, exposure = "low", "Single-threaded today, but it blocks concurrency, reuse and isolated tests."

    listing = "; ".join(f"`{n}` ({p}:{ln})" for p, n, ln in shared[:6])
    return [
        _finding(
            lens="scalability",
            severity=severity,
            confidence=0.85,
            title="Per-run state lives in module-level dicts shared by every caller",
            claim=f"{len(shared)} module-level mutable container(s) are written during a run: {listing}. {exposure}",
            why_it_matters="Hidden global state makes runs non-reentrant. It also couples agents and tools through side channels instead of explicit inputs, which makes them harder to test and reason about.",
            suggested_fix="Create a run-scoped context object (a dataclass holding the DataFrame and cached results) and pass it through orchestrator -> agents -> tools, so each run owns its state.",
            evidence=[Evidence(file=p, line=ln, quote=_src(snap, p, ln)) for p, _, ln in shared[:4]],
            dedupe_key="shared-module-state",
        )
    ]


# ---------------------------------------------------------------------------
# 6. No tests / no CI
# ---------------------------------------------------------------------------
def check_no_tests(snap: Snapshot, stack: StackInfo) -> list[Finding]:
    code_lines = sum(v for k, v in stack.languages.items() if k in {"python", "typescript", "javascript", "go", "rust", "java"})
    if stack.has_tests or code_lines < 100:
        return []
    return [
        _finding(
            lens="reliability",
            severity="medium",
            confidence=0.95,
            title="No automated tests",
            claim=f"No test directory or test files exist among {len(snap.files)} tracked files, for about {code_lines} lines of code.",
            why_it_matters="There is no safety net for refactors, and no way to prove the numeric logic stays correct.",
            suggested_fix="Start with the deterministic core: functions that take data and return numbers need no network or LLM, so they are cheap to pin with a few fixtures.",
            evidence=[Evidence(kind="absence", file="(repo)", quote="no tests/ directory and no test_*.py, *_test.py or *.spec.* files")],
            dedupe_key="no-tests",
        )
    ]


def check_no_ci(snap: Snapshot, stack: StackInfo) -> list[Finding]:
    if stack.has_ci or stack.total_files < 8:
        return []
    return [
        _finding(
            lens="reliability",
            severity="low",
            confidence=0.9,
            title="No CI configuration",
            claim="No CI workflow (GitHub Actions, GitLab CI, CircleCI, Jenkins, Azure Pipelines) is present.",
            why_it_matters="Nothing runs tests, linting or secret scanning on pushes and pull requests.",
            suggested_fix="Add a minimal workflow that installs dependencies, runs the tests, and runs a secret scanner on every PR.",
            evidence=[Evidence(kind="absence", file="(repo)", quote="no .github/workflows, .gitlab-ci.yml, .circleci, Jenkinsfile or azure-pipelines.yml")],
            dedupe_key="no-ci",
        )
    ]


# ---------------------------------------------------------------------------
# 7. README drift
# ---------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*```")
_README_CMDS = [
    re.compile(r"pip3? install -r\s+(\S+)"),
    re.compile(r"streamlit run\s+(\S+)"),
    re.compile(r"(?:python3?|node)\s+(\S+\.(?:py|js|ts))\b"),
]


def check_readme_paths(snap: Snapshot, stack: StackInfo) -> list[Finding]:
    findings: list[Finding] = []
    for readme in stack.readmes:
        base = PurePosixPath(readme).parent
        in_fence = False
        broken: list[tuple[int, str, str]] = []
        for i, line in enumerate(snap.lines(readme), start=1):
            if _FENCE.match(line):
                in_fence = not in_fence
                continue
            if not in_fence:
                continue
            for pat in _README_CMDS:
                m = pat.search(line)
                if not m:
                    continue
                target = m.group(1)
                if target.startswith(("-", "http", "/", "~", "path/")) or "<" in target:
                    continue
                if (base / target).as_posix() not in snap.files and target not in snap.files:
                    broken.append((i, line.strip(), target))
        if broken:
            n, text, target = broken[0]
            where = [p for p in snap.files if PurePosixPath(p).name == PurePosixPath(target).name]
            hint = f" A file with that name exists at `{where[0]}`." if where else ""
            findings.append(
                _finding(
                    lens="reliability",
                    severity="nit",
                    confidence=0.9,
                    title="README setup command points to a path that does not exist from the README's directory",
                    claim=f"`{readme}` line {n} runs `{text}`, but `{target}` does not exist relative to `{base.as_posix() or '.'}`.{hint}",
                    why_it_matters="A first-time user following the README hits an immediate error.",
                    suggested_fix="Fix the path in the README, or move the file so the documented command works.",
                    evidence=[Evidence(file=readme, line=n, quote=text)],
                    dedupe_key=f"readme-path:{readme}",
                )
            )
    return findings


def check_duplicate_readmes(snap: Snapshot, stack: StackInfo) -> list[Finding]:
    by_hash: dict[str, list[str]] = {}
    for r in stack.readmes:
        h = hashlib.sha256((snap.read(r) or "").encode()).hexdigest()
        by_hash.setdefault(h, []).append(r)
    out = []
    for paths in by_hash.values():
        if len(paths) > 1:
            out.append(
                _finding(
                    lens="reliability",
                    severity="nit",
                    confidence=0.95,
                    title="Two identical READMEs will drift apart",
                    claim=f"{' and '.join(f'`{p}`' for p in paths)} have identical content.",
                    why_it_matters="Duplicated docs get updated in one place only; this is how setup instructions go stale.",
                    suggested_fix="Keep one README and link to it from the other location.",
                    evidence=[Evidence(file=p, line=1, quote=(snap.line(p, 1) or "").strip()[:100]) for p in paths],
                    dedupe_key="duplicate-readme",
                )
            )
    return out


CHECKS = [
    check_import_cycles,
    check_committed_secrets,
    check_tracked_artifacts,
    check_personal_config,
    check_env_before_dotenv,
    check_shared_mutable_state,
    check_no_tests,
    check_no_ci,
    check_readme_paths,
    check_duplicate_readmes,
]


def run_static_checks(snap: Snapshot, stack: StackInfo, topo: Topology | None = None) -> list[Finding]:
    out: list[Finding] = []
    for check in CHECKS:
        out.extend(check(snap, stack))
    return out
