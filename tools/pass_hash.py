"""Hash one fragment pass's inputs for the producer fast-pass.

Deterministic only: sha1 over plan.json plus every strictly-upstream fragment
file, in chain order. The producer compares the printed hash with the card
result's input_hash: a match plus existing outputs means the inputs are
unchanged and the pass can finish verified without rewriting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.board.store import BoardSet
from studio.config import get_config
from studio.planner import read_episode_plan
from studio.show import Show

PASS_ORDER = ["locations", "cast", "outfits", "objects", "narrative",
              "dialogue", "shots", "camera", "action", "sound"]


def pass_inputs(show: Show, episode: int, frag: str) -> list[Path]:
    """Plan file plus upstream fragments this pass reads, in chain order."""
    ep_dir = show.dir / "runs" / f"EP{episode:02d}"
    files = [ep_dir / "plan.json"]
    plan = read_episode_plan(show, episode) or {}
    sids = [str(s.get("id") or "") for s in (plan.get("scenes") or [])
            if isinstance(s, dict) and s.get("id")]
    idx = PASS_ORDER.index(frag) if frag in PASS_ORDER else len(PASS_ORDER)
    for upstream in PASS_ORDER[:idx]:
        for sid in sids:
            files.append(ep_dir / "passes" / f"{sid}.{upstream}.json")
    return files


def hash_inputs(files: list[Path]) -> str:
    h = hashlib.sha1()
    for p in files:
        h.update(str(p.name).encode("utf-8"))
        h.update(b"\x00")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\x00")
    return h.hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    args = ap.parse_args()
    cfg = get_config()
    store = BoardSet(cfg)
    card = store.get(args.card)
    store.close()
    if not card:
        raise SystemExit(f"no card {args.card!r}")
    show = Show(str(card.get("show_id") or ""))
    episode = int(card.get("episode") or 0)
    kind = str(card.get("kind") or "")
    print(hash_inputs(pass_inputs(show, episode, kind)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
