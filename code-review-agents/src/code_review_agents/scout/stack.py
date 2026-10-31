"""Stack detection: languages, frameworks, entry points and repo-level hygiene signals."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

from .snapshot import Snapshot, is_vendored_or_generated

FRAMEWORK_BY_IMPORT = {
    "anthropic": "anthropic", "openai": "openai", "langchain": "langchain",
    "langchain_core": "langchain", "langgraph": "langgraph", "llama_index": "llamaindex",
    "crewai": "crewai", "autogen": "autogen", "streamlit": "streamlit", "fastapi": "fastapi",
    "flask": "flask", "django": "django", "gradio": "gradio", "pandas": "pandas",
    "numpy": "numpy", "torch": "torch", "transformers": "transformers", "sqlalchemy": "sqlalchemy",
    "pydantic": "pydantic", "pytest": "pytest", "reportlab": "reportlab",
    "sentence_transformers": "sentence-transformers", "faiss": "faiss", "chromadb": "chromadb",
    "pgvector": "pgvector", "boto3": "aws-sdk", "google": "google-sdk",
}
WEB_SERVING = {"streamlit", "fastapi", "flask", "django", "gradio"}

_ENTRY_NAMES = {"main.py", "app.py", "cli.py", "manage.py", "server.py", "wsgi.py", "asgi.py", "index.ts", "index.js", "server.ts"}
_TEST_RE = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.[jt]sx?$")
_CI_RE = re.compile(r"^\.github/workflows/|^\.gitlab-ci\.yml$|^Jenkinsfile$|^\.circleci/|^azure-pipelines\.yml$|^\.travis\.yml$|^bitbucket-pipelines\.yml$")


@dataclass
class StackInfo:
    languages: dict[str, int] = field(default_factory=dict)
    frameworks: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    has_tests: bool = False
    has_ci: bool = False
    has_gitignore: bool = False
    readmes: list[str] = field(default_factory=list)
    web_serving: list[str] = field(default_factory=list)
    total_files: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _has_main_guard(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            left = node.test.left
            if isinstance(left, ast.Name) and left.id == "__name__":
                return True
    return False


def _deps(snap: Snapshot) -> set[str]:
    deps: set[str] = set()
    for path in snap.files:
        name = PurePosixPath(path).name
        if is_vendored_or_generated(path):
            continue
        text = snap.read(path) or ""
        if re.fullmatch(r"requirements[^/]*\.txt", name):
            for line in text.splitlines():
                m = re.match(r"\s*([A-Za-z0-9_.\-]+)", line)
                if m and not line.strip().startswith(("#", "-")):
                    deps.add(m.group(1).lower().replace("_", "-"))
        elif name == "pyproject.toml":
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            project = data.get("project", {})
            for spec in project.get("dependencies", []):
                m = re.match(r"\s*([A-Za-z0-9_.\-]+)", spec)
                if m:
                    deps.add(m.group(1).lower().replace("_", "-"))
        elif name == "package.json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            for key in ("dependencies", "devDependencies"):
                deps.update(k.lower() for k in data.get(key, {}))
    return deps


def detect_stack(snap: Snapshot) -> StackInfo:
    info = StackInfo(total_files=len(snap.files))

    for path, f in snap.files.items():
        if is_vendored_or_generated(path) or not f.is_text:
            continue
        info.languages[f.lang] = info.languages.get(f.lang, 0) + f.lines
        if _TEST_RE.search(path):
            info.has_tests = True
        if _CI_RE.search(path):
            info.has_ci = True
        if path == ".gitignore":
            info.has_gitignore = True
        if PurePosixPath(path).name.lower().startswith("readme"):
            info.readmes.append(path)

    frameworks: set[str] = set()
    entry: list[str] = []
    for path in snap.code_paths("python"):
        tree = snap.parse(path)
        if tree is None:
            continue
        for mod in _imports(tree):
            if mod in FRAMEWORK_BY_IMPORT:
                frameworks.add(FRAMEWORK_BY_IMPORT[mod])
        if _has_main_guard(tree) or PurePosixPath(path).name in _ENTRY_NAMES:
            entry.append(path)
    for path in snap.code_paths():
        if not path.endswith(".py") and PurePosixPath(path).name in _ENTRY_NAMES:
            entry.append(path)

    deps = _deps(snap)
    for dep in deps:
        key = dep.replace("-", "_")
        if key in FRAMEWORK_BY_IMPORT:
            frameworks.add(FRAMEWORK_BY_IMPORT[key])

    info.frameworks = sorted(frameworks)
    info.deps = sorted(deps)
    info.entrypoints = sorted(set(entry))
    info.web_serving = sorted(frameworks & WEB_SERVING)
    return info
