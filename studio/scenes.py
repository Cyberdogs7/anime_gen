"""Single owner of scene (location) reference images and their state.

Mirrors :mod:`studio.refs` for locations: ONE place that renders a location ref
image and reads/writes ``scenes/<sid>/refs/refs.json``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from . import prompts
from .config import get_config
from .show import Show


def refs_dir(show: Show, sid: str) -> Path:
    return show.scenes_dir / sid / "refs"


def load_refs(show: Show, sid: str) -> dict[str, Any]:
    p = refs_dir(show, sid) / "refs.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_refs(show: Show, sid: str, data: dict[str, Any]) -> None:
    rd = refs_dir(show, sid)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "refs.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def generate_ref(show: Show, scene: dict[str, Any], sid: str, cfg=None,
                 report: Callable[[str], None] | None = None) -> bool:
    """Render one location ref image (krea2).

    Returns True when an image landed; records a stub (and returns False) when no
    krea2 instance is configured or the render fails, so callers stop retrying.
    """
    cfg = cfg or get_config()
    rd = refs_dir(show, sid)
    rd.mkdir(parents=True, exist_ok=True)
    if (rd / f"{sid}_ref_01.png").exists():
        return True
    krea2 = cfg.get("env", "comfyui", {}).get("krea2", {}) or {}
    if not krea2.get("url"):
        save_refs(show, sid, {"status": "stub",
                              "note": "no krea2 url configured (env.yaml comfyui.krea2.url)"})
        return False
    from .remote import ServiceOps
    name = scene.get("name", "") or sid
    setting = (scene.get("setting_prompt") or scene.get("description") or "").strip()
    if report:
        report(f"Generating scene image: {name} (Krea 2)…")
    style = ((show.read_bible() or {}).get("style_guide") or "").strip()
    prompt = (f"Anime location concept art. {setting}".rstrip(" .")
              + ". Empty unpopulated establishing shot of the location itself, "
                "cinematic composition. "
              + prompts.ref_style_clause(style, show.ref_medium()))
    out = rd / f"{sid}_ref_01.png"
    anchor = show.dir / "style_anchor.png"
    use_anchor = cfg.get("comfy", "style_anchor", False)
    res = ServiceOps(cfg).generate_image(
        prompt, seed=0, aspect_ratio="16:9", out_path=str(out),
        style_ref=str(anchor) if (use_anchor and anchor.exists()) else "")
    if res.get("ok") and out.exists():
        save_refs(show, sid, {"status": "real", "refs": [out.name],
                              "seed": res.get("seed", 0)})
        return True
    save_refs(show, sid, {"status": "stub",
                          "note": "krea2 scene ref generation unavailable"})
    return False
