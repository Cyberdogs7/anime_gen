"""Validate one fragment pass's per-scene files, then record the input hash.

Deterministic only: no LLM calls. Checks JSON shape plus light cross-refs
(canon names, shot ids, dialogue line refs). On success writes input_hash into
the card result so the next run can fast-pass when inputs are unchanged.
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
from pass_hash import PASS_ORDER, hash_inputs, pass_inputs


def _canon_names(show: Show) -> set[str]:
    names = set()
    for cid in show.list_characters():
        try:
            n = str((show.read_character(cid) or {}).get("name") or "")
        except Exception:
            n = ""
        if n.strip():
            names.add(n.strip())
    return names


def _fail(msg: str) -> int:
    print(f"INVALID: {msg}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    ap.add_argument("--pass", dest="frag", required=True)
    args = ap.parse_args()
    frag = str(args.frag)
    if frag not in PASS_ORDER:
        return _fail(f"unknown pass {frag!r}")

    cfg = get_config()
    store = BoardSet(cfg)
    card = store.get(args.card)
    if not card:
        store.close()
        return _fail(f"no card {args.card!r}")
    show = Show(str(card.get("show_id") or ""))
    episode = int(card.get("episode") or 0)
    plan = read_episode_plan(show, episode) or {}
    sids = [str(s.get("id") or "") for s in (plan.get("scenes") or [])
            if isinstance(s, dict) and s.get("id")]
    if not sids:
        store.close()
        return _fail("plan has no scenes")
    ep_dir = show.dir / "runs" / f"EP{episode:02d}"
    canon = _canon_names(show)

    def load(sid: str, what: str) -> dict[str, Any]:
        p = ep_dir / "passes" / f"{sid}.{what}.json"
        if not p.exists():
            raise SystemExit(f"missing {p.name}")
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise SystemExit(f"{p.name}: invalid JSON: {exc}")
        if not isinstance(data, dict):
            raise SystemExit(f"{p.name}: must be a JSON object")
        return data

    try:
        if frag == "locations":
            for sid in sids:
                d = load(sid, frag)
                for k in ("location", "time_of_day"):
                    if not str(d.get(k) or "").strip():
                        raise SystemExit(f"{sid}: {k} must be non-empty")
                if not isinstance(d.get("location_id"), str):
                    raise SystemExit(f"{sid}: location_id must be a string")
        elif frag == "cast":
            for sid in sids:
                d = load(sid, frag)
                chars = d.get("characters")
                if not isinstance(chars, list) or not chars:
                    raise SystemExit(f"{sid}: characters must be a non-empty list")
                for c in chars:
                    if not str(c or "").strip():
                        raise SystemExit(f"{sid}: empty character name")
                    if str(c).strip() not in canon:
                        raise SystemExit(f"{sid}: unknown character {c!r}")
        elif frag == "outfits":
            for sid in sids:
                d = load(sid, frag)
                costumes = d.get("costumes")
                if not isinstance(costumes, dict):
                    raise SystemExit(f"{sid}: costumes must be an object")
                cast = load(sid, "cast").get("characters") or []
                for name, label in costumes.items():
                    if name not in cast:
                        raise SystemExit(f"{sid}: costume for off-screen {name!r}")
                    if not str(label or "").strip():
                        raise SystemExit(f"{sid}: empty costume label for {name!r}")
        elif frag == "objects":
            for sid in sids:
                d = load(sid, frag)
                objs = d.get("objects")
                if not isinstance(objs, list):
                    raise SystemExit(f"{sid}: objects must be a list")
                for o in objs:
                    if not isinstance(o, dict) or not str(o.get("name") or "").strip():
                        raise SystemExit(f"{sid}: object needs a name")
                    if not str(o.get("description") or "").strip():
                        raise SystemExit(f"{sid}: object {o.get('name')!r} needs a description")
        elif frag == "narrative":
            for sid in sids:
                d = load(sid, frag)
                if not str(d.get("narrative") or "").strip():
                    raise SystemExit(f"{sid}: narrative must be non-empty")
        elif frag == "dialogue":
            for sid in sids:
                d = load(sid, frag)
                lines = d.get("dialogue")
                if not isinstance(lines, list):
                    raise SystemExit(f"{sid}: dialogue must be a list")
                if not isinstance(d.get("silence"), bool):
                    raise SystemExit(f"{sid}: silence must be a bool")
                if d["silence"] and lines:
                    raise SystemExit(f"{sid}: silent scene must have empty dialogue")
                seen = set()
                for entry in lines:
                    if not isinstance(entry, dict):
                        raise SystemExit(f"{sid}: dialogue entry must be an object")
                    n = entry.get("n")
                    if not isinstance(n, int) or n in seen:
                        raise SystemExit(f"{sid}: dialogue n must be unique ints")
                    seen.add(n)
                    if str(entry.get("char") or "").strip() not in canon:
                        raise SystemExit(f"{sid}: unknown dialogue char {entry.get('char')!r}")
                    if not str(entry.get("line") or "").strip():
                        raise SystemExit(f"{sid}: empty line for n={n}")
        elif frag == "shots":
            for sid in sids:
                d = load(sid, frag)
                shots = d.get("shots")
                if not isinstance(shots, list) or not shots:
                    raise SystemExit(f"{sid}: shots must be a non-empty list")
                try:
                    dlg = load(sid, "dialogue")
                    valid_n = {e.get("n") for e in (dlg.get("dialogue") or [])
                               if isinstance(e, dict)}
                except SystemExit:
                    valid_n = set()
                seen_ids = set()
                cast = set(load(sid, "cast").get("characters") or [])
                for sh in shots:
                    if not isinstance(sh, dict):
                        raise SystemExit(f"{sid}: shot must be an object")
                    shot_id = str(sh.get("id") or "")
                    if not shot_id or shot_id in seen_ids:
                        raise SystemExit(f"{sid}: duplicate/empty shot id {shot_id!r}")
                    seen_ids.add(shot_id)
                    if not str(sh.get("beat") or "").strip():
                        raise SystemExit(f"{shot_id}: beat must be non-empty")
                    try:
                        dur = float(sh.get("duration_s") or 0)
                    except (TypeError, ValueError):
                        raise SystemExit(f"{shot_id}: bad duration_s")
                    if dur <= 0:
                        raise SystemExit(f"{shot_id}: duration_s must be > 0")
                    if sh.get("type") not in ("ref2va", "fl2v"):
                        raise SystemExit(f"{shot_id}: type must be ref2va|fl2v")
                    if sh.get("importance") not in ("standard", "hero"):
                        raise SystemExit(f"{shot_id}: importance must be standard|hero")
                    for c in (sh.get("characters") or []):
                        if c not in cast:
                            raise SystemExit(f"{shot_id}: off-screen char {c!r}")
                    for n in (sh.get("line_refs") or []):
                        if n not in valid_n:
                            raise SystemExit(f"{shot_id}: line_ref {n!r} not in dialogue")
        elif frag in ("camera", "action"):
            need_shots = {sid: load(sid, "shots") for sid in sids}
            for sid in sids:
                d = load(sid, frag)
                block = d.get(frag)
                if not isinstance(block, dict) or not block:
                    raise SystemExit(f"{sid}: {frag} must be a non-empty object")
                shot_ids = {str(s.get("id") or "") for s in
                            (need_shots[sid].get("shots") or [])}
                for shot_id, text in block.items():
                    if shot_id not in shot_ids:
                        raise SystemExit(f"{sid}: {frag} for unknown shot {shot_id!r}")
                    if not str(text or "").strip():
                        raise SystemExit(f"{sid}: empty {frag} for {shot_id!r}")
        elif frag == "sound":
            need_shots = {sid: load(sid, "shots") for sid in sids}
            for sid in sids:
                d = load(sid, frag)
                block = d.get("sound")
                if not isinstance(block, dict) or not block:
                    raise SystemExit(f"{sid}: sound must be a non-empty object")
                shot_ids = {str(s.get("id") or "") for s in
                            (need_shots[sid].get("shots") or [])}
                for shot_id, entry in block.items():
                    if shot_id not in shot_ids:
                        raise SystemExit(f"{sid}: sound for unknown shot {shot_id!r}")
                    if not isinstance(entry, dict):
                        raise SystemExit(f"{sid}: sound entry must be an object")
                    for k in ("soundscape", "music"):
                        if not str(entry.get(k) or "").strip():
                            raise SystemExit(f"{sid}: empty {k} for {shot_id!r}")
    except SystemExit as exc:
        store.close()
        code = exc.code
        print(f"INVALID: {code}" if isinstance(code, str) else "INVALID")
        return 1

    digest = hash_inputs(pass_inputs(show, episode, frag))
    result = dict(card.get("result") or {})
    result.update({"pass": frag, "scenes": len(sids), "input_hash": digest})
    store.update(args.card, result=result)
    store.close()
    print(f"ok: {frag} x{len(sids)} scenes, input_hash {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
