"""Shared helpers for resolving intra-repo Python imports."""

from __future__ import annotations

import ast
from pathlib import PurePosixPath

from .snapshot import Snapshot


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_imports(snap: Snapshot, index: dict[str, str], path: str, node: ast.stmt) -> list[str]:
    cur = _module_name(path)
    is_pkg = path.endswith("__init__.py")
    candidates: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            candidates.append(alias.name)
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            pkg_parts = (cur if is_pkg else cur.rpartition(".")[0]).split(".") if cur else []
            pkg_parts = [p for p in pkg_parts if p]
            pkg_parts = pkg_parts[: len(pkg_parts) - (node.level - 1)] if node.level > 1 else pkg_parts
            base = ".".join(pkg_parts + ([node.module] if node.module else []))
        else:
            base = node.module or ""
        if base:
            candidates.append(base)
        for alias in node.names:
            candidates.append(f"{base}.{alias.name}" if base else alias.name)
    return [index[c] for c in candidates if c in index]
