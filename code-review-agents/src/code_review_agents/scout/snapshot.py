"""A read-only view of the repo under review.

All later stages read through the Snapshot so that (a) only tracked files are
considered, (b) paths are confined to the repo root, and (c) file reads are cached.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".tox", ".idea", ".next",
}
MAX_READ_BYTES = 1_000_000

LANG_BY_EXT = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".mjs": "javascript", ".go": "go", ".rs": "rust", ".java": "java",
    ".kt": "kotlin", ".rb": "ruby", ".cs": "csharp", ".cpp": "cpp", ".cc": "cpp", ".c": "c",
    ".h": "c", ".sh": "shell", ".sql": "sql", ".md": "markdown", ".rst": "markdown",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml", ".txt": "text",
    ".ipynb": "notebook",
}
CODE_LANGS = {
    "python", "typescript", "javascript", "go", "rust", "java", "kotlin", "ruby",
    "csharp", "cpp", "c", "shell", "sql",
}


@dataclass
class FileInfo:
    path: str
    size: int
    lang: str
    is_text: bool
    lines: int


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60, check=True
        )
        return out.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def is_vendored_or_generated(path: str) -> bool:
    parts = set(PurePosixPath(path).parts)
    return bool(parts & SKIP_DIRS) or path.endswith(".pyc")


class Snapshot:
    def __init__(
        self,
        root: Path,
        files: dict[str, FileInfo],
        is_git: bool,
        head_sha: str | None,
        remote: str | None,
        changed: set[str] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.files = files
        self.is_git = is_git
        self.head_sha = head_sha
        self.remote = remote
        self.changed = changed
        self._text: dict[str, str | None] = {}
        self._ast: dict[str, ast.Module | None] = {}

    # -- paths ---------------------------------------------------------------
    def resolve(self, rel: str) -> Path | None:
        """Absolute path for a repo-relative path, or None if it escapes the root."""
        candidate = (self.root / rel).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate

    def exists(self, path: str) -> bool:
        return path in self.files

    def match(self, pattern: str) -> list[str]:
        return sorted(p for p in self.files if fnmatch.fnmatch(p, pattern))

    def code_paths(self, lang: str | None = None) -> list[str]:
        out = []
        for p, info in self.files.items():
            if is_vendored_or_generated(p) or not info.is_text:
                continue
            if info.lang in CODE_LANGS and (lang is None or info.lang == lang):
                out.append(p)
        return sorted(out)

    # -- content -------------------------------------------------------------
    def read(self, path: str) -> str | None:
        if path in self._text:
            return self._text[path]
        text: str | None = None
        info = self.files.get(path)
        abs_path = self.resolve(path)
        if info and info.is_text and abs_path and info.size <= MAX_READ_BYTES:
            try:
                text = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = None
        self._text[path] = text
        return text

    def lines(self, path: str) -> list[str]:
        text = self.read(path)
        return text.splitlines() if text is not None else []

    def line(self, path: str, n: int) -> str | None:
        ls = self.lines(path)
        return ls[n - 1] if 1 <= n <= len(ls) else None

    def parse(self, path: str) -> ast.Module | None:
        if path not in self._ast:
            tree = None
            text = self.read(path)
            if text is not None and path.endswith(".py"):
                try:
                    tree = ast.parse(text)
                except (SyntaxError, ValueError):
                    tree = None
            self._ast[path] = tree
        return self._ast[path]


def _is_text(abs_path: Path) -> bool:
    try:
        with abs_path.open("rb") as fh:
            return b"\0" not in fh.read(8192)
    except OSError:
        return False


def load_snapshot(root: str | Path, base: str | None = None) -> Snapshot:
    root = Path(root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"not a directory: {root}")

    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    is_git = bool(inside and inside.strip() == "true")

    paths: list[str]
    head_sha = remote = None
    if is_git:
        listing = _git(root, "ls-files", "-z") or ""
        paths = [p for p in listing.split("\0") if p]
        head_sha = (_git(root, "rev-parse", "HEAD") or "").strip() or None
        remote = (_git(root, "remote", "get-url", "origin") or "").strip() or None
        if remote:  # CI remotes often embed a token (https://user:token@host/...); never let it reach an output
            remote = re.sub(r"(://)[^/@\s]*@", r"\1", remote)
    else:
        paths = []
        for p in sorted(root.rglob("*")):
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                if not (set(PurePosixPath(rel).parts) & SKIP_DIRS):
                    paths.append(rel)

    files: dict[str, FileInfo] = {}
    for rel in paths:
        abs_path = root / rel
        if not abs_path.is_file():
            continue
        size = abs_path.stat().st_size
        lang = LANG_BY_EXT.get(PurePosixPath(rel).suffix.lower(), "other")
        is_text = _is_text(abs_path)
        lines = 0
        if is_text and size <= MAX_READ_BYTES:
            try:
                lines = abs_path.read_text(encoding="utf-8", errors="replace").count("\n") + 1
            except OSError:
                lines = 0
        files[rel] = FileInfo(rel, size, lang, is_text, lines)

    changed: set[str] | None = None
    if base and is_git:
        diff = _git(root, "diff", "--name-only", f"{base}...HEAD") or _git(root, "diff", "--name-only", base, "HEAD")
        if diff is not None:
            changed = {p for p in diff.splitlines() if p}

    return Snapshot(root, files, is_git, head_sha, remote, changed)
