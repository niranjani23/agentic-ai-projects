"""Mine merged-PR review comments for conventions humans actually enforce.

What reviewers repeatedly nitpicked *is* the convention; what they keep dismissing is
an anti-rule. This is the richest and most-skipped source, so it is a first-class
input, but it degrades gracefully: no network, no PRs or no token just means "no
history", reported as such in the review.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable

from ..models import ConventionRule
from .harvest import TOPIC_PATTERNS, slug

HttpGet = Callable[[str], list | dict]

MIN_COMMENTS = 3
MIN_PRS = 2


def _default_get(token: str | None) -> HttpGet:
    def get(url: str):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "code-review-agents"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as resp:
            return json.load(resp)

    return get


def mine_review_history(
    repo_slug: str, token: str | None = None, limit: int = 30, http_get: HttpGet | None = None
) -> tuple[list[ConventionRule], dict]:
    get = http_get or _default_get(token)
    api = f"https://api.github.com/repos/{repo_slug}"
    try:
        pulls = get(f"{api}/pulls?state=closed&per_page={limit}&sort=updated&direction=desc")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return [], {"prs_scanned": 0, "error": f"{type(exc).__name__}: {exc}"}

    merged = [p for p in pulls if isinstance(p, dict) and p.get("merged_at")]
    by_topic: dict[str, list[tuple[int, str]]] = defaultdict(list)
    comments_seen = 0
    for pr in merged:
        n = pr["number"]
        try:
            comments = get(f"{api}/pulls/{n}/comments?per_page=100")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        for c in comments:
            body = (c.get("body") or "").strip()
            if len(body) < 15 or (c.get("user") or {}).get("type") == "Bot":
                continue
            comments_seen += 1
            for topic, pat in TOPIC_PATTERNS.items():
                if pat.search(body):
                    by_topic[topic].append((n, c.get("path") or ""))
                    break

    rules: list[ConventionRule] = []
    for topic, items in sorted(by_topic.items()):
        prs = {n for n, _ in items}
        if len(items) >= MIN_COMMENTS and len(prs) >= MIN_PRS:
            rules.append(
                ConventionRule(
                    id=f"mined-{slug(topic)}",
                    topic=topic,
                    statement=f"Reviewers repeatedly raise {topic} ({len(items)} comments across {len(prs)} merged PRs)",
                    provenance="mined",
                    status="candidate",
                    sample=len(items),
                    evidence=[f"PR#{n}:{path}" for n, path in items[:3]],
                    source=f"review history of {repo_slug}",
                )
            )
    summary = {"prs_scanned": len(merged), "comments_scanned": comments_seen, "topics": {k: len(v) for k, v in by_topic.items()}}
    if not merged:
        summary["note"] = "no merged PRs with review comments to learn from"
    return rules, summary
