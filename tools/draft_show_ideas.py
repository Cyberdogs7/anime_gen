"""Draft the Gate 0 pitch batch deterministically (single LLM call, no agent).

Reads the brief + pitch count off the ideas card, runs the same draft code
path as the exec fallback, and writes shows/<id>/ideas.json. The agent loop
starts here: after this helper runs, the worker critiques and revises the
concrete file with the write/edit tools and gates on check_show_ideas.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.board.store import BoardSet
from studio.bootstrap import BootstrapChain
from studio.config import get_config
from studio.show import Show


def run(card_id: str, cfg=None, store=None, show=None) -> dict:
    cfg = cfg or get_config()
    store = store or BoardSet(cfg)
    card = store.get(card_id)
    if not card:
        raise SystemExit(f"no card {card_id!r}")
    show = show or Show(str(card.get("show_id") or ""))
    payload = card.get("payload") or {}
    brief = str(payload.get("brief") or "")
    try:
        n = int(payload.get("n") or 5)
    except (TypeError, ValueError):
        n = 5
    chain = BootstrapChain(show, cfg=cfg, llm=None)
    log = chain.advance_ideas(brief=brief, n=n)
    ideas = show.read_ideas() or {}
    return {"pitches": len(ideas.get("pitches") or []), "log": log}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    args = ap.parse_args()
    print(json.dumps(run(args.card), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
