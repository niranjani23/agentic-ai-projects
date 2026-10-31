"""Turn a Review into a GitHub pull-request review.

Safety properties:
  * Nothing is posted unless the caller passes post=True. The default is a dry run that returns the payload.
  * Inline comments are only attached to lines that are really in the PR diff (GitHub rejects the whole review otherwise).
    Findings outside the diff are listed in the review body instead of being dropped.
  * Uses the `gh` CLI so it inherits the user's auth and proxy settings; the runner is injectable for tests.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..lenses.catalog import ALL_LENSES
from ..models import SEVERITY_ORDER, Finding, Review

Runner = Callable[[list[str], str | None], str]

MAX_BODY = 60_000
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class GhError(RuntimeError):
    pass


def _run_gh(args: list[str], stdin: str | None = None) -> str:
    proc = subprocess.run(["gh", "api", *args], input=stdin, capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        raise GhError((proc.stderr or proc.stdout).strip()[:600])
    return proc.stdout


def parse_commentable_lines(patch: str | None) -> set[int]:
    """Right-side line numbers that a review comment may attach to (added and context lines)."""
    lines: set[int] = set()
    new: int | None = None
    for raw in (patch or "").splitlines():
        m = _HUNK.match(raw)
        if m:
            new = int(m.group(1))
            continue
        if new is None or raw.startswith(("-", "\\")):
            continue
        lines.add(new)
        new += 1
    return lines


def parse_pr_ref(ref: str) -> tuple[str, str, int]:
    """'owner/repo#12' or a PR URL -> (owner, repo, number)."""
    m = re.fullmatch(r"([\w.\-]+)/([\w.\-]+)#(\d+)", ref.strip()) or re.search(r"github\.com/([\w.\-]+)/([\w.\-]+)/pull/(\d+)", ref)
    if not m:
        raise ValueError(f"cannot parse PR reference {ref!r}; use owner/repo#N")
    return m.group(1), m.group(2), int(m.group(3))


class GitHub:
    def __init__(self, runner: Runner | None = None) -> None:
        self.run = runner or _run_gh

    def get(self, path: str):
        return json.loads(self.run([path], None) or "null")

    def pr_head_sha(self, owner: str, repo: str, number: int) -> str:
        return self.get(f"repos/{owner}/{repo}/pulls/{number}")["head"]["sha"]

    def pr_files(self, owner: str, repo: str, number: int) -> dict[str, str | None]:
        out = self.run([f"repos/{owner}/{repo}/pulls/{number}/files?per_page=100", "--paginate", "--slurp"], None)
        pages = json.loads(out or "[]")
        files = [f for page in pages for f in (page if isinstance(page, list) else [page])]
        return {f["filename"]: f.get("patch") for f in files}

    def post_review(self, owner: str, repo: str, number: int, payload: dict) -> dict:
        out = self.run([f"repos/{owner}/{repo}/pulls/{number}/reviews", "--method", "POST", "--input", "-"], json.dumps(payload))
        return json.loads(out or "{}")


def _comment_body(f: Finding) -> str:
    lens = ALL_LENSES[f.lens].title if f.lens in ALL_LENSES else f.lens
    parts = [f"**{f.severity.capitalize()} · {lens}**: {f.title}", "", f.claim.split("\n\n")[0], "", f"**Why it matters:** {f.why_it_matters}"]
    if f.suggested_fix:
        parts += ["", f"**Suggested fix:** {f.suggested_fix}"]
    if f.notes:
        parts += ["", "<sub>" + " · ".join(f.notes) + "</sub>"]
    parts += ["", f"<sub>{f.id} · confidence {f.confidence:.2f} · {'deterministic check' if f.origin == 'static' else 'LLM lens'} · code-review-agents</sub>"]
    return "\n".join(parts)[:MAX_BODY]


def build_review_payload(
    review: Review,
    commit_id: str | None = None,
    commentable: dict[str, set[int]] | None = None,
    report_hint: str = "the full review document (`review.md`)",
) -> dict:
    """GitHub review payload. `commentable` maps path -> diff-visible lines; None means 'trust the anchors' (dry run)."""
    comments: list[dict] = []
    outside: list[Finding] = []
    for f in review.findings:
        if not f.inline:
            continue
        anchor = None
        for ev in f.evidence:
            if ev.kind != "line" or not ev.line:
                continue
            if commentable is None or ev.line in commentable.get(ev.file, set()):
                anchor = ev
                break
        if anchor is None:
            outside.append(f)
            continue
        comments.append({"path": anchor.file, "line": anchor.line, "side": "RIGHT", "body": _comment_body(f)})

    counts = {s: sum(1 for f in review.findings if f.severity == s) for s in SEVERITY_ORDER}
    body = [
        "## Automated review (code-review-agents)",
        "",
        f"**{review.verdict}**",
        "",
        " · ".join(f"{counts[s]} {s}" for s in SEVERITY_ORDER),
        "",
    ]
    if review.objective_statement:
        body += [f"**What this is trying to do.** {review.objective_statement}", ""]
    top = [f for f in review.findings if f.severity in {"critical", "high"}][:6]
    if top:
        body += ["| ID | Severity | Finding |", "|---|---|---|"]
        body += [f"| {f.id} | {f.severity} | {f.title.replace('|', '/')} |" for f in top]
        body.append("")
    if outside:
        body += ["### Findings that could not be attached inline", "", "These are not on lines changed in this PR, so they are listed here:", ""]
        body += [f"- **{f.id}** ({f.severity}) {f.title}: `{f.primary().file}:{f.primary().line}`" for f in outside]
        body.append("")
    rag = review.lens_status.get("rag-retrieval", {})
    if rag.get("status") == "skipped":
        body += ["_RAG lenses skipped: no RAG pipeline detected._", ""]
    body += [f"See {report_hint} for all lenses, the conventions applied, and open questions for the team."]

    payload = {"event": "COMMENT", "body": "\n".join(body)[:MAX_BODY], "comments": comments}
    if commit_id:
        payload["commit_id"] = commit_id
    return payload


def post_pr_review(
    review: Review,
    pr_ref: str,
    post: bool = False,
    gh: GitHub | None = None,
    report_hint: str = "the full review document (`review.md`)",
) -> dict:
    """Build (and, only if post=True, submit) a review. Returns {'payload', 'posted', 'response'}."""
    owner, repo, number = parse_pr_ref(pr_ref)
    gh = gh or GitHub()
    sha = gh.pr_head_sha(owner, repo, number)
    commentable = {path: parse_commentable_lines(patch) for path, patch in gh.pr_files(owner, repo, number).items()}
    payload = build_review_payload(review, commit_id=sha, commentable=commentable, report_hint=report_hint)
    if not post:
        return {"payload": payload, "posted": False, "response": None}
    return {"payload": payload, "posted": True, "response": gh.post_review(owner, repo, number, payload)}


# ---------------------------------------------------------------------------
# Review a repo that has no PR: a baseline branch makes the whole tree "changed".
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise GhError(f"git {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def create_baseline_branches(repo: str | Path, base: str = "review-baseline", head: str = "review-full") -> dict:
    """Create two LOCAL branches that make a repo with no PR reviewable.

    GitHub draws a PR diff from the merge-base, so a deletion commit on the base side would never show
    up, and GitHub refuses to compare unrelated histories. The shape that works:

        base  = E                      (one empty commit)
        head  = E + snapshot commit    (adds every file of the source commit)

    A PR head -> base then shows every file as added, so inline comments can anchor on any line, and the
    head tree is identical to the source commit. Nothing is pushed here; the caller decides.
    """
    repo = Path(repo)
    if _git(repo, "status", "--porcelain"):
        raise GhError("working tree is not clean; refusing to create review branches")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    start = _git(repo, "rev-parse", "HEAD") if branch == "HEAD" else branch
    src = _git(repo, "rev-parse", "HEAD")
    ident = ["-c", "user.name=code-review-agents", "-c", "user.email=cra@users.noreply.github.com"]
    try:
        _git(repo, "checkout", "-q", "--orphan", base)
        _git(repo, "rm", "-rqf", ".")
        _git(repo, *ident, "commit", "-q", "--allow-empty", "-m", "review baseline: empty tree (do not merge)")
        base_sha = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "-b", head)
        _git(repo, "checkout", "-q", src, "--", ".")
        _git(repo, *ident, "commit", "-q", "-m", f"review snapshot of {src[:7]} (do not merge)")
        head_sha = _git(repo, "rev-parse", "HEAD")
    finally:
        _git(repo, "checkout", "-q", "-f", start)
    return {"base": base, "head": head, "base_sha": base_sha, "head_sha": head_sha, "source_sha": src}
