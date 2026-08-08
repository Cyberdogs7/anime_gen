"""Ref pass: ensure every character an episode script's cast pre-section lists has a ref.

The writers' room emits a structured `cast` field in the script JSON (see
prompts.script_prompt) listing EVERY character who appears. This pass simply
diffs those exact names against characters that already have an approved
reference image and creates a ref for the missing ones — no prose scanning, no
fragile string matching.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .bootstrap import ACTIVITY
from .config import get_config
from .show import Show


def _latest_script(show: Show, episode: int) -> dict[str, Any] | None:
    d = show.dir / "runs" / f"EP{episode:02d}"
    scripts = sorted(d.glob("script.r*.json"), key=lambda p: p.stat().st_mtime)
    if not scripts:
        return None
    return json.loads(scripts[-1].read_text(encoding="utf-8"))


def _approved_names(show: Show) -> set[str]:
    return {show.read_character(cid).get("name") for cid in show.list_characters()}


def _ask_appearance(llm, model: str, name: str, context: str) -> str:
    text = llm.chat([
        {"role": "user", "content":
         f"Write a canonical appearance description of the anime character '{name}' for image "
         "generation: hair color and style, eye color, outfit, build. 1-3 vivid sentences.\n"
         f"How the episode depicts them: {context or 'a supporting character in this episode'}\n"
         'Reply with ONLY JSON: {"appearance_canonical": "..."}'},
    ], model=model, temperature=0.6, max_tokens=1200)
    try:
        s = text[text.find("{"): text.rfind("}") + 1]
        return (json.loads(s).get("appearance_canonical") or "").strip()
    except Exception:
        return ""


def _character_context(script: dict[str, Any], name: str) -> str:
    """Concatenate the script's structured mentions of this character (trimmed)."""
    parts: list[str] = []
    for sc in script.get("scenes", []):
        for shot in sc.get("shots", []):
            for d in shot.get("dialogue", []) or []:
                if d.get("char") == name and d.get("line"):
                    parts.append(d["line"])
            if name in (shot.get("action", "") + shot.get("camera", "")):
                parts.append(shot.get("action", "")[:160])
    return " ".join(parts)[:900]


def create_missing_character_refs(show: Show, episode: int, cfg=None, llm=None,
                                  max_new: int = 4) -> list[str]:
    """Diff the script's `cast` pre-section against approved refs; create the missing ones.

    Returns the names of newly created characters.
    """
    from .clients.lmstudio import LMStudioClient
    from .remote.ops import ServiceOps
    from .comfy_workflows import generate_keyframe, load_workflow

    cfg = cfg or get_config()
    script = _latest_script(show, episode)
    if not script:
        return []
    cast = [c for c in script.get("cast", []) if c]
    if not cast:
        return []
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    approved = _approved_names(show)
    missing = [name for name in cast if name not in approved][:max_new]
    if not missing:
        return []

    created: list[str] = []
    ops = ServiceOps(cfg)
    wf_path = cfg.workflows_dir / "image_keyframe.json"
    client, stop = ops._krea2_client()
    try:
        for name in missing:
            slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "char"
            if (show.characters_dir / f"{slug}.yaml").exists():
                continue
            ACTIVITY[show.show_id] = {"detail": f"Ref pass: writing appearance for {name}…",
                                      "ts": time.time()}
            canon = _ask_appearance(llm, model, name, _character_context(script, name))
            if not canon:
                continue
            show.write_character({"id": slug, "name": name, "role": "episode supporting",
                                  "appearance_canonical": canon})
            refs_dir = show.character_refs_dir(slug)
            refs_dir.mkdir(parents=True, exist_ok=True)
            out = refs_dir / f"{slug}_ref_01.png"
            prompt = (f"Anime character reference portrait of {name}. {canon}".rstrip(" .")
                      + ". Full body, front view, neutral standing pose, plain studio "
                        "background, clean lineart, consistent character design, high quality.")
            ACTIVITY[show.show_id] = {"detail": f"Ref pass: rendering ref for {name} (Krea 2)…",
                                      "ts": time.time()}
            try:
                generate_keyframe(client, load_workflow(wf_path), prompt, 0, str(out),
                                  aspect_ratio="3:4")
                (refs_dir / "refs.json").write_text(
                    json.dumps({"status": "real", "variants": {"base": out.name}}),
                    encoding="utf-8")
                created.append(name)
            except Exception as exc:
                ACTIVITY.setdefault(show.show_id, {})["detail"] = (
                    f"Ref pass {name} failed: {exc}")
    finally:
        if stop:
            stop()
    return created
