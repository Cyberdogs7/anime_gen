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
    """Persist an LLM-approved new plotline into the BIBLE (canon) only.

    Continuity is updated separately by the caller (commit_episode_plotlines) at
    PLAN APPROVAL, so a rejected/regenerated episode never pollutes continuity.
    """
    plotline = {k: v for k, v in (plotline or {}).items() if k in (
        "id", "name", "characters", "summary", "status")}
    plotline.setdefault("status", "active")
    plotline.setdefault("last_seen_episode", episode)
    bible = show.read_bible()
    if plotline.get("id") not in _plotline_ids(bible):
        bible.setdefault("plotlines", []).append(plotline)
        show.write_bible(bible)
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

    NOTE: this persists immediately (bible + continuity + characters). The plan
    pipeline uses it only AFTER the plan is approved; see
    commit_plotline_on_approval.
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
    _persist_continuity(show, plotline, episode)
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


def _persist_continuity(show: Show, plotline: dict[str, Any], episode: int) -> None:
    """Append a newly approved plotline to continuity + mark it as seen."""
    cont = show.read_continuity()
    cont.setdefault("plotlines", []).append(plotline)
    cont["last_new_plotline_episode"] = episode
    show.write_continuity(cont)


def commit_plotline_on_approval(show: Show, featured_ids: list[str],
                                new_plotline: dict[str, Any] | None, episode: int,
                                llm=None, cfg=None, episode_type: str = "",
                                arc_lengths: dict[str, int] | None = None,
                                new_arc_length: int | None = None) -> None:
    """Commit plotline bookkeeping ONLY when an episode plan is APPROVED.

    Runs the persistence that used to happen inside develop_episode — stamping
    last_seen on featured plotlines, applying the ARC/COOLDOWN roll, and
    persisting a reviewed new plotline (bible canon + continuity + character
    sheets) — but only after the outline has been approved. A rejected or deleted
    episode therefore never mutates continuity, so regenerating EP01 cannot keep
    re-growing the same plotlines.

    Arc bookkeeping (from ``roll_episode_plotlines``):
    - A plotline just rolled into an arc gets ``arc_remaining = length - 1`` (the
      current episode consumed one).
    - A plotline that was already running gets its ``arc_remaining`` decremented.
    - When ``arc_remaining`` hits 0 the thread enters a cooldown (no longer
      selectable) for ``cooldown_episodes``.
    - Every plotline's ``cooldown_remaining`` ticks down by 1 each approved
      episode; when it reaches 0 the thread is eligible again (a fresh arc length
      is rolled the next time it is selected).
    - A newly invented plotline (NEW roll) is persisted with its rolled arc.

    Also records ``last_battle_episode`` when a battle episode is approved so the
    battle cadence throttle in development knows how recently combat aired.
    """
    from .development import _overall_state, _plotline_state, _record_seen
    cfg = cfg or get_config()
    cooldown_episodes = int(cfg.get("growth", "cooldown_episodes", 4) or 4)
    arc_lengths = arc_lengths or {}
    bible = show.read_bible()
    continuity = show.read_continuity()
    plotlines = _plotline_state(show, bible)
    overall = _overall_state(show, bible)
    data = {"featured_plotlines": [{"id": i} for i in featured_ids],
            "new_plotline": new_plotline}
    _record_seen(show, continuity, episode, data, plotlines, overall)

    # Apply the arc/cooldown roll for every plotline.
    cont = show.read_continuity()
    by_id = {p.get("id"): p for p in cont.get("plotlines", []) or []}
    featured_set = set(featured_ids)
    for pid, p in by_id.items():
        # Tick down any active cooldown.
        cd = int(p.get("cooldown_remaining", 0) or 0)
        if cd > 0:
            p["cooldown_remaining"] = max(0, cd - 1)
        else:
            p.setdefault("cooldown_remaining", 0)
        # Feature this thread this episode: start/continue its arc.
        if pid in featured_set:
            if pid in arc_lengths:
                # Freshly rolled this episode: length - 1 episodes remain.
                p["arc_remaining"] = max(0, int(arc_lengths[pid]) - 1)
            else:
                # Already running: consume one episode.
                p["arc_remaining"] = max(0, int(p.get("arc_remaining", 0) or 0) - 1)
            if int(p.get("arc_remaining", 0) or 0) <= 0:
                p["cooldown_remaining"] = cooldown_episodes
    # A NEW plotline that was invented gets its own arc applied.
    if isinstance(new_plotline, dict) and new_plotline.get("id"):
        nid = new_plotline["id"]
        if nid in by_id:
            by_id[nid]["arc_remaining"] = max(0, int(new_arc_length or 1) - 1)
            by_id[nid]["cooldown_remaining"] = 0
    cont["plotlines"] = list(by_id.values())
    if str(episode_type or "").lower() == "battle":
        cont["last_battle_episode"] = episode
    show.write_continuity(cont)

    # Persist the new plotline to the bible (canon) + its new characters, but only
    # if it wasn't already persisted by a prior approval (idempotent).
    if isinstance(new_plotline, dict) and new_plotline.get("id"):
        if new_plotline["id"] not in _plotline_ids(show.read_bible()):
            np = approve_new_plotline(show, new_plotline, episode)
            np["arc_remaining"] = max(0, int(new_arc_length or 1) - 1)
            np["cooldown_remaining"] = 0
            names = [n for n in (np.get("characters") or []) if n]
            existing = {c.get("name") for c in
                        (show.read_character(cid) for cid in show.list_characters())}
            for n in [x for x in names if x not in existing][:2]:
                generate_new_character(show, n, np, episode, llm=llm, cfg=cfg)
