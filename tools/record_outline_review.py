"""Record a reviewer's outline verdict into plan.json.

Normal file tool, not an LLM call: the reviewer agent writes a small review
JSON and runs this helper so it does not have to rewrite the whole plan.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--review")
    src.add_argument("--review-stdin", action="store_true")
    args = ap.parse_args()

    plan_path = Path(args.plan)
    if args.review_stdin:
        text = sys.stdin.read()
    else:
        text = Path(args.review).read_text(encoding="utf-8")
    review = json.loads(text)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    reviews = {
        str(name): {"grade": str(r.get("grade") or ""),
                    "review": str(r.get("review") or "")}
        for name, r in (review.get("reviews") or {}).items()
        if isinstance(r, dict)
    }
    plan["plan_review"] = {
        "rounds": int(review.get("round") or 1),
        "passed": bool(review.get("passed")),
        "reviews": reviews,
    }
    plan_path.write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"recorded plan_review passed={plan['plan_review']['passed']} "
          f"reviewers={len(reviews)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
