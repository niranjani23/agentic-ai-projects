"""Command line: `cra review | lens-prompt | post | baseline`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .api import resolve_source, run_review
from .github.poster import GhError, build_review_payload, create_baseline_branches, post_pr_review
from .lenses.catalog import ALL_LENSES, plan_lenses, render_system_prompt
from .lenses.context import LensContext, render_context
from .models import Review, ReviewOptions
from .scout.snapshot import load_snapshot
from .scout.stack import detect_stack
from .scout.static_checks import run_static_checks
from .scout.topology import detect_topology


def _cmd_review(a: argparse.Namespace) -> int:
    options = ReviewOptions(
        source=a.source, mode="pr" if a.base else "audit", base=a.base, out_dir=a.out, runner=a.runner,
        replay_dir=a.replay_dir, gate=a.gate, answers_file=a.answers, profile_path=a.profile, model=a.model,
        lenses=a.lenses.split(",") if a.lenses else None, max_inline=a.max_inline, min_confidence=a.min_confidence,
        mine_history=a.mine_history,
    )
    review = run_review(options)
    counts = {s: sum(1 for f in review.findings if f.severity == s) for s in ("critical", "high", "medium", "low", "nit")}
    print(f"\n{review.verdict}")
    print("  " + " · ".join(f"{n} {s}" for s, n in counts.items()) + f"   (dropped during verification: {len(review.dropped)})")
    print(f"  outputs in {options.out_dir}/: review.md, review.json, convention_profile.json, pr_review_payload.json")

    if a.pr:
        result = post_pr_review(review, a.pr, post=a.post)
        Path(options.out_dir, "pr_review_payload.json").write_text(json.dumps(result["payload"], indent=2, ensure_ascii=False))
        n = len(result["payload"]["comments"])
        print(f"  PR {a.pr}: payload re-mapped onto the PR diff, {n} inline comment(s)")
        print("  POSTED." if result["posted"] else "  dry run: nothing was posted (add --post to submit).")
    elif a.post:
        print("  --post needs --pr owner/repo#N", file=sys.stderr)
        return 2
    return 0


def _cmd_lens_prompt(a: argparse.Namespace) -> int:
    if a.lens not in ALL_LENSES:
        print(f"unknown lens {a.lens!r}; choose from: {', '.join(ALL_LENSES)}", file=sys.stderr)
        return 2
    root = resolve_source(a.source, ".cra-work")
    snap = load_snapshot(root)
    stack, topo = detect_stack(snap), detect_topology(snap)
    from .conventions.gate import build_profile

    opts = ReviewOptions(source=root)
    ctx = LensContext(ALL_LENSES[a.lens], snap, stack, topo, run_static_checks(snap, stack, topo), build_profile(snap, opts), opts)
    text, included = render_context(ctx)
    print(render_system_prompt(ALL_LENSES[a.lens], opts.max_tool_calls))
    print(f"\n--- context pack: {len(text):,} chars, {len(included)} file(s) included ---")
    if a.full:
        print(text)
    return 0


def _cmd_post(a: argparse.Namespace) -> int:
    review = Review.model_validate_json(Path(a.review_json).read_text())
    result = post_pr_review(review, a.pr, post=a.yes)
    Path(a.review_json).with_name("pr_review_payload.json").write_text(json.dumps(result["payload"], indent=2, ensure_ascii=False))
    print(f"{len(result['payload']['comments'])} inline comment(s); " + ("POSTED" if result["posted"] else "dry run (add --yes to post)"))
    return 0


def _cmd_baseline(a: argparse.Namespace) -> int:
    info = create_baseline_branches(a.repo_dir, a.base, a.head)
    print(json.dumps(info, indent=2))
    print(f"\nNext (you run these; nothing was pushed):\n  git push origin {info['base']} {info['head']}\n"
          f"  gh pr create --draft --base {info['base']} --head {info['head']} --title 'Full-repo review (do not merge)'")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cra", description="Convention-aware multi-agent code review.")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("review", help="review a repo (path, owner/repo, or URL)")
    r.add_argument("source")
    r.add_argument("--out", default="review-out")
    r.add_argument("--base", help="PR mode: only changed files since this ref get inline comments")
    r.add_argument("--runner", choices=["anthropic", "replay"], default="anthropic")
    r.add_argument("--replay-dir", help="directory of <lens>.json recorded findings (runner=replay)")
    r.add_argument("--gate", choices=["interactive", "answers", "auto"], default="auto")
    r.add_argument("--answers", help="YAML/JSON answers file for --gate answers")
    r.add_argument("--profile", help="path to cache the convention profile between runs")
    r.add_argument("--model", default="claude-sonnet-5-5")
    r.add_argument("--lenses", help="comma-separated subset of lenses")
    r.add_argument("--max-inline", type=int, default=12)
    r.add_argument("--min-confidence", type=float, default=0.5)
    r.add_argument("--mine-history", metavar="OWNER/REPO", help="learn conventions from merged-PR review comments")
    r.add_argument("--pr", metavar="OWNER/REPO#N", help="map inline comments onto this PR's diff")
    r.add_argument("--post", action="store_true", help="actually submit the review to --pr (default is a dry run)")
    r.set_defaults(fn=_cmd_review)

    lp = sub.add_parser("lens-prompt", help="print a lens's system prompt and context-pack size")
    lp.add_argument("lens")
    lp.add_argument("source")
    lp.add_argument("--full", action="store_true", help="also print the whole context pack")
    lp.set_defaults(fn=_cmd_lens_prompt)

    po = sub.add_parser("post", help="post a saved review.json to a PR")
    po.add_argument("review_json")
    po.add_argument("pr", metavar="OWNER/REPO#N")
    po.add_argument("--yes", action="store_true", help="really post (default is a dry run)")
    po.set_defaults(fn=_cmd_post)

    b = sub.add_parser("baseline", help="create local base/head branches so a repo with no PR can be reviewed")
    b.add_argument("repo_dir")
    b.add_argument("--base", default="review-baseline")
    b.add_argument("--head", default="review-full")
    b.set_defaults(fn=_cmd_baseline)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (GhError, FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
