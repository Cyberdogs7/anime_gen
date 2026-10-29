"""Compile every shot's H3 prompt without rendering; report ref gaps.

Deterministic only: no LLM calls. Reads the merged script_scenes/*.json,
runs render.compile_shot_prompt per shot, writes runs/EPxx/h3_prompts.json
({prompts, gaps}) and attaches it. Always exits 0: gaps are review input,
not a merge failure — the h3 reviewer fails the card with directives.
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
from studio.show import Show


def _cid_by_name(show: Show) -> dict[str, str]:
    out: dict[str, str] = {}
    for cid in show.list_characters():
        try:
            name = str((show.read_character(cid) or {}).get("name") or "").strip()
        except Exception:
            continue
        if name:
            out[name] = cid
    return out


def _char_ref_ok(show: Show, cid: str) -> bool:
    try:
        from studio import refs as _refs
        data = _refs.load_refs(show, cid) or {}
        variants = data.get("variants") or {}
        approved = data.get("approved") or []
        return bool(variants) and bool(approved)
    except Exception:
        return False


def _scene_ref_ok(show: Show, sid_or_name: str) -> bool:
    try:
        from studio import scenes as _scenes
        for sid in show.list_scenes():
            try:
                data = _scenes.load_refs(show, sid) or {}
            except Exception:
                continue
            if data.get("status") != "real":
                continue
            if sid == sid_or_name:
                return True
        # Fall back to name match on the scene registry.
        import yaml
        for sid in show.list_scenes():
            y = show.scenes_dir / f"{sid}.yaml"
            try:
                meta = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if str(meta.get("name") or "") == sid_or_name:
                try:
                    data = _scenes.load_refs(show, sid) or {}
                except Exception:
                    data = {}
                return data.get("status") == "real"
    except Exception:
        pass
    return False


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

    from studio.render import compile_shot_prompt
    cids = _cid_by_name(show)
    prompts: dict[str, str] = {}
    gaps: list[dict[str, Any]] = []
    scenes_dir = ep_dir / "script_scenes"
    scene_files = sorted(scenes_dir.glob("*.json")) if scenes_dir.exists() else []
    if not scene_files:
        store.close()
        raise SystemExit("no merged scene files (run merge first)")
    script = {"episode": episode, "scenes": []}
    for path in scene_files:
        try:
            sc = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            store.close()
            raise SystemExit(f"{path.name}: invalid JSON: {exc}")
        if isinstance(sc, dict):
            script["scenes"].append(sc)
    for sc in script["scenes"]:
        if not isinstance(sc, dict):
            continue
        for sh in (sc.get("shots") or []):
            if not isinstance(sh, dict):
                continue
            shot_id = str(sh.get("id") or "shot")
            framed = [str(c) for c in
                      ((sh.get("references") or {}).get("characters") or [])]
            via = [str(c) for c in
                   ((sh.get("references") or {}).get("via_display") or [])]
            names = [n for n in framed + via if n]
            try:
                prompt = compile_shot_prompt(show, script, sc, sh, names)
            except Exception as exc:
                gaps.append({"shot": shot_id,
                             "missing": [f"prompt compile failed: {exc}"]})
                continue
            prompts[shot_id] = prompt
            missing: list[str] = []
            for n in framed:
                cid = cids.get(n)
                if not cid or not _char_ref_ok(show, cid):
                    missing.append(f"no approved ref for {n}")
            loc = str(sc.get("location") or "")
            if loc and not _scene_ref_ok(show, loc):
                missing.append(f"no real ref for set {loc!r}")
            if missing:
                gaps.append({"shot": shot_id, "missing": missing})

    out = ep_dir / "h3_prompts.json"
    out.write_text(json.dumps({"prompts": prompts, "gaps": gaps},
                              ensure_ascii=False), encoding="utf-8")
    store.attach(args.card, out.relative_to(Path(cfg.root)).as_posix(),
                 "H3 prompts")
    result = dict(card.get("result") or {})
    result.update({"compiled_shots": len(prompts), "ref_gaps": len(gaps)})
    store.update(args.card, result=result)
    store.close()
    print(f"compiled {len(prompts)} prompts, {len(gaps)} gap(s)")
    for g in gaps:
        print(f"  gap {g['shot']}: {'; '.join(g['missing'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
