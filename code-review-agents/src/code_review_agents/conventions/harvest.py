"""Harvest team conventions that are already written down, and infer the ones that are not.

Declared sources are trusted (provenance="declared"). Inferred rules are *candidates*:
the agent measures how consistently the codebase follows a pattern and reports the
adherence rate, but never enforces an inferred rule until the team confirms it.
"""

from __future__ import annotations

import ast
import configparser
import hashlib
import io
import json
import re
import tokenize
import tomllib
from collections.abc import Iterator
from pathlib import PurePosixPath

import yaml

from ..models import ConventionRule
from ..scout.snapshot import Snapshot

TOPIC_PATTERNS: dict[str, re.Pattern[str]] = {
    "error-handling": re.compile(r"error|exception|raise|result type|try/except", re.I),
    "logging": re.compile(r"\blog(ging|s|ger)?\b|print\(", re.I),
    "testing": re.compile(r"\btests?\b|pytest|coverage|mock|fixture", re.I),
    "typing": re.compile(r"\btyp(e|ed|es|ing)\b|annotation|mypy", re.I),
    "naming": re.compile(r"\bnam(e|es|ing)\b|camel|snake|pascal", re.I),
    "docs": re.compile(r"docstring|comment|documentation|readme", re.I),
    "imports": re.compile(r"\bimports?\b", re.I),
    "style": re.compile(r"format|lint|line length|indent|quote|whitespace", re.I),
    "dependencies": re.compile(r"dependenc|requirements|\bpin(ned)?\b", re.I),
    "security": re.compile(r"secret|credential|auth|injection|sanitiz", re.I),
    "git": re.compile(r"commit|branch|pull request|\bPR\b", re.I),
}

DECLARED_DOCS = ["REVIEW.md", "CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md", ".github/REVIEW.md", "docs/CONTRIBUTING.md"]
CODEOWNERS_PATHS = ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"]
_HEADING_OK = re.compile(r"convention|style|standard|guideline|rule|testing|review|code", re.I)
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)")


def slug(text: str, limit: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "rule"


def classify(text: str) -> str:
    for topic, pat in TOPIC_PATTERNS.items():
        if pat.search(text):
            return topic
    return "general"


# ---------------------------------------------------------------------------
# Declared
# ---------------------------------------------------------------------------
def _from_review_rules_yaml(snap: Snapshot) -> tuple[list[ConventionRule], dict]:
    rules: list[ConventionRule] = []
    extras: dict = {"ignore_paths": [], "legacy_paths": []}
    for name in (".review-rules.yaml", ".review-rules.yml"):
        text = snap.read(name)
        if text is None:
            continue
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        extras["ignore_paths"] += list(data.get("ignore_paths", []))
        extras["legacy_paths"] += list(data.get("legacy_paths", []))
        for r in data.get("rules", []):
            if not isinstance(r, dict) or "statement" not in r:
                continue
            rules.append(
                ConventionRule(
                    id=str(r.get("id") or slug(r["statement"])),
                    topic=str(r.get("topic") or classify(r["statement"])),
                    statement=str(r["statement"]),
                    provenance="declared",
                    status="confirmed",
                    source=name,
                    severity_cap=r.get("severity_cap", "low"),
                )
            )
    return rules, extras


def _from_docs(snap: Snapshot) -> list[ConventionRule]:
    rules: list[ConventionRule] = []
    for name in DECLARED_DOCS:
        text = snap.read(name)
        if text is None:
            continue
        section_ok = False
        count = 0
        for i, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                section_ok = bool(_HEADING_OK.search(line))
                continue
            m = _BULLET.match(line)
            if section_ok and m and count < 25:
                statement = m.group(1).strip()
                if 12 <= len(statement) <= 220:
                    rules.append(
                        ConventionRule(
                            id=f"{slug(PurePosixPath(name).stem)}-{slug(statement, 32)}",
                            topic=classify(statement),
                            statement=statement,
                            provenance="declared",
                            status="confirmed",
                            source=f"{name}:{i}",
                        )
                    )
                    count += 1
    return rules


def _toml_rules(snap: Snapshot) -> list[ConventionRule]:
    text = snap.read("pyproject.toml")
    if not text:
        return []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    tool = data.get("tool", {})
    rules: list[ConventionRule] = []

    def add(rid: str, topic: str, statement: str, by: str) -> None:
        rules.append(ConventionRule(id=rid, topic=topic, statement=statement, provenance="declared", status="confirmed", source="pyproject.toml", enforced_by=by))

    for name in ("ruff", "black"):
        ll = tool.get(name, {}).get("line-length")
        if ll:
            add("line-length", "style", f"Lines stay within {ll} columns", name)
    if "ruff" in tool:
        add("lint", "style", "Code passes ruff lint checks", "ruff")
    if tool.get("mypy", {}).get("strict"):
        add("type-checking", "typing", "Code passes mypy --strict", "mypy")
    elif "mypy" in tool:
        add("type-checking", "typing", "Code passes mypy", "mypy")
    if "isort" in tool:
        add("import-order", "imports", "Imports are sorted by isort", "isort")
    return rules


def _ini_rules(snap: Snapshot) -> list[ConventionRule]:
    rules: list[ConventionRule] = []
    for name in ("setup.cfg", ".flake8", "tox.ini"):
        text = snap.read(name)
        if not text:
            continue
        cp = configparser.ConfigParser()
        try:
            cp.read_string(text)
        except configparser.Error:
            continue
        if cp.has_section("flake8"):
            ll = cp["flake8"].get("max-line-length")
            rules.append(ConventionRule(id="lint", topic="style", statement="Code passes flake8", provenance="declared", status="confirmed", source=name, enforced_by="flake8"))
            if ll:
                rules.append(ConventionRule(id="line-length", topic="style", statement=f"Lines stay within {ll} columns", provenance="declared", status="confirmed", source=name, enforced_by="flake8"))
    text = snap.read(".editorconfig")
    if text:
        indent = re.search(r"^indent_style\s*=\s*(\w+)", text, re.M)
        size = re.search(r"^indent_size\s*=\s*(\d+)", text, re.M)
        if indent:
            rules.append(ConventionRule(id="indentation", topic="style", statement=f"Indent with {indent.group(1)}" + (f" ({size.group(1)})" if size else ""), provenance="declared", status="confirmed", source=".editorconfig", enforced_by="editorconfig"))
    return rules


_HOOK_TOPICS = {"ruff": "style", "black": "style", "flake8": "style", "isort": "imports", "mypy": "typing", "prettier": "style", "eslint": "style", "gitleaks": "security", "detect-secrets": "security"}


def _precommit_rules(snap: Snapshot) -> list[ConventionRule]:
    text = snap.read(".pre-commit-config.yaml")
    if not text:
        return []
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return []
    rules = []
    for repo in data.get("repos", []):
        for hook in repo.get("hooks", []):
            hid = str(hook.get("id", ""))
            for key, topic in _HOOK_TOPICS.items():
                if key in hid:
                    rules.append(ConventionRule(id=f"hook-{key}", topic=topic, statement=f"pre-commit runs {key}", provenance="declared", status="confirmed", source=".pre-commit-config.yaml", enforced_by=key))
    return rules


def _js_rules(snap: Snapshot) -> list[ConventionRule]:
    rules: list[ConventionRule] = []
    pkg = snap.read("package.json")
    if pkg:
        try:
            data = json.loads(pkg)
        except json.JSONDecodeError:
            data = {}
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        for tool in ("eslint", "prettier"):
            if tool in deps:
                rules.append(ConventionRule(id=f"js-{tool}", topic="style", statement=f"Code passes {tool}", provenance="declared", status="confirmed", source="package.json", enforced_by=tool))
    ts = snap.read("tsconfig.json")
    if ts and re.search(r'"strict"\s*:\s*true', ts):
        rules.append(ConventionRule(id="ts-strict", topic="typing", statement="TypeScript strict mode is on", provenance="declared", status="confirmed", source="tsconfig.json", enforced_by="tsc"))
    return rules


def harvest_declared(snap: Snapshot) -> tuple[list[ConventionRule], dict]:
    rules, extras = _from_review_rules_yaml(snap)
    rules += _from_docs(snap)
    rules += _toml_rules(snap) + _ini_rules(snap) + _precommit_rules(snap) + _js_rules(snap)
    owners: list[str] = []
    for name in CODEOWNERS_PATHS:
        text = snap.read(name)
        if text:
            owners = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            break
    extras["codeowners"] = owners
    seen: set[str] = set()
    unique = []
    for r in rules:
        if r.id not in seen:
            seen.add(r.id)
            unique.append(r)
    return unique, extras


def declared_fingerprint(snap: Snapshot) -> str:
    """Hash of every file that can declare conventions. A change invalidates cached answers."""
    names = [".review-rules.yaml", ".review-rules.yml", "pyproject.toml", "setup.cfg", ".flake8", "tox.ini",
             ".editorconfig", ".pre-commit-config.yaml", "package.json", "tsconfig.json", *DECLARED_DOCS, *CODEOWNERS_PATHS]
    h = hashlib.sha256()
    for n in names:
        text = snap.read(n)
        if text is not None:
            h.update(n.encode())
            h.update(text.encode())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Inferred (Python)
# ---------------------------------------------------------------------------
def _is_test(path: str) -> bool:
    return bool(re.search(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*\.py$|_test\.py$", path))


def _functions(snap: Snapshot) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    for path in snap.code_paths("python"):
        if _is_test(path):
            continue
        tree = snap.parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield path, node


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _rule(rid: str, topic: str, statement: str, good: list[str], bad: list[str], n: int) -> ConventionRule | None:
    if n < 5:
        return None
    adherence = len(good) / n
    return ConventionRule(
        id=rid, topic=topic, statement=statement, provenance="inferred", status="candidate",
        adherence=round(adherence, 3), sample=n, evidence=good[:3], deviations=bad[:3], source="inferred from code",
    )


def _returns_error_value(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant) and k.value == "status" and isinstance(v, ast.Constant) and v.value == "error":
                    return True
                if isinstance(k, ast.Constant) and k.value == "error":
                    return True
    return False


def _quote_counts(snap: Snapshot) -> tuple[int, int]:
    dbl = sgl = 0
    for path in snap.code_paths("python"):
        if _is_test(path):
            continue
        text = snap.read(path) or ""
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type != tokenize.STRING:
                    continue
                body = tok.string.lstrip("rbfuRBFU")
                if body.startswith(('"""', "'''")):
                    continue
                inner = body[1:-1]
                if '"' in inner or "'" in inner:
                    continue  # the author had no choice
                dbl += body.startswith('"')
                sgl += body.startswith("'")
        except (tokenize.TokenError, IndentationError, SyntaxError):
            continue
    return dbl, sgl


def infer_python(snap: Snapshot, declared_ids: set[str]) -> list[ConventionRule]:
    rules: list[ConventionRule] = []

    def loc(path: str, node: ast.AST) -> str:
        return f"{path}:{getattr(node, 'lineno', 1)}"

    fns = [(p, f) for p, f in _functions(snap) if not _is_dunder(f.name)]

    # Type annotations on return values
    good = [loc(p, f) for p, f in fns if f.returns is not None]
    bad = [loc(p, f) for p, f in fns if f.returns is None]
    if "type-checking" not in declared_ids:
        r = _rule("type-annotations", "typing", "Functions declare return type annotations", good, bad, len(fns))
        if r:
            rules.append(r)

    # Docstrings on public functions
    pub = [(p, f) for p, f in fns if not f.name.startswith("_")]
    good = [loc(p, f) for p, f in pub if ast.get_docstring(f)]
    bad = [loc(p, f) for p, f in pub if not ast.get_docstring(f)]
    r = _rule("docstrings", "docs", "Public functions have docstrings", good, bad, len(pub))
    if r:
        rules.append(r)

    # Error signalling: return error values vs raise
    returns_err = [loc(p, f) for p, f in fns if _returns_error_value(f)]
    raises: list[str] = []
    for p, f in fns:
        for node in ast.walk(f):
            if isinstance(node, ast.Raise):
                raises.append(loc(p, node))
    total = len(returns_err) + len(raises)
    if total >= 5:
        if len(returns_err) >= len(raises):
            r = _rule("error-signalling", "error-handling", "Failures are returned as error values (e.g. {\"status\": \"error\"}) instead of raised", returns_err, raises, total)
        else:
            r = _rule("error-signalling", "error-handling", "Failures are raised as exceptions", raises, returns_err, total)
        if r:
            rules.append(r)

    # Logging vs print
    prints: list[str] = []
    logs: list[str] = []
    for path in snap.code_paths("python"):
        if _is_test(path):
            continue
        tree = snap.parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = ast.unparse(node.func)
                if fn == "print":
                    prints.append(loc(path, node))
                elif re.fullmatch(r"(logging|logger|log|LOG)\.(debug|info|warning|warn|error|exception|critical)", fn):
                    logs.append(loc(path, node))
    total = len(prints) + len(logs)
    if total >= 5:
        if len(logs) >= len(prints):
            r = _rule("logging", "logging", "Diagnostics go through the logging module", logs, prints, total)
        else:
            r = _rule("logging", "logging", "Diagnostics are written with print()", prints, logs, total)
        if r:
            rules.append(r)

    # Intra-repo import style
    modules = {".".join(PurePosixPath(p).with_suffix("").parts) for p in snap.code_paths("python")}
    tops = {m.split(".")[0] for m in modules}
    rel, absolute = [], []
    for path in snap.code_paths("python"):
        tree = snap.parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    rel.append(loc(path, node))
                elif node.module and node.module.split(".")[0] in tops:
                    absolute.append(loc(path, node))
    total = len(rel) + len(absolute)
    if total >= 5:
        if len(rel) >= len(absolute):
            r = _rule("import-style", "imports", "Imports between modules of this package are relative", rel, absolute, total)
        else:
            r = _rule("import-style", "imports", "Imports between modules of this package are absolute", absolute, rel, total)
        if r:
            rules.append(r)

    # Quote style
    dbl, sgl = _quote_counts(snap)
    total = dbl + sgl
    if total >= 20:
        rules.append(
            ConventionRule(
                id="quote-style", topic="style",
                statement="String literals use double quotes" if dbl >= sgl else "String literals use single quotes",
                provenance="inferred", status="candidate", adherence=round(max(dbl, sgl) / total, 3), sample=total, source="inferred from code",
            )
        )

    # Naming
    badfn = [loc(p, f) for p, f in fns if not re.fullmatch(r"_{0,2}[a-z][a-z0-9_]*", f.name)]
    goodfn = [loc(p, f) for p, f in fns if re.fullmatch(r"_{0,2}[a-z][a-z0-9_]*", f.name)]
    r = _rule("naming-functions", "naming", "Functions are snake_case", goodfn, badfn, len(fns))
    if r:
        rules.append(r)

    # Line length
    if "line-length" not in declared_ids:
        lengths = [len(line) for p in snap.code_paths("python") if not _is_test(p) for line in snap.lines(p)]
        if len(lengths) >= 50:
            for limit in (79, 88, 100, 120, 140):
                share = sum(1 for n in lengths if n <= limit) / len(lengths)
                if share >= 0.97:
                    rules.append(
                        ConventionRule(
                            id="line-length-observed", topic="style", statement=f"Lines stay within {limit} columns",
                            provenance="inferred", status="candidate", adherence=round(share, 3), sample=len(lengths), source="inferred from code",
                        )
                    )
                    break

    # Test framework
    if any(_is_test(p) for p in snap.files):
        uses_pytest = any("pytest" in (snap.read(p) or "") for p in snap.code_paths("python") if _is_test(p))
        rules.append(
            ConventionRule(
                id="test-framework", topic="testing", statement="Tests use pytest" if uses_pytest else "Tests use unittest",
                provenance="inferred", status="candidate", adherence=1.0, sample=1, source="inferred from code",
            )
        )

    return [r for r in rules if r.adherence is None or r.adherence >= 0.4]
