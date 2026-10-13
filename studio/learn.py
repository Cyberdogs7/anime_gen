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
    """Flatten every reviewer notes file into [{reviewer, round, severity, scene, item, note}].

    When round_no is None, uses the latest round per reviewer. HIGH severity sorts first.
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
            rnd = 0
            m = re.match(r"^(.*)\.r(\d+)$", f.stem)
            if m:
                rnd = int(m.group(2))
            for n in data.get("notes", []) or []:
                if not isinstance(n, dict):
                    out.append({"reviewer": reviewer, "round": rnd, "severity": "",
                                "scene": "", "item": "", "note": str(n)})
                    continue
                out.append({"reviewer": reviewer, "round": rnd,
                            "severity": str(n.get("severity", "")).upper(),
                            "scene": n.get("scene", "") or "",
                            "item": n.get("item", "") or "",
                            "note": n.get("note", "") or n.get("item", "") or ""})
    sev_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "": 3}
    out.sort(key=lambda n: (sev_rank.get(n["severity"], 3), n["reviewer"], n["scene"]))
    return out


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def distill_rules(notes: list[dict[str, Any]],
                  high_only: bool = True) -> list[str]:
    """Turn raw reviewer notes into short durable constraint candidates (deduped).

    Keeps the reviewer's concrete fix directive, strips scene-specific prefixes so
    the rule generalizes to future episodes. Returns human-readable strings ready
    for review before promoting to director_notes.json.
    """
    seen: set[str] = set()
    rules: list[str] = []
    for n in notes:
        if high_only and n.get("severity") != "HIGH":
            continue
        text = (n.get("note") or "").strip()
        if not text:
            continue
        # Strip leading "[Scene s0X] Location - " prefixes; keep the directive.
        text = re.sub(r"^\[Scene\s+s\d+\][^-]*-\s*", "", text).strip()
        text = re.sub(r"^\[Muse Spark\]\s*", "", text).strip()
        candidate = f"[{n.get('reviewer')}] {text}"
        key = _normalize(candidate)
        if key in seen:
            continue
        seen.add(key)
        rules.append(candidate)
    return rules


def summarize(notes: list[dict[str, Any]]) -> dict[str, Any]:
    """Count notes by reviewer/severity for dashboard/CLI display."""
    by_reviewer: dict[str, dict[str, int]] = {}
    for n in notes:
        bucket = by_reviewer.setdefault(n["reviewer"], {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "total": 0})
        sev = n.get("severity") or "LOW"
        if sev not in bucket:
            bucket[sev] = 0
        bucket[sev] += 1
        bucket["total"] += 1
    return {"total": len(notes),
            "high": sum(1 for n in notes if n.get("severity") == "HIGH"),
            "by_reviewer": by_reviewer}


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
    print(f"{args.show} EP{args.episode:02d}: {s['total']} notes ({s['high']} HIGH)")
    for reviewer, counts in sorted(s["by_reviewer"].items()):
        print(f"  {reviewer:12s} HIGH={counts.get('HIGH',0)} MED={counts.get('MEDIUM',0)} "
              f"LOW={counts.get('LOW',0)} total={counts.get('total',0)}")
    print()
    for n in notes:
        if args.all or n.get("severity") == "HIGH":
            print(f"[{n['severity'] or '?':6s}] ({n['reviewer']}) {n['note']}")
    rules = distill_rules(notes, high_only=not args.all)
    print(f"\n{len(rules)} candidate durable rules (HIGH-derived):")
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
