"""Read-only tools handed to lens agents.

The repo under review is untrusted input, so these tools are deliberately weak:
read-only, confined to tracked files inside the repo root, size-capped, and they never
return secret values (sensitive files are refused; other content is redacted).
"""

from __future__ import annotations

import re

from ..scout.redact import is_sensitive_path, redact_secrets
from ..scout.snapshot import Snapshot

MAX_LINES_PER_READ = 400
MAX_GREP_MATCHES = 50

READ_FILE_TOOL = {
    "name": "read_file",
    "description": "Read lines from a tracked file in the repository. Returns numbered lines. Max 400 lines per call.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repo-relative path, e.g. src/app.py"},
            "start_line": {"type": "integer", "description": "1-indexed first line (default 1)"},
            "end_line": {"type": "integer", "description": "1-indexed last line (default start+399)"},
        },
        "required": ["path"],
    },
}

GREP_TOOL = {
    "name": "grep",
    "description": "Search tracked text files with a Python regular expression. Returns up to 50 matching lines as path:line: text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "glob": {"type": "string", "description": "Optional fnmatch filter on the path, e.g. *.py"},
        },
        "required": ["pattern"],
    },
}


class RepoTools:
    def __init__(self, snap: Snapshot) -> None:
        self.snap = snap

    def _allowed(self, path: str) -> str | None:
        """Return an error string if the path may not be read, else None."""
        if ".." in path.replace("\\", "/").split("/") or path.startswith(("/", "~")):
            return f"error: path must be repo-relative and inside the repository: {path}"
        if path not in self.snap.files:
            return f"error: not a tracked file: {path}"
        if is_sensitive_path(path):
            return f"error: {path} may contain secrets and is not readable"
        if self.snap.read(path) is None:
            return f"error: {path} is binary or too large to read"
        return None

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        if err := self._allowed(path):
            return err
        lines = self.snap.lines(path)
        start = max(1, int(start_line or 1))
        end = min(len(lines), int(end_line) if end_line else start + MAX_LINES_PER_READ - 1, start + MAX_LINES_PER_READ - 1)
        if start > len(lines):
            return f"error: {path} has only {len(lines)} lines"
        body = "\n".join(f"{i:>5}| {redact_secrets(lines[i - 1])}" for i in range(start, end + 1))
        return f"{path} (lines {start}-{end} of {len(lines)})\n{body}"

    def grep(self, pattern: str, glob: str | None = None) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"error: invalid regex: {exc}"
        out: list[str] = []
        allowed = set(self.snap.match(glob)) if glob else None
        for path, info in sorted(self.snap.files.items()):
            if not info.is_text or is_sensitive_path(path) or (allowed is not None and path not in allowed):
                continue
            for i, line in enumerate(self.snap.lines(path), start=1):
                if rx.search(line):
                    out.append(f"{path}:{i}: {redact_secrets(line).strip()[:200]}")
                    if len(out) >= MAX_GREP_MATCHES:
                        return "\n".join(out) + f"\n(stopped at {MAX_GREP_MATCHES} matches)"
        return "\n".join(out) or "(no matches)"

    def dispatch(self, name: str, args: dict) -> str:
        if name == "read_file":
            return self.read_file(args.get("path", ""), args.get("start_line", 1), args.get("end_line"))
        if name == "grep":
            return self.grep(args.get("pattern", ""), args.get("glob"))
        return f"error: unknown tool {name}"
