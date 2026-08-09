"""Series growth: LLM-reviewed introduction of new plotlines and new cast.

Development stage may propose a ``new_plotline`` (possibly with NEW characters).
This module runs an LLM reviewer that AUTO-APPROVES or rejects the proposal against
the bible, then persists the approved plotline to the BIBLE (canon) and generates
a full character sheet + ref image for each newly-introduced character.

Voice is NOT synthesized for new characters: there is no audio-input model, so the
sheet carries the voice spec (mode=manual) and the human supplies a sample later.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from . import prompts
from .bootstrap import ACTIVITY
from .config import get_config
from .show import Show

log = logging.getLogger(__name__)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (text or "").strip().lower()).strip("-")


# ---------------------------------------------------------------------------
# LLM review gate
# ---------------------------------------------------------------------------

def review_new_plotline(show: Show, proposal: dict[str, Any], episode: int,
                        llm=None, cfg=None) -> dict[str, Any]:
    """Auto-approve/reject a proposed new plotline against the series bible.

    Returns {"approved": bool, "notes": [...], "plotline": {…}} where ``plotline``
    is the reviewed/refined proposal (may include a ``name`` the LLM tightens).
    """
    from .clients.lmstudio import LMStudioClient
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
    bible = show.read_bible()
    existing = _plotline_ids(bible)
    cast = [c.get("name") for c in (show.read_character(cid) for cid in show.list_characters())
            if c.get("name")] or (bible.get("cast") and [c.get("name") for c in bible["cast"]] or [])
    roles = cfg.get("llm", "roles", {})
    model = roles.get("growth_reviewer") or roles.get("showrunner") or cfg.get("llm", "model")
    system = prompts.showrunner_system(cfg["show_profile"], bible.get("content_policy", "mature"),
                                       cfg["show_profile"].get("baseline", "ranma-1-2"))
    user = prompts.new_plotline_review_prompt(bible, existing, cast, proposal, episode)
    data = llm.chat_json([{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                         model=model, temperature=0.2, max_tokens=4096)
    if not isinstance(data, dict):
        data = {}
    approved = bool(data.get("approved"))
    if approved and isinstance(data.get("plotline"), dict):
        proposal = data["plotline"]
    return {"approved": approved,
            "notes": data.get("notes", []) or [],
            "plotline": proposal}


def review_new_character(show: Show, name: str, plotline: dict[str, Any],
                         episode: int, llm=None, cfg=None) -> dict[str, Any]:
    """Auto-approve/reject a proposed new character sheet against the bible + plotline."""
    from .clients.lmstudio import LMStudioClient
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
    bible = show.read_bible()
    cast = [c.get("name") for c in (show.read_character(cid) for cid in show.list_characters())
            if c.get("name")] or (bible.get("cast") and [c.get("name") for c in bible["cast"]] or [])
    roles = cfg.get("llm", "roles", {})
    model = roles.get("growth_reviewer") or roles.get("showrunner") or cfg.get("llm", "model")
    system = prompts.showrunner_system(cfg["show_profile"], bible.get("content_policy", "mature"),
                                       cfg["show_profile"].get("baseline", "ranma-1-2"))
    user = prompts.new_character_review_prompt(bible, cast, name, plotline, episode)
    data = llm.chat_json([{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                         model=model, temperature=0.2, max_tokens=2048)
    if not isinstance(data, dict):
        data = {}
    return {"approved": bool(data.get("approved")), "notes": data.get("notes", []) or []}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _plotline_ids(bible: dict[str, Any]) -> list[str]:
    return [p.get("id") for p in (bible.get("plotlines", []) or []) if p.get("id")]


def approve_new_plotline(show: Show, plotline: dict[str, Any], episode: int) -> dict[str, Any]:
    """Persist an LLM-approved new plotline into the BIBLE (canon) + continuity."""
    plotline = {k: v for k, v in (plotline or {}).items() if k in (
        "id", "name", "characters", "summary", "status")}
    plotline.setdefault("status", "active")
    plotline.setdefault("last_seen_episode", episode)
    bible = show.read_bible()
    if plotline.get("id") not in _plotline_ids(bible):
        bible.setdefault("plotlines", []).append(plotline)
        show.write_bible(bible)
    cont = show.read_continuity()
    cont.setdefault("plotlines", []).append(plotline)
    cont["last_new_plotline_episode"] = episode
    show.write_continuity(cont)
    return plotline


def generate_new_character(show: Show, name: str, plotline: dict[str, Any],
                           episode: int, llm=None, cfg=None) -> dict[str, Any] | None:
    """Propose, LLM-review, and persist ONE new character for a plotline.

    Voice is written as mode=manual (no audio-input model; human supplies the sample).
    Returns the sheet on success, None if rejected by the reviewer.
    """
    from .clients.lmstudio import LMStudioClient
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
    bible = show.read_bible()
    existing = [show.read_character(cid) for cid in show.list_characters()]
    cast = [c.get("name") for c in existing if c.get("name")] or (
        bible.get("cast") and [c.get("name") for c in bible["cast"]] or [])
    roles = cfg.get("llm", "roles", {})
    model = roles.get("showrunner") or cfg.get("llm", "model")
    system = prompts.showrunner_system(cfg["show_profile"], bible.get("content_policy", "mature"),
                                       cfg["show_profile"].get("baseline", "ranma-1-2"))
    ACTIVITY[show.show_id] = {"detail": f"Growth: proposing new character {name}…",
                              "ts": time.time()}
    proposal = llm.chat_json(
        [{"role": "system", "content": system},
         {"role": "user", "content": prompts.new_character_prompt(
             bible, cast, name, plotline)}],
        model=model, temperature=0.7, max_tokens=4096)
    if not isinstance(proposal, dict) or not proposal.get("name"):
        return None
    proposal["name"] = name
    proposal["id"] = _slug(name)

    verdict = review_new_character(show, name, plotline, episode, llm=llm, cfg=cfg)
    if not verdict["approved"]:
        ACTIVITY[show.show_id] = {"detail": f"Growth: {name} rejected by reviewer "
                                   "({'; '.join(verdict['notes'][:2]) or 'see log'})",
                                  "ts": time.time()}
        log.warning("new character %s rejected: %s", name, verdict["notes"])
        return None

    proposal.setdefault("role", "episode supporting")
    proposal.setdefault("appearance_canonical", "")
    proposal.setdefault("personality", [])
    proposal.setdefault("traits_for_llm", "")
    proposal["voice"] = {"mode": "manual", "speaker": None,
                         "voice_description": (proposal.get("voice") or {}).get(
                             "voice_description", "")}
    show.write_character(proposal)
    _generate_ref(show, proposal, cfg)
    log.info("growth: approved + persisted new character %s (voice=manual)", name)
    return proposal


def _generate_ref(show: Show, char: dict[str, Any], cfg=None) -> None:
    """Generate the Krea 2 ref portrait for a new character (best effort)."""
    cfg = cfg or get_config()
    krea2 = cfg.get("env", "comfyui", {}).get("krea2", {}) or {}
    if not krea2.get("url"):
        return
    try:
        from .remote import ServiceOps
        name = char.get("name", "")
        canon = (char.get("appearance_canonical") or "").strip()
        prompt = (f"Anime character reference portrait of {name}. {canon}".rstrip(" .")
                  + ". Full body, front view, neutral standing pose, plain studio "
                    "background, clean lineart, consistent character design, high quality.")
        rd = show.character_refs_dir(char.get("id", ""))
        rd.mkdir(parents=True, exist_ok=True)
        out = rd / f"{char.get('id', 'char')}_ref_01.png"
        res = ServiceOps(cfg).generate_image(prompt, seed=0, aspect_ratio="3:4",
                                             out_path=str(out))
        if res.get("ok") and out.exists():
            (rd / "refs.json").write_text(json.dumps(
                {"status": "real", "refs": [out.name], "seed": res.get("seed", 0)},
                ensure_ascii=False), encoding="utf-8")
        else:
            (rd / "refs.json").write_text(
                '{"status": "stub", "note": "Krea 2 ref generation unavailable"}',
                encoding="utf-8")
    except Exception as exc:
        log.warning("new character ref generation failed for %s: %s",
                    char.get("id"), exc)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_new_plotline(show: Show, proposal: dict[str, Any], episode: int,
                         llm=None, cfg=None) -> dict[str, Any]:
    """Full growth path for a proposed new plotline: review -> approve -> persist
    -> create its new characters. Returns {"approved", "plotline", "characters",
    "notes"}.
    """
    verdict = review_new_plotline(show, proposal, episode, llm=llm, cfg=cfg)
    if not verdict["approved"]:
        ACTIVITY[show.show_id] = {"detail": "Growth: new plotline rejected by reviewer "
                                   f"({'; '.join(verdict['notes'][:2]) or 'see log'})",
                                  "ts": time.time()}
        log.warning("new plotline rejected for %s: %s",
                    show.show_id, verdict["notes"])
        return {"approved": False, "plotline": proposal,
                "characters": [], "notes": verdict["notes"]}

    plotline = approve_new_plotline(show, verdict["plotline"], episode)
    names = [n for n in (plotline.get("characters") or []) if n]
    existing = {c.get("name") for c in
                (show.read_character(cid) for cid in show.list_characters())}
    new_names = [n for n in names if n not in existing]
    created = []
    for n in new_names[:2]:
        sheet = generate_new_character(show, n, plotline, episode, llm=llm, cfg=cfg)
        if sheet:
            created.append(sheet)
    return {"approved": True, "plotline": plotline, "characters": created,
            "notes": verdict["notes"]}
