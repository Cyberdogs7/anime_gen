"""Apply agent-written object descriptions as image prompts.

Deterministic only: builds the Krea 2 prompts and saves them for the keyframe
worker. No LLM calls.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.board.store import BoardSet
from studio.config import get_config
from studio.planner import read_episode_plan
from studio.show import Show
from studio.storyboard import (_base_object_prompt, _save_object_prompt,
                               _show_style)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    args = ap.parse_args()

    cfg = get_config()
    store = BoardSet(cfg)
    card = store.get(args.card)
    if not card:
        raise SystemExit(f"no card {args.card!r}")
    show = Show(str(card.get("show_id") or ""))
    episode = int(card.get("episode") or 0)
    ep_dir = show.dir / "runs" / f"EP{episode:02d}"
    plan = read_episode_plan(show, episode)
    wanted = [str(o) for o in (plan.get("objects") or []) if o][:6]
    if not wanted:
        print("no plan objects")
        store.close()
        return 0

    source = ep_dir / "object-prompts.json"
    if not source.exists():
        raise SystemExit(f"object prompts not found: {source}")
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    entries = data.get("objects") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise SystemExit("object-prompts.json must contain an objects list")
    by_name = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name"):
            by_name[str(entry["name"]).strip().lower()] = str(
                entry.get("description") or "").strip()
    missing = [name for name in wanted if not by_name.get(name.strip().lower())]
    if missing:
        raise SystemExit("missing object descriptions: " + ", ".join(missing))

    style = _show_style(show)
    medium = show.ref_medium()
    saved = []
    for name in wanted:
        desc = by_name[name.strip().lower()]
        prompt = _base_object_prompt(name, style, desc, medium)
        _save_object_prompt(show, episode, slugify(name), prompt)
        saved.append(name)

    rel = source.relative_to(Path(cfg.root)).as_posix()
    store.attach(card["id"], rel, "object prompts")
    store.update(card["id"], result={"objects": saved, "path": rel})
    store.close()
    print(f"saved {len(saved)} object prompt(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
