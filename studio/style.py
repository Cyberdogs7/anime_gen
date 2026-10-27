"""Iterate on the show's visual style guide (the house look) ALONE.

The style guide lives in ``bible.yaml`` and is appended to every ref/keyframe
prompt. This module lets you re-roll it from feedback without touching the rest of
the bible, and rebuild the house style-anchor image from it.
"""
from __future__ import annotations

from . import prompts
from .config import get_config
from .show import Show


def current(show: Show) -> str:
    return ((show.read_bible() or {}).get("style_guide") or "").strip()


def revise(show: Show, feedback: str = "", cfg=None, llm=None) -> str:
    """LLM-rewrite the show's art direction as separate bible fields.

    ``style_guide`` becomes the rendering/linework line that generation prompts
    use verbatim; ``art_direction`` holds camera/lighting/palette as bible
    reference only. Returns the new ``style_guide`` ('' on failure).
    """
    cfg = cfg or get_config()
    if llm is None:
        from .clients.lmstudio import LMStudioClient
        llm = LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
    model = (cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model"))
    bible = show.read_bible() or {}
    data = llm.chat_json(
        [{"role": "user",
          "content": prompts.art_direction_prompt(bible, feedback)}],
        model=model, role="showrunner", temperature=0.7, max_tokens=400)
    new = (data.get("style_guide") or "").strip()
    art = data.get("art_direction")
    if not new:
        return ""
    bible["style_guide"] = new
    prior = bible.get("art_direction")
    merged = dict(prior) if isinstance(prior, dict) else {}
    if isinstance(art, dict):
        for key in ("camera", "lighting", "palette"):
            val = str(art.get(key) or "").strip()
            if val:
                merged[key] = val
    bible["art_direction"] = merged
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
