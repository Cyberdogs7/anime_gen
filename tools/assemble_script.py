"""Assemble an agent-written screenplay from per-scene files.

Deterministic only: no LLM calls. The script-writer agent writes
``runs/EPxx/script_scenes/<scene>.json``; this helper validates them, applies the
existing script normalizers, writes the next ``script.rN.json``, and attaches it
to the card.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio import epstate
from studio.board.store import BoardSet
from studio.config import get_config
from studio.planner import (read_episode_plan, _finalize_on_camera_script,
                            _repair_shot_locations)
from studio.scriptgen import _normalize
from studio.show import Show


def _next_script_path(runs: Path) -> Path:
    best = 0
    for p in runs.glob("script.r*.json"):
        m = re.fullmatch(r"script\.r(\d+)\.json", p.name)
        if m:
            best = max(best, int(m.group(1)))
    return runs / f"script.r{best + 1}.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card")
    args = ap.parse_args()
    if not args.card:
        raise SystemExit("--card is required")

    cfg = get_config()
    store = BoardSet(cfg)
    card = store.get(args.card)
    if not card:
        raise SystemExit(f"no card {args.card!r}")

    show = Show(str(card.get("show_id") or ""))
    episode = int(card.get("episode") or 0)
    ep_dir = show.dir / "runs" / f"EP{episode:02d}"
    plan = read_episode_plan(show, episode)
    plan_scenes = [s for s in (plan.get("scenes") or [])
                   if isinstance(s, dict) and s.get("id")]
    if not plan_scenes:
        raise SystemExit("plan has no scenes; write the outline first")

    scenes_dir = ep_dir / "script_scenes"
    missing: list[str] = []
    scenes: list[dict] = []
    for ps in plan_scenes:
        sid = str(ps["id"])
        path = scenes_dir / f"{sid}.json"
        if not path.exists():
            missing.append(sid)
            continue
        try:
            scene = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise SystemExit(f"scene {sid}: invalid JSON: {exc}")
        if not isinstance(scene, dict):
            raise SystemExit(f"scene {sid}: must be a JSON object")
        scene["id"] = sid
        scene.setdefault("location", ps.get("location", ""))
        scene.setdefault("time_of_day", ps.get("time_of_day", ""))
        scene.setdefault("summary", ps.get("summary", ""))
        scene.setdefault("characters", ps.get("characters", []))
        shots = scene.get("shots")
        if not isinstance(shots, list) or not shots:
            raise SystemExit(f"scene {sid}: needs a non-empty shots list")
        seen_ids: set[str] = set()
        for i, shot in enumerate(shots):
            if not isinstance(shot, dict):
                raise SystemExit(f"scene {sid}: shot {i} must be an object")
            shot.setdefault("id", f"{sid}_sh{i + 1:02d}")
            if shot["id"] in seen_ids:
                raise SystemExit(f"scene {sid}: duplicate shot id {shot['id']!r}")
            seen_ids.add(shot["id"])
            refs = shot.get("references")
            if not isinstance(refs, dict):
                refs = {}
                shot["references"] = refs
            chars = [c for c in (refs.get("characters") or []) if c]
            refs["characters"] = chars
            refs.setdefault("via_display", [])
            refs.setdefault("scene", scene.get("location") or "")
            refs.setdefault("costumes", {})
            shot.setdefault("type", "ref2va" if chars else "fl2va")
            shot.setdefault("importance", "standard")
            shot.setdefault("duration_s", 8.0)
            shot.setdefault("beat", "")
            shot.setdefault("camera", "")
            shot.setdefault("action", "")
            shot.setdefault("soundscape", "")
            shot.setdefault("music", "none")
            shot.setdefault("dialogue", [])
            shot.setdefault("silence", not shot["dialogue"])
        scenes.append(scene)

    if missing:
        raise SystemExit(
            "missing scene files: " + ", ".join(missing)
            + f"\nwrite them under {scenes_dir}")

    cast_names = [c.get("name") for c in
                  (show.read_character(cid) for cid in show.list_characters())
                  if c.get("name")]
    script = {
        "episode": episode,
        "summary": plan.get("plot") or plan.get("synopsis") or plan.get("summary") or "",
        "cast": plan.get("characters") or plan.get("cast") or cast_names,
        "scenes": scenes,
    }
    script = _normalize(script, cast_names)
    _finalize_on_camera_script(script)
    _repair_shot_locations(show, script)

    out = _next_script_path(ep_dir)
    epstate.save_script(out, script)
    rel = out.relative_to(Path(cfg.root)).as_posix()
    store.attach(card["id"], rel, "episode script")
    store.close()
    shots = sum(len(sc.get("shots") or []) for sc in scenes)
    print(json.dumps({"path": rel, "scenes": len(scenes), "shots": shots}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
