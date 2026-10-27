"""Requeue renders for QC failures after the fixer agent revised the shots.

Deterministic only: no LLM calls. The fixer agent edits the scene files and runs
the assembler; this helper creates the render cards and advances the QC card.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.board.cards import new_card
from studio.board.store import BoardSet
from studio.config import get_config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    args = ap.parse_args()

    cfg = get_config()
    store = BoardSet(cfg)
    card = store.get(args.card)
    if not card:
        raise SystemExit(f"no card {args.card!r}")
    payload = card.get("payload") or {}
    failures = payload.get("failures") or []
    if not failures:
        raise SystemExit("no QC failures on this card")
    show_id = str(card.get("show_id") or "")
    ep = int(card.get("episode") or 0)
    milestone = str(payload.get("render_milestone")
                    or f"{show_id}.ep{ep:02d}.render")

    made: list[str] = []
    for failure in failures:
        sid = str((failure or {}).get("shot_id") or "")
        if not sid:
            continue
        cid = f"{show_id}.ep{ep:02d}.shot.{sid}.r{int(time.time() * 1000)}"
        store.upsert(new_card(
            cid, kind="render_shot", title=f"Re-render EP{ep:02d} shot {sid}",
            show_id=show_id, episode=ep, role="render_exec",
            lane="video", priority=4, max_attempts=3,
            payload={"tool": "render_shot", "args": {"shot_id": sid},
                     "shot_id": sid, "parent": milestone},
        ))
        made.append(cid)

    qc_id = str(payload.get("qc_card") or "")
    qc = store.get(qc_id) if qc_id else None
    attempts = 0
    if qc:
        attempts = int(qc.get("attempts") or 0) + 1
        cap = int(qc.get("max_attempts") or 4)
        store.update(qc_id, depends_on=made, attempts=attempts)
        if qc.get("state") == "blocked" and attempts < cap:
            store.advance(qc_id, "planned", "fix", "QC retry")
        elif attempts >= cap:
            store.note(qc_id, "fixer", f"QC retake cap reached ({attempts})")
    store.update(card["id"], result={"render_cards": made, "revised": len(made),
                                     "attempt": attempts if qc else 0})
    store.close()
    print(f"requeued {len(made)} render(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
