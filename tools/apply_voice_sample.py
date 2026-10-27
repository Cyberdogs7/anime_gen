"""Apply an agent-written voice sample to the character sheet.

Deterministic only: validates the text, saves it, resets the voice state, and
creates the TTS card. No LLM calls.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio.board.cards import new_card
from studio.board.store import BoardSet
from studio.config import get_config
from studio.show import Show


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
    payload = card.get("payload") or {}
    name = str(payload.get("char") or "").strip()
    if not name:
        raise SystemExit("card has no payload.char")

    show = Show(str(card.get("show_id") or ""))
    char = None
    for cid in show.list_characters():
        c = show.read_character(cid) or {}
        if str(c.get("name") or "").lower() == name.lower():
            char = c
            break
    if not char:
        raise SystemExit(f"no character named {name!r}")

    text_path = show.dir / "voices" / f"{slugify(name)}.txt"
    if not text_path.exists():
        raise SystemExit(f"voice sample not found: {text_path}")
    text = text_path.read_text(encoding="utf-8-sig").strip()
    if len(text.split()) < 50:
        raise SystemExit("voice sample is too short (minimum 50 words)")

    char["voice_sample_text"] = text
    show.write_character(char)
    state = show.bootstrap_state()
    for entry in state.get("characters", []):
        if str(entry.get("name") or "").lower() == name.lower():
            entry["voice"] = ""
    show.set_bootstrap_state(state)

    parent = str(payload.get("parent") or "")
    asset = payload.get("asset") or {}
    active = [c for c in store.all(show_id=show.show_id)
              if c.get("kind") == "gate0_voices"
              and str((c.get("payload") or {}).get("char") or "").lower() == name.lower()
              and c.get("state") not in ("accepted", "blocked", "deferred")]
    if active:
        audio_card = active[0]["id"]
        if parent:
            current = store.get(audio_card) or {}
            new_payload = dict(current.get("payload") or {})
            new_payload.update({"parent": parent, "asset": asset})
            store.update(audio_card, payload=new_payload)
    else:
        audio_card = f"g0.{show.show_id}.voices.r{int(time.time())}"
        voice_payload = {"char": name}
        if parent:
            voice_payload.update({"parent": parent, "asset": asset})
        store.upsert(new_card(
            audio_card, kind="gate0_voices", title=f"Record voice sample: {name}",
            show_id=show.show_id, role="gate0_voices", reviewer="human",
            lane="audio", priority=8, max_attempts=2,
            payload=voice_payload, depends_on=[card["id"]]))

    char_path = show.characters_dir / f"{char.get('id') or name}.yaml"
    rel = char_path.relative_to(Path(cfg.root)).as_posix()
    store.attach(card["id"], rel, f"{name} character sheet")
    result = {"char": name, "words": len(text.split()),
              "voice_sample_text": text, "audio_card": audio_card}
    store.update(card["id"], result=result)
    store.close()
    print(f"applied voice sample for {name}: {result['words']} words -> {audio_card}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
