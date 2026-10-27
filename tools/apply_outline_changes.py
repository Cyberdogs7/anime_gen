"""Apply a small outline-changes JSON to plan.json.

Normal file tool, not an LLM call. The writer agent writes only the scenes and
fields it wants to change; this helper merges them, validates the beats, and
rewrites plan.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOP_KEYS = ("episode", "title", "episode_type", "threat_of_the_week", "plot",
            "synopsis", "characters", "cast", "objects", "plotline_updates")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan")
    ap.add_argument("--changes")
    ap.add_argument("--card")
    args = ap.parse_args()

    card = None
    if args.card:
        from studio.config import get_config
        from studio.board.store import BoardSet
        card = BoardSet(get_config()).get(args.card)
        if not card:
            raise SystemExit(f"no card {args.card!r}")
        root = Path(get_config().root)
        ep_dir = (root / "shows" / str(card.get("show_id") or "")
                  / "runs" / f"EP{int(card.get('episode') or 0):02d}")
        args.plan = str(ep_dir / "plan.json")
        args.changes = str(ep_dir / "outline-changes.json")
    if not args.plan or not args.changes:
        raise SystemExit("provide --plan and --changes, or --card")

    plan_path = Path(args.plan)
    plan = (json.loads(plan_path.read_text(encoding="utf-8-sig"))
            if plan_path.exists() else {})
    changes = json.loads(Path(args.changes).read_text(encoding="utf-8-sig"))
    if not isinstance(changes, dict):
        raise SystemExit("changes must be a JSON object")

    for key in TOP_KEYS:
        if key in changes:
            plan[key] = changes[key]

    original = [s for s in (plan.get("scenes") or []) if isinstance(s, dict)
                and s.get("id")]
    by_id = {str(s["id"]): s for s in original}
    for sid in (changes.get("remove_scene_ids") or []):
        by_id.pop(str(sid), None)

    changed = 0
    for scene in (changes.get("scenes") or []):
        if not isinstance(scene, dict) or not scene.get("id"):
            continue
        sid = str(scene["id"])
        merged = {**by_id.get(sid, {}), **scene}
        beats = merged.get("beats")
        if not isinstance(beats, list) or len(beats) != 3:
            raise SystemExit(f"scene {sid}: beats must be a list of exactly 3")
        by_id[sid] = merged
        changed += 1

    order = [str(x) for x in (changes.get("order") or [])]
    if order:
        ordered = [by_id[sid] for sid in order if sid in by_id]
        ordered += [s for sid, s in by_id.items() if sid not in order]
    else:
        ordered = [by_id[str(s["id"])] for s in original
                   if str(s["id"]) in by_id]
        ordered += [s for sid, s in by_id.items()
                    if sid not in {str(s["id"]) for s in original}]

    if not ordered:
        raise SystemExit("plan has no scenes")

    episode = int(card.get("episode") or 0) if card else int(plan.get("episode") or 0)
    if card:
        plan.setdefault("episode", episode)
        plan.setdefault("title", f"Episode {episode}")
        plan.setdefault("episode_type", "character")
        plan.setdefault("threat_of_the_week", "none")
        chars = list(plan.get("characters") or [])
        for scene in ordered:
            for name in scene.get("characters") or []:
                if name not in chars:
                    chars.append(name)
        plan["characters"] = chars
        plan.setdefault("cast", chars)
        plan.setdefault("plot", "")
        plan.setdefault("synopsis", "")
        plan.setdefault("objects", [])
        try:
            from studio.show import Show
            from studio.season import read_season_entry
            from studio.planner import _canonicalize_locations
            show = Show(str(card.get("show_id") or ""))
            season_no, entry = read_season_entry(show, episode)
            if entry:
                plan["_season"] = {"season": season_no, "entry": entry}
            plan.setdefault("_plan_dev", {
                "episode_type": plan.get("episode_type", ""),
                "featured": [],
                "new_plotline": None,
                "season": season_no,
            })
            _canonicalize_locations(show, plan)
        except Exception as exc:
            print(f"warning: season/location finalize skipped: {exc}")

    plan["scenes"] = ordered
    plan["status"] = "pending"
    plan.pop("plan_review", None)
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    print(f"applied {changed} scene change(s); plan now has {len(ordered)} scenes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
