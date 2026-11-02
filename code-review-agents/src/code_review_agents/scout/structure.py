"""Deterministic structure metrics for Python code.

The design lenses (patterns, extensibility, maintainability) are only as good as their evidence. "This module is
too coupled" is an opinion; "this module imports 6 internal modules and 0 import it" is a fact a reader can check.
So the scout measures, and the lenses interpret:

  * per function: length, cyclomatic complexity, parameter count, nesting depth
  * per class:    method count
  * per module:   internal import graph -> fan-in, fan-out, import cycles

Only import cycles become a finding on their own (they are defects in any codebase). Everything else is context
handed to the lenses and printed in the review's "structure at a glance" section.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field

from ..models import Evidence, Finding
from .pyimports import _module_name, _resolve_imports
from .snapshot import Snapshot

_DECISIONS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.Assert, ast.comprehension)
_NESTERS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)

# Rough, widely used thresholds; they mark functions worth a look, not defects.
LONG_FUNCTION = 60
COMPLEX_FUNCTION = 10
MANY_PARAMS = 5
DEEP_NESTING = 4
BIG_CLASS = 15


@dataclass
class FunctionMetric:
    file: str
    line: int
    name: str
    lines: int
    complexity: int
    params: int
    nesting: int

    def hot(self) -> bool:
        return (self.lines >= LONG_FUNCTION or self.complexity >= COMPLEX_FUNCTION
                or self.params >= MANY_PARAMS or self.nesting >= DEEP_NESTING)


@dataclass
class ModuleMetric:
    file: str
    lines: int
    functions: int
    classes: int
    fan_out: list[str] = field(default_factory=list)  # internal modules this one imports
    fan_in: list[str] = field(default_factory=list)   # internal modules that import this one


@dataclass
class StructureReport:
    functions: list[FunctionMetric] = field(default_factory=list)
    modules: list[ModuleMetric] = field(default_factory=list)
    big_classes: list[tuple[str, int, str, int]] = field(default_factory=list)  # file, line, name, methods
    cycles: list[list[str]] = field(default_factory=list)

    def hot_functions(self, n: int = 10) -> list[FunctionMetric]:
        hot = [f for f in self.functions if f.hot()]
        return sorted(hot, key=lambda f: (-f.complexity, -f.lines, f.file, f.line))[:n]

    def summary(self) -> dict:
        fns = self.functions
        return {
            "python_functions": len(fns),
            "python_modules": len(self.modules),
            "hot_functions": len([f for f in fns if f.hot()]),
            "max_function_lines": max((f.lines for f in fns), default=0),
            "max_complexity": max((f.complexity for f in fns), default=0),
            "import_cycles": len(self.cycles),
        }

    def to_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "hot_functions": [asdict(f) for f in self.hot_functions(15)],
            "modules": [asdict(m) for m in sorted(self.modules, key=lambda m: (-len(m.fan_in) - len(m.fan_out), m.file))],
            "big_classes": [{"file": f, "line": ln, "name": n, "methods": c} for f, ln, n, c in self.big_classes],
            "cycles": self.cycles,
            "thresholds": {"long_function": LONG_FUNCTION, "complex_function": COMPLEX_FUNCTION,
                           "many_params": MANY_PARAMS, "deep_nesting": DEEP_NESTING, "big_class": BIG_CLASS},
        }


def _complexity(fn: ast.AST) -> int:
    score = 1
    for node in ast.walk(fn):
        if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue  # nested functions are measured on their own (approximation: their branches still count once)
        if isinstance(node, _DECISIONS):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        elif isinstance(node, ast.match_case):
            score += 1
    return score


def _nesting(node: ast.AST, depth: int = 0) -> int:
    best = depth
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        best = max(best, _nesting(child, depth + 1 if isinstance(child, _NESTERS) else depth))
    return best


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    a = fn.args
    names = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    return len([n for n in names if n not in {"self", "cls"}])


def _sccs(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components; returns cycles (size > 1, or a self-import)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = [0]

    def visit(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or v in graph.get(v, ()):
                out.append(sorted(comp))

    for v in sorted(graph):
        if v not in index:
            visit(v)
    return sorted(out)


def compute_structure(snap: Snapshot) -> StructureReport:
    cached = getattr(snap, "_structure", None)
    if cached is not None:
        return cached

    report = StructureReport()
    py = [p for p in snap.code_paths("python")]
    index = {_module_name(p): p for p in py}
    graph: dict[str, set[str]] = {p: set() for p in py}

    for path in py:
        tree = snap.parse(path)
        if tree is None:
            continue
        n_fn = n_cls = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                n_fn += 1
                end = getattr(node, "end_lineno", node.lineno) or node.lineno
                report.functions.append(FunctionMetric(
                    file=path, line=node.lineno, name=node.name, lines=end - node.lineno + 1,
                    complexity=_complexity(node), params=_params(node), nesting=_nesting(node),
                ))
            elif isinstance(node, ast.ClassDef):
                n_cls += 1
                methods = sum(isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) for b in node.body)
                if methods >= BIG_CLASS:
                    report.big_classes.append((path, node.lineno, node.name, methods))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                targets = [t for t in _resolve_imports(snap, index, path, node) if t != path]
                specific = [t for t in targets if not t.endswith("__init__.py")]
                graph[path].update(specific or targets)  # prefer the submodule over its package __init__
        report.modules.append(ModuleMetric(file=path, lines=snap.files[path].lines, functions=n_fn, classes=n_cls))

    by_file = {m.file: m for m in report.modules}
    for src, dsts in graph.items():
        if src in by_file:
            by_file[src].fan_out = sorted(dsts)
        for d in dsts:
            if d in by_file:
                by_file[d].fan_in.append(src)
    for m in report.modules:
        m.fan_in.sort()
    report.cycles = _sccs(graph)
    report.functions.sort(key=lambda f: (f.file, f.line))
    snap._structure = report  # type: ignore[attr-defined]
    return report


def render_structure(report: StructureReport, limit: int = 12) -> str:
    """Compact text for the lens context pack."""
    s = report.summary()
    rows = [
        f"{s['python_functions']} functions in {s['python_modules']} modules; {s['hot_functions']} exceed a threshold "
        f"(lines>={LONG_FUNCTION}, complexity>={COMPLEX_FUNCTION}, params>={MANY_PARAMS}, nesting>={DEEP_NESTING}).",
        "Hot functions (file:line name - lines / complexity / params / nesting):",
    ]
    rows += [f"  {f.file}:{f.line} {f.name} - {f.lines} / {f.complexity} / {f.params} / {f.nesting}" for f in report.hot_functions(limit)] or ["  (none)"]
    coupled = sorted(report.modules, key=lambda m: (-(len(m.fan_in) + len(m.fan_out)), m.file))[:limit]
    rows.append("Internal coupling (file - imported by N / imports M):")
    rows += [f"  {m.file} - {len(m.fan_in)} / {len(m.fan_out)}" for m in coupled if m.fan_in or m.fan_out] or ["  (no internal imports)"]
    rows.append("Import cycles: " + ("; ".join(" -> ".join(c) for c in report.cycles) if report.cycles else "none"))
    if report.big_classes:
        rows.append("Classes with many methods: " + "; ".join(f"{f}:{ln} {n} ({c})" for f, ln, n, c in report.big_classes))
    return "\n".join(rows)


def check_import_cycles(snap: Snapshot, stack=None) -> list[Finding]:
    report = compute_structure(snap)
    findings = []
    for cycle in report.cycles:
        ev = []
        for path in cycle[:3]:
            tree = snap.parse(path)
            nxt = next((c for c in cycle if c != path), None)
            line = None
            if tree is not None and nxt:
                index = {_module_name(p): p for p in snap.code_paths("python")}
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Import, ast.ImportFrom)) and nxt in _resolve_imports(snap, index, path, node):
                        line = node.lineno
                        break
            if line:
                ev.append(Evidence(file=path, line=line, quote=(snap.line(path, line) or "").strip()[:140]))
        if not ev:
            continue
        findings.append(Finding(
            lens="extensibility", severity="medium", confidence=0.9, origin="static",
            title=f"Import cycle between {len(cycle)} modules",
            claim="These modules import each other, directly or transitively: " + " -> ".join(f"`{c}`" for c in cycle) + ".",
            why_it_matters="Cycles make import order matter (partially initialised modules), block reuse of either side alone, "
                           "and usually mean a responsibility sits in the wrong module.",
            suggested_fix="Move the shared piece into a lower-level module both can import, or invert the dependency with an interface or callback.",
            evidence=ev, dedupe_key="import-cycle:" + "|".join(cycle),
        ))
    return findings
