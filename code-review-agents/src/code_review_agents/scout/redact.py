"""Secret detection and redaction.

Two jobs, both safety-critical:
  1. Find secrets that were committed so we can report them (by location, never by value).
  2. Make sure no secret value is ever placed in a model prompt, a log line or a review doc.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
]

_PLACEHOLDER_HINTS = ("your", "xxx", "changeme", "change-me", "example", "placeholder", "dummy", "<", "...", "todo", "replace")

SENSITIVE_BASENAMES = {"id_rsa", "id_ed25519", "credentials.json", "service-account.json", ".npmrc", ".pypirc"}
ENV_EXAMPLE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".tmpl")


def is_env_file(path: str) -> bool:
    name = PurePosixPath(path).name
    if not (name == ".env" or name.startswith(".env.") or name.endswith(".env")):
        return False
    return not name.endswith(ENV_EXAMPLE_SUFFIXES)


def is_sensitive_path(path: str) -> bool:
    """Files whose *contents* must never be sent to a model or echoed."""
    p = PurePosixPath(path)
    return is_env_file(path) or p.name in SENSITIVE_BASENAMES or p.suffix in {".pem", ".key", ".p12", ".pfx"}


def is_placeholder(value: str) -> bool:
    v = value.strip().strip("'\"").lower()
    if not v:
        return True
    if any(h in v for h in _PLACEHOLDER_HINTS):
        return True
    return len(set(v)) <= 2  # "aaaa", "0000"


def find_secrets(text: str) -> list[tuple[str, int, int]]:
    """Return (kind, start, end) for every real-looking secret in `text`."""
    hits: list[tuple[str, int, int]] = []
    for kind, pat in SECRET_PATTERNS:
        for m in pat.finditer(text):
            if not is_placeholder(m.group(0)):
                hits.append((kind, m.start(), m.end()))
    return hits


def redact_secrets(text: str) -> str:
    """Replace secret-looking spans so the text is safe to put in a prompt."""
    spans = sorted(find_secrets(text), key=lambda h: h[1])
    if not spans:
        return text
    out: list[str] = []
    cursor = 0
    for kind, start, end in spans:
        if start < cursor:
            continue
        out.append(text[cursor:start])
        out.append(f"<redacted:{kind}>")
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def parse_env_lines(text: str) -> list[tuple[int, str, str]]:
    """(line_number, key, value) for KEY=VALUE lines; comments and blanks skipped."""
    rows: list[tuple[int, str, str]] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            rows.append((i, key, value.strip()))
    return rows
