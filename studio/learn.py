"""Review-to-prompt learning loop: turn writers'-room rejections into durable prompting rules.

A rejected script leaves its full feedback in two places:

  shows/<id>/runs/EP##/reviews/<reviewer>.r<N>.json   # per-reviewer notes files
  shows/<id>/runs/EP##/script.r<N>.json::_review      # flattened gate verdict

This module aggregates those notes (HIGH severity first), dedupes them into
candidate director constraints, and promotes the approved ones into the places
the generators actually read:

  shows/<id>/director_notes.json          # standing ABSOLUTE constraints fed to
                                          # outline + dialogue + scene-detail + shot passes
  continuity/state.json: continuity_rules # hard canon the continuity reviewer checks

Usage:
  python -m studio.learn --show <show-id> --episode 1        # print HIGH summary
  python -m studio.learn --show <show-id> --episode 1 --promote  # append HIGH-derived
                                                               # rules to director_notes.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def _runs_dir(show_dir: Path, episode: int) -> Path:
    return show_dir / "runs" / f"EP{episode:02d}"


def aggregate_reviews(show_dir: Path, episode: int,
                      round_no: int | None = None) -> list[dict[str, Any]]:
    """Flatten each reviewer's verdict into [{reviewer, round, grade, review}].

    When round_no is None, uses the latest round per reviewer. Fails sort first.
    """
    rdir = _runs_dir(show_dir, episode) / "reviews"
    if not rdir.exists():
        return []
    by_reviewer: dict[str, list[tuple[int, Path]]] = {}
    for f in rdir.glob("*.json"):
        m = re.match(r"^(.*)\.r(\d+)$", f.stem)
        reviewer = m.group(1) if m else f.stem
        rnd = int(m.group(2)) if m else 0
        by_reviewer.setdefault(reviewer, []).append((rnd, f))
    out: list[dict[str, Any]] = []
    for reviewer, files in by_reviewer.items():
        files.sort(key=lambda t: t[0])
        picked = [f for r, f in files if round_no is None or r == round_no]
        if round_no is None and files:
            picked = [files[-1][1]]
        for f in picked:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            m = re.match(r"^(.*)\.r(\d+)$", f.stem)
            rnd = int(m.group(2)) if m else 0
            passed = data.get("pass")
            grade = str(data.get("grade") or ("Pass" if passed else "Fail"))
            out.append({"reviewer": reviewer, "round": rnd, "grade": grade,
                        "review": str(data.get("review") or "").strip()})
    out.sort(key=lambda n: (str(n["grade"]).lower() == "pass", n["reviewer"]))
    return out


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def distill_rules(notes: list[dict[str, Any]],
                  high_only: bool = True) -> list[str]:
    """Turn failing reviewers' free-form reviews into durable constraint candidates.

    Keeps the reviewer's concrete directive (strips any scene prefix). Returns
    human-readable strings ready for review before promoting to director_notes.json.
    """
    seen: set[str] = set()
    rules: list[str] = []
    for n in notes:
        if high_only and str(n.get("grade", "")).lower() == "pass":
            continue
        text = (n.get("review") or "").strip()
        if not text:
            continue
        text = re.sub(r"^\[Scene\s+s\d+\][^-]*-\s*", "", text).strip()
        candidate = f"[{n.get('reviewer')}] {text}"
        key = _normalize(candidate)
        if key in seen:
            continue
        seen.add(key)
        rules.append(candidate)
    return rules


def summarize(notes: list[dict[str, Any]]) -> dict[str, Any]:
    """Count reviewers by grade for dashboard/CLI display."""
    by_reviewer: dict[str, dict[str, int]] = {}
    for n in notes:
        bucket = by_reviewer.setdefault(n["reviewer"], {"Pass": 0, "Fail": 0, "total": 0})
        g = "Pass" if str(n.get("grade", "")).lower() == "pass" else "Fail"
        bucket[g] += 1
        bucket["total"] += 1
    fails = sum(1 for n in notes if str(n.get("grade", "")).lower() != "pass")
    return {"total": len(notes), "fail": fails, "by_reviewer": by_reviewer}


def promote_rules(show_dir: Path, rules: list[str]) -> list[str]:
    """Append approved rule strings to director_notes.json (deduped). Returns full list."""
    from studio.planner import add_director_note  # local import: planner owns the file
    from studio.show import Show

    # add_director_note needs a Show; reconstruct show_id from dir name.
    show = Show(show_dir.name, root=show_dir.parent.parent)
    # Show() expects <root>/shows/<id>; show_dir is <root>/shows/<id>.
    out: list[str] = []
    for r in rules:
        out = add_director_note(show, r)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Aggregate writers'-room review notes into prompt rules")
    ap.add_argument("--show", required=True)
    ap.add_argument("--episode", type=int, required=True)
    ap.add_argument("--round", type=int, default=None, help="review round (default: latest per reviewer)")
    ap.add_argument("--root", default=".", help="repo root (contains shows/)")
    ap.add_argument("--promote", action="store_true",
                    help="append HIGH-derived rules to director_notes.json")
    ap.add_argument("--all", action="store_true", help="include MEDIUM/LOW, not just HIGH")
    args = ap.parse_args(argv)

    show_dir = Path(args.root) / "shows" / args.show
    notes = aggregate_reviews(show_dir, args.episode, round_no=args.round)
    if not notes:
        print(f"no review notes for {args.show} EP{args.episode:02d}")
        return 1
    s = summarize(notes)
    print(f"{args.show} EP{args.episode:02d}: {s['total']} reviewers ({s['fail']} Fail)")
    for reviewer, counts in sorted(s["by_reviewer"].items()):
        print(f"  {reviewer:14s} Pass={counts.get('Pass', 0)} Fail={counts.get('Fail', 0)}")
    print()
    for n in notes:
        if args.all or str(n.get("grade", "")).lower() != "pass":
            print(f"[{n['grade']:4s}] ({n['reviewer']}) {n['review']}")
    rules = distill_rules(notes, high_only=not args.all)
    print(f"\n{len(rules)} candidate durable rules (Fail-derived):")
    for r in rules:
        print(f"  - {r}")
    if args.promote:
        promoted = promote_rules(show_dir, rules)
        print(f"\ndirector_notes.json now holds {len(promoted)} constraints.")
    elif rules:
        print("\nRe-run with --promote to append these to director_notes.json (deduped).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
