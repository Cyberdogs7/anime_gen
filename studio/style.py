"""Iterate on the show's visual style guide (the house look) ALONE.

The style guide lives in ``bible.yaml`` and is appended to every ref/keyframe
prompt. This module lets you re-roll it from feedback without touching the rest of
the bible, and rebuild the house style-anchor image from it.
"""
from __future__ import annotations

import json

from . import prompts
from .config import get_config
from .show import Show


def current(show: Show) -> str:
    return ((show.read_bible() or {}).get("style_guide") or "").strip()


def revise(show: Show, feedback: str = "", cfg=None, llm=None) -> str:
    """LLM-rewrite ONLY ``style_guide`` from feedback (pure art direction) and write
    it back to the bible. Returns the new guide ('' on failure)."""
    cfg = cfg or get_config()
    if llm is None:
        from .clients.lmstudio import LMStudioClient
        llm = LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
    model = (cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model"))
    bible = show.read_bible() or {}
    text = llm.chat(
        [{"role": "user",
          "content": prompts.style_guide_prompt(current(show), feedback, bible)}],
        model=model, role="showrunner", temperature=0.7, max_tokens=400)
    s = text[text.find("{"): text.rfind("}") + 1]
    new = (json.loads(s).get("style_guide") or "").strip()
    if new:
        bible["style_guide"] = new
        show.write_bible(bible)
    return new


def regenerate_anchor(show: Show, cfg=None) -> bool:
    """Rebuild the house style-anchor image from the current style guide."""
    from .storyboard import ensure_style_anchor
    try:
        p = show.dir / "style_anchor.png"
        if p.exists():
            p.unlink()
        ensure_style_anchor(show, cfg)
        return p.exists()
    except Exception:
        return False
