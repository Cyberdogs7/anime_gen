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

from . import fsutil, prompts, refs
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


def _revise_appearance(llm, model: str, name: str, current: str, notes: str,
                       bible: dict[str, Any] | None = None, style: str = "") -> str:
    """LLM rewrites ONLY the appearance_canonical from rejection feedback."""
    world = (f"Series bible (world, tone, style): {json.dumps(bible, ensure_ascii=False)}\n"
             if bible else "")
    text = llm.chat([
        {"role": "user", "content":
         f"Revise this anime entity's canonical appearance description to address "
         f"the director's feedback. Keep everything the feedback did not ask to change.\n"
         f"Subject: {name}\n"
         f"{world}"
         + (f"Visual style guide: {style}\n" if style else "")
         + f"CURRENT APPEARANCE:\n{current}\n"
         f"DIRECTOR'S FEEDBACK:\n{notes}\n"
         "If the feedback indicates the subject is NOT what it was (e.g. it is a "
         "group or squadron rather than one person, or a machine rather than a human), "
         "correct the subject itself, not just its details.\n"
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
    """Feedback-driven regeneration of a character's REFERENCE.

    Same split as the "refs" reject: if the character has an APPROVED nude, notes
    revise the OUTFIT and the clothed canonical (base) is re-rendered from that nude
    (the nude is kept); otherwise there is no base yet, so the nude identity root is
    re-rolled. Notes never silently re-roll the nude when a base exists.
    """
    cfg = cfg or get_config()
    cid = next((c for c in show.list_characters()
                if _unit_key((show.read_character(c).get("name") or "")) == _unit_key(name)),
               None)
    if not cid:
        return False
    c = show.read_character(cid)
    # Notes on the character's BASE apply ONLY to the base (its outfit). Never
    # re-roll the nude from here — that has its own Regenerate.
    return refs.regenerate_base(show, cid, c, cfg=cfg, llm=llm, notes=notes)


def _ask_appearance(llm, model: str, name: str, context: str,
                    bible: dict[str, Any] | None = None, style: str = "",
                    role: str = "") -> str:
    """Write a rich, image-prompt-style appearance_canonical for a supporting
    entity — a person, or a non-human subject (a machine/mech, a creature, or a
    group/squadron of units).

    The writer is given the series bible (world, tone, style), the entity's role,
    and how the episode depicts it, and must infer what the entity actually IS
    BEFORE describing it — so the reference image shows the correct subject for ANY
    show, with no human default. Same appearance discipline the main cast uses.
    """
    world = json.dumps(bible, ensure_ascii=False) if bible else "(no bible provided)"
    text = llm.chat([
        {"role": "user", "content":
         f"Design the canonical on-screen look of ONE visual subject for an anime "
         f"reference image.\n"
         f"SUBJECT NAME: {name}\n"
         f"ROLE: {role or 'supporting entity'}\n"
         f"HOW THE EPISODE DEPICTS IT: {context or '(not described in the script)'}\n\n"
         "From the name, the world and the depiction above, decide what this subject "
         "ACTUALLY is — a person, a machine/mech/vehicle, a creature, a group or "
         "squadron of units, a faction, or a prop — and describe THAT. Do not assume a "
         "human, and do not turn a group/squadron/faction into one person.\n\n"
         f"Series bible (world, tone, style): {world}\n"
         + (f"Visual style guide: {style}\n" if style else "")
         + "\n" + prompts.appearance_canonical_rules() + "\n\n"
         "MANDATORY: name the SINGLE canonical subject the reference image must show, "
         "matching what the entity really is in this world. A person is described in "
         "one outfit; a non-person is described as itself (form, materials, colors, "
         "markings, scale).\n\n"
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
    # Everything the appearance writer needs to identify the SUBJECT correctly for
    # ANY show: the world/tone/style (bible), the episode's story (plan plot), and
    # how the script depicts this entity. No show-specific assumptions.
    bible = show.read_bible() or {}
    style = (bible.get("style_guide") or "").strip()
    plan_plot = (plan.get("plot") or "").strip()

    def _entity_context(nm: str) -> str:
        ctx = _character_context(script, nm)
        if plan_plot:
            ctx = (ctx + " " + plan_plot).strip()
        return ctx[:1400]

    def _entity_role(nm: str) -> str:
        return ("threat of the week" if threat and nm.lower() == threat.lower()
                else "episode supporting entity")

    approved = _approved_names(show)
    approved_units = {_unit_key(n) for n in approved}
    missing = [name for name in cast
               if _unit_key(name) not in approved_units
               and looks_like_character_name(name)][:max_new]
    if not missing:
        return []

    created: list[str] = []
    for name in missing:
        # Group-count names ("Pods (x6)") are normalized to ONE unit so the sheet +
        # ref describe a single pod and the scene decides the count.
        unit = _unit_name(name)
        slug = re.sub(r"[^a-z0-9-]+", "-", unit.lower()).strip("-") or "char"
        sheet_path = show.characters_dir / f"{slug}.yaml"
        # A sheet may exist with no ref. Reuse its appearance_canonical; only WRITE a
        # new sheet when none exists.
        if sheet_path.exists():
            existing = show.read_character(slug)
            canon = (existing.get("appearance_canonical") or "").strip()
            if not canon:
                ACTIVITY[show.show_id] = {"detail": f"Ref pass: writing appearance for {name}…",
                                          "ts": time.time()}
                canon = _ask_appearance(llm, model, unit, _entity_context(name),
                                        bible=bible, style=style, role=_entity_role(name))
                if not canon:
                    continue
                existing["appearance_canonical"] = canon
                show.write_character(existing)
        else:
            ACTIVITY[show.show_id] = {"detail": f"Ref pass: writing appearance for {name}…",
                                      "ts": time.time()}
            canon = _ask_appearance(llm, model, unit, _entity_context(name),
                                    bible=bible, style=style, role=_entity_role(name))
            if not canon:
                continue
            show.write_character({"id": slug, "name": unit, "role": "episode supporting",
                                  "appearance_canonical": canon})
        # ONE pipeline (refs): nude identity root -> clothed canonical.
        ACTIVITY[show.show_id] = {"detail": f"Ref pass: refs for {name} (nude → outfit)…",
                                  "ts": time.time()}
        try:
            if refs.generate_refs(show, slug, show.read_character(slug), cfg=cfg, llm=llm):
                created.append(name)
            else:
                ACTIVITY.setdefault(show.show_id, {})["detail"] = f"Ref pass {name} failed"
        except Exception as exc:
            ACTIVITY.setdefault(show.show_id, {})["detail"] = f"Ref pass {name} failed: {exc}"
    return created
