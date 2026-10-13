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

from . import fsutil, prompts
from .bootstrap import ACTIVITY
from .config import get_config
from .names import looks_like_character_name  # noqa: F401  (re-exported)
from .show import Show


def _style_ref(show: Show) -> str | None:
    """The house-style anchor for IPAdapter, or None when off/absent.

    Gated by ``comfy.style_anchor``: the SDXL ip-adapter has no effect on krea2.
    """
    if not get_config().get("comfy", "style_anchor", False):
        return None
    p = show.dir / "style_anchor.png"
    return str(p) if p.exists() else None



def _latest_script(show: Show, episode: int) -> dict[str, Any] | None:
    d = show.dir / "runs" / f"EP{episode:02d}"
    scripts = fsutil.existing_by_mtime(d.glob("script.r*.json"))
    if not scripts:
        return None
    return json.loads(scripts[-1].read_text(encoding="utf-8"))


def _approved_names(show: Show) -> set[str]:
    """Names of characters that have an ACTUAL generated reference image.

    A character sheet alone is not "approved" — the sheet may exist while no ref
    was ever rendered (e.g. an episode-supporting character like the threat of
    the week). Only a refs.json with a real base image counts.
    """
    approved: set[str] = set()
    for cid in show.list_characters():
        c = show.read_character(cid)
        if not c.get("name"):
            continue
        rd = show.character_refs_dir(cid)
        rj = rd / "refs.json"
        if not rj.exists():
            continue
        try:
            data = json.loads(rj.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("status") != "real":
            continue
        base = (data.get("variants") or {}).get("base")
        refs = data.get("refs") or []
        if base and (rd / base).exists():
            approved.add(c["name"])
        elif any((rd / f).exists() for f in refs):
            approved.add(c["name"])
    return approved


# A group-count marker on a character name, e.g. "Swarmer Pods (x6)",
# "Swarmer Pods x6", "(6)"... A count is a SCENE fact (how many of that unit
# appear), not part of the character's identity - the ref must be ONE unit.
# A bare trailing number (an entity DESIGNATION such as "Unit-734") is NOT a
# count, so the marker must be parenthesized/bracketed or explicitly prefixed
# with "x".
_GROUP_COUNT_RE = re.compile(
    r"[\s]*[\(\[][\s]*x?\s*(\d+)\s*[\)\]]\s*$"   # (x6), (6), [x6]
    r"|[\s]*x\s*(\d+)\s*$",                       # x6 (no brackets)
    re.I)


def _unit_name(name: str) -> str:
    """De-quantify + de-pluralize a group character name into a single unit.

    e.g. 'Swarmer Pods (x6)' -> 'Swarmer Pod'. A ref generated from this name
    describes ONE unit; the scene's action text still says how many.
    """
    s = (name or "").strip()
    m = _GROUP_COUNT_RE.search(s)
    if m:
        s = s[: m.start()].strip()
    words = s.split()
    if words:
        last = words[-1]
        low = last.lower()
        if low.endswith("s") and not low.endswith(("ss", "us", "is")):
            words[-1] = last[:-1]
    return " ".join(words).strip()


def _unit_key(name: str) -> str:
    """Normalized key for matching a script name to a character sheet/ref.

    e.g. 'Swarmer Pods (x6)', 'Swarmer Pods' and 'Swarmer Pod' all key to
    'swarmer pod'.
    """
    return _unit_name(name).lower()


def _revise_appearance(llm, model: str, name: str, current: str, notes: str) -> str:
    """LLM rewrites ONLY the appearance_canonical from rejection feedback."""
    text = llm.chat([
        {"role": "user", "content":
         f"Revise this anime character's canonical appearance description to address "
         f"the director's feedback. Keep everything the feedback did not ask to change.\n"
         f"Character: {name}\n"
         f"CURRENT APPEARANCE:\n{current}\n"
         f"DIRECTOR'S FEEDBACK:\n{notes}\n"
         + prompts.appearance_canonical_rules() + "\n"
         'Reply with ONLY JSON: {"appearance_canonical": "..."}'},
    ], model=model, role="showrunner", temperature=0.6, max_tokens=1200)
    try:
        s = text[text.find("{"): text.rfind("}") + 1]
        return (json.loads(s).get("appearance_canonical") or "").strip()
    except Exception:
        return ""


def regenerate_character_ref(show: Show, name: str, notes: str = "",
                             cfg=None, llm=None) -> bool:
    """Feedback-driven regeneration of a character's BASE reference image.

    Works for bootstrap AND episode-supporting characters (enemy units, group
    units): finds the sheet by unit name, revises the appearance from the notes
    (LLM), re-renders the ref via Krea 2, and rewrites refs.json. Returns True
    when a fresh ref image landed.
    """
    from .clients.lmstudio import LMStudioClient
    from .comfy_workflows import generate_keyframe, load_workflow
    from .consistency import render_until_single
    from .remote.ops import ServiceOps

    cfg = cfg or get_config()
    target = None
    for cid in show.list_characters():
        c = show.read_character(cid)
        if c.get("name") and _unit_key(c.get("name")) == _unit_key(name):
            target = (cid, c)
            break
    if not target:
        return False
    cid, c = target
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    vmodel = cfg.get("llm", "roles", {}).get("describer") or model
    canon = (c.get("appearance_canonical") or "").strip()
    if notes.strip():
        revised = _revise_appearance(llm, model, c.get("name", ""), canon, notes)
        if revised:
            canon = revised
            c["appearance_canonical"] = canon
            show.write_character(c)
    if not canon:
        return False
    refs_dir = show.character_refs_dir(cid)
    refs_dir.mkdir(parents=True, exist_ok=True)
    out = refs_dir / f"{cid}_ref_01.png"
    try:
        if out.exists():
            out.unlink()
    except Exception:
        pass
    style = ((show.read_bible() or {}).get("style_guide") or "").strip()
    prompt = prompts.character_ref_prompt(c.get('name', ''), canon, style)
    ops = ServiceOps(cfg)
    wf_path = cfg.workflows_dir / "image_keyframe.json"
    wf = load_workflow(wf_path)
    client, stop = ops._krea2_client()
    try:
        def _render() -> bool:
            generate_keyframe(client, wf, prompt, 0, str(out),
                              aspect_ratio="3:4", style_ref=_style_ref(show))
            return out.exists()

        gate = render_until_single(_render, out, llm=llm, model=vmodel,
                                   label=f"base ref {c.get('name', '')}")
        if not gate["ok"]:
            return False
        # Preserve any existing costume variants / prompts / approvals — a base-ref
        # regen must NOT wipe the character's other registered refs.
        rj = refs_dir / "refs.json"
        prior: dict[str, Any] = {}
        if rj.exists():
            try:
                prior = json.loads(rj.read_text(encoding="utf-8")) or {}
            except Exception:
                prior = {}
        variants = dict(prior.get("variants") or {})
        variants["base"] = out.name
        prior.update({"status": "real", "refs": [out.name], "variants": variants})
        rj.write_text(json.dumps(prior, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return False
    finally:
        if stop:
            stop()
    return out.exists()


def _ask_appearance(llm, model: str, name: str, context: str) -> str:
    """Write a rich, image-prompt-style appearance_canonical for a supporting
    character — the SAME discipline the main cast uses (appearance_canonical_rules),
    not the old terse '1-3 sentences' ask, so the ref renders in one fixed look."""
    text = llm.chat([
        {"role": "user", "content":
         f"Write the canonical appearance for the anime character '{name}'.\n"
         f"How the episode depicts them: {context or 'a supporting character in this episode'}\n\n"
         + prompts.appearance_canonical_rules() + "\n\n"
         "MANDATORY: name the single outfit they wear, head to toe (garments, "
         "colors, materials, footwear). The character is NEVER nude or "
         "underwear-only — always fully dressed in the outfit.\n\n"
         'Reply with ONLY JSON: {"appearance_canonical": "..."}'},
    ], model=model, role="showrunner", temperature=0.6, max_tokens=1200)
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
    from .consistency import render_until_single

    cfg = cfg or get_config()
    script = _latest_script(show, episode)
    if not script:
        return []
    cast = [c for c in script.get("cast", []) if c]
    if not cast:
        return []
    # The threat/enemy gets lots of screen time but the LLM often leaves it out
    # of the cast. Always inject it deterministically so it gets a ref sheet.
    from .planner import read_episode_plan
    plan = read_episode_plan(show, episode)
    threat = (plan.get("threat_of_the_week") or "").strip()
    if (threat and threat.lower() != "none"
            and looks_like_character_name(threat) and threat not in cast):
        cast.append(threat)
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    vmodel = cfg.get("llm", "roles", {}).get("describer") or model
    approved = _approved_names(show)
    approved_units = {_unit_key(n) for n in approved}
    missing = [name for name in cast
               if _unit_key(name) not in approved_units
               and looks_like_character_name(name)][:max_new]
    if not missing:
        return []

    created: list[str] = []
    ops = ServiceOps(cfg)
    wf_path = cfg.workflows_dir / "image_keyframe.json"
    client, stop = ops._krea2_client()
    try:
        for name in missing:
            # Group-count names ("Pods (x6)") are normalized to ONE unit so the
            # sheet + ref describe a single pod and the scene decides the count.
            unit = _unit_name(name)
            slug = re.sub(r"[^a-z0-9-]+", "-", unit.lower()).strip("-") or "char"
            sheet_path = show.characters_dir / f"{slug}.yaml"
            # A sheet may exist with no ref (e.g. the threat of the week got a
            # sheet but the ref pass skipped it because the sheet was there).
            # Reuse its appearance_canonical and render the missing ref; only
            # WRITE a new sheet when none exists.
            if sheet_path.exists():
                existing = show.read_character(slug)
                canon = (existing.get("appearance_canonical") or "").strip()
                if not canon:
                    ACTIVITY[show.show_id] = {"detail": f"Ref pass: writing appearance for {name}…",
                                              "ts": time.time()}
                    canon = _ask_appearance(llm, model, unit, _character_context(script, name))
                    if not canon:
                        continue
                    existing["appearance_canonical"] = canon
                    show.write_character(existing)
            else:
                ACTIVITY[show.show_id] = {"detail": f"Ref pass: writing appearance for {name}…",
                                          "ts": time.time()}
                canon = _ask_appearance(llm, model, unit, _character_context(script, name))
                if not canon:
                    continue
                show.write_character({"id": slug, "name": unit, "role": "episode supporting",
                                      "appearance_canonical": canon})
            refs_dir = show.character_refs_dir(slug)
            refs_dir.mkdir(parents=True, exist_ok=True)
            out = refs_dir / f"{slug}_ref_01.png"
            # Use the de-quantified unit name (ONE pod, not "Pods (x6)") so a
            # group-count suffix never asks the model for multiple figures.
            style = ((show.read_bible() or {}).get("style_guide") or "").strip()
            prompt = prompts.character_ref_prompt(unit, canon, style)
            ACTIVITY[show.show_id] = {"detail": f"Ref pass: rendering ref for {name} (Krea 2)…",
                                      "ts": time.time()}
            try:
                wf = load_workflow(wf_path)

                def _render(name=name, out=out, prompt=prompt, wf=wf) -> bool:
                    generate_keyframe(client, wf, prompt, 0, str(out),
                                      aspect_ratio="3:4", style_ref=_style_ref(show))
                    return out.exists()

                gate = render_until_single(_render, out, llm=llm, model=vmodel,
                                           label=f"base ref {name}")
                if not gate["ok"]:
                    raise RuntimeError("krea2 render produced no image")
                (refs_dir / "refs.json").write_text(
                    json.dumps({"status": "real", "refs": [out.name],
                                "variants": {"base": out.name}}),
                    encoding="utf-8")
                created.append(name)
            except Exception as exc:
                ACTIVITY.setdefault(show.show_id, {})["detail"] = (
                    f"Ref pass {name} failed: {exc}")
    finally:
        if stop:
            stop()
    return created
