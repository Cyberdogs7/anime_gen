"""Merge episode fragment passes into full scene files.

Deterministic only: no LLM calls. Reads runs/EPxx/passes/<sid>.<pass>.json
for every plan scene plus plan.json, writes script_scenes/<sid>.json in the
exact schema assemble_script.py expects, builds episode object-prompts.json,
and saves the Krea object prompts for the keyframe worker.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.board.store import BoardSet
from studio.config import get_config
from studio.planner import read_episode_plan
from studio.show import Show

PASSES_DIR = "passes"


def _read(ep_dir: Path, sid: str, frag: str) -> dict[str, Any]:
    p = ep_dir / PASSES_DIR / f"{sid}.{frag}.json"
    if not p.exists():
        raise SystemExit(f"missing fragment {p.name} (run the {frag} pass first)")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise SystemExit(f"{p.name}: invalid JSON: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{p.name}: must be a JSON object")
    return data


def load_merged_scenes(show: Show, episode: int) -> list[dict[str, Any]]:
    """Build full scene objects from plan + fragments (no writes)."""
    plan = read_episode_plan(show, episode) or {}
    plan_scenes = [s for s in (plan.get("scenes") or [])
                   if isinstance(s, dict) and s.get("id")]
    if not plan_scenes:
        raise SystemExit("plan has no scenes")
    ep_dir = show.dir / "runs" / f"EP{episode:02d}"
    scenes: list[dict[str, Any]] = []
    for ps in plan_scenes:
        sid = str(ps["id"])
        loc = _read(ep_dir, sid, "locations")
        cast = _read(ep_dir, sid, "cast")
        outfits = _read(ep_dir, sid, "outfits")
        narrative = _read(ep_dir, sid, "narrative")
        dialogue = _read(ep_dir, sid, "dialogue")
        shots_frag = _read(ep_dir, sid, "shots")
        camera = _read(ep_dir, sid, "camera")
        action = _read(ep_dir, sid, "action")
        sound = _read(ep_dir, sid, "sound")

        location = str(loc.get("location") or ps.get("location") or "")
        time_of_day = str(loc.get("time_of_day") or ps.get("time_of_day") or "")
        characters = [str(c) for c in (cast.get("characters") or [])]
        scene_costumes = {str(k): str(v) for k, v in
                          ((outfits.get("costumes") or {}).items())}
        lines = {e.get("n"): e for e in (dialogue.get("dialogue") or [])
                 if isinstance(e, dict)}
        shots = []
        for sh in (shots_frag.get("shots") or []):
            shot_id = str(sh.get("id") or "")
            framed = [str(c) for c in (sh.get("characters") or [])]
            dlg_entries = []
            for n in (sh.get("line_refs") or []):
                entry = lines.get(n)
                if entry is None:
                    raise SystemExit(f"{sid} {shot_id}: line_ref {n!r} missing")
                dlg_entries.append({"char": str(entry.get("char") or ""),
                                    "line": str(entry.get("line") or "")})
            via = [e["char"] for e in dlg_entries if e["char"] not in framed]
            snd = (sound.get("sound") or {}).get(shot_id) or {}
            shots.append({
                "id": shot_id,
                "type": sh.get("type") or ("ref2va" if framed else "fl2v"),
                "importance": sh.get("importance") or "standard",
                "duration_s": sh.get("duration_s"),
                "beat": str(sh.get("beat") or ""),
                "camera": str((camera.get("camera") or {}).get(shot_id) or ""),
                "action": str((action.get("action") or {}).get(shot_id) or ""),
                "references": {
                    "characters": framed,
                    "via_display": via,
                    "scene": location,
                    "costumes": {c: scene_costumes[c] for c in framed
                                 if c in scene_costumes},
                },
                "soundscape": str(snd.get("soundscape") or ""),
                "music": str(snd.get("music") or "none"),
                "dialogue": dlg_entries,
                "silence": not dlg_entries,
            })
        if not shots:
            raise SystemExit(f"{sid}: no shots merged")
        scenes.append({
            "id": sid,
            "location": location,
            "time_of_day": time_of_day,
            "summary": str(ps.get("summary") or ""),
            "narrative": str(narrative.get("narrative") or ""),
            "characters": characters,
            "shots": shots,
        })
    return scenes


def _save_object_prompts(show: Show, episode: int, ep_dir: Path,
                         scenes: list[dict[str, Any]]) -> Path:
    from studio.storyboard import (_base_object_prompt, _save_object_prompt,
                                   _show_style)
    seen: dict[str, str] = {}
    for sc in scenes:
        try:
            data = json.loads((ep_dir / PASSES_DIR / f"{sc['id']}.objects.json")
                              .read_text(encoding="utf-8-sig"))
        except OSError:
            continue
        for o in (data.get("objects") or []):
            if not isinstance(o, dict):
                continue
            name = str(o.get("name") or "").strip()
            desc = str(o.get("description") or "").strip()
            if name and desc and name.lower() not in seen:
                seen[name.lower()] = desc
    names_in_order = list(seen)
    out = ep_dir / "object-prompts.json"
    out.write_text(json.dumps({"objects": [
        {"name": n, "description": seen[n.lower()]} for n in names_in_order
    ]}, ensure_ascii=False), encoding="utf-8")
    style = _show_style(show)
    medium = show.ref_medium()
    import re
    for name in names_in_order[:6]:
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
        prompt = _base_object_prompt(name, style, seen[name.lower()], medium)
        _save_object_prompt(show, episode, slug, prompt)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    args = ap.parse_args()

    cfg = get_config()
    store = BoardSet(cfg)
    card = store.get(args.card)
    if not card:
        store.close()
        raise SystemExit(f"no card {args.card!r}")
    show = Show(str(card.get("show_id") or ""))
    episode = int(card.get("episode") or 0)
    ep_dir = show.dir / "runs" / f"EP{episode:02d}"

    scenes = load_merged_scenes(show, episode)
    scenes_dir = ep_dir / "script_scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    for sc in scenes:
        (scenes_dir / f"{sc['id']}.json").write_text(
            json.dumps(sc, ensure_ascii=False), encoding="utf-8")

    obj_path = _save_object_prompts(show, episode, ep_dir, scenes)
    store.attach(args.card, obj_path.relative_to(Path(cfg.root)).as_posix(),
                 "object prompts")
    result = dict(card.get("result") or {})
    result.update({"merged_scenes": len(scenes),
                   "merged_shots": sum(len(s["shots"]) for s in scenes)})
    store.update(args.card, result=result)
    store.close()
    print(f"merged {len(scenes)} scenes, "
          f"{sum(len(s['shots']) for s in scenes)} shots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
