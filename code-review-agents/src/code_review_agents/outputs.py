"""Write the review artifacts to disk."""

from __future__ import annotations

import json
from pathlib import Path

from .github.poster import build_review_payload
from .models import Review
from .render.markdown import render_markdown


def write_outputs(review: Review, out_dir: str | Path) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "review.md": out / "review.md",
        "review.json": out / "review.json",
        "convention_profile.json": out / "convention_profile.json",
        "pr_review_payload.json": out / "pr_review_payload.json",
    }
    paths["review.md"].write_text(render_markdown(review), encoding="utf-8")
    paths["review.json"].write_text(review.model_dump_json(indent=2), encoding="utf-8")
    paths["convention_profile.json"].write_text(review.profile.model_dump_json(indent=2), encoding="utf-8")
    # Dry-run payload: anchors are trusted here. `cra review --pr` / `cra post` re-map it onto the real PR diff.
    payload = build_review_payload(review, commit_id=review.head_sha, commentable=None)
    paths["pr_review_payload.json"].write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}
