"""Gate 0 as staged board cards, each with its own human approval.

ideas      (LLM): 5 pitches off the brief, all archived (pick = concept approved)
bible      (LLM): series bible from the approved concept
characters (LLM): character proposals from the approved bible
scenes     (LLM): scene registry from the approved bible + cast
images (Comfy): character refs + scene refs
voices (TTS):   voice samples

The legacy single-shot ``plan`` domain is kept for shows minted before the
partition (their cards still decide/approve through it).
"""
from __future__ import annotations

from typing import Any

from .cards import new_card

DOMAINS = ("ideas", "bible", "characters", "scenes",
           "plan", "images", "voices")

#: card kind -> gate0 domain (decide/approve routing).
KIND_DOMAINS = {
    "gate0_ideas": "ideas",
    "gate0_bible": "bible",
    "gate0_characters": "characters",
    "gate0_scenes": "scenes",
    "gate0_plan": "plan",
    "gate0_images": "images",
    "gate0_voices": "voices",
}


def card_id(show_id: str, domain: str) -> str:
    return f"g0.{show_id}.{domain}"


def bootstrap_cards(show_id: str, brief: str = "",
                    auto: bool = False, n_ideas: int = 5,
                    auto_id: bool = False) -> list[dict[str, Any]]:
    ideas = card_id(show_id, "ideas")
    bible = card_id(show_id, "bible")
    characters = card_id(show_id, "characters")
    scenes = card_id(show_id, "scenes")
    return [
        new_card(
            ideas, kind="gate0_ideas", title="Pitch 5 show ideas off the brief",
            show_id=show_id, role="gate0_ideas", reviewer="human",
            lane="llm", priority=10, max_attempts=3,
            payload={"brief": brief, "n": n_ideas, "auto": bool(auto),
                     "auto_id": bool(auto_id)},
        ),
        new_card(
            bible, kind="gate0_bible", title="Write the series bible",
            show_id=show_id, role="gate0_bible", reviewer="human",
            lane="llm", priority=9, max_attempts=3,
            payload={"auto": bool(auto)}, depends_on=[ideas],
        ),
        new_card(
            characters, kind="gate0_characters",
            title="Propose the cast from the approved bible",
            show_id=show_id, role="gate0_characters", reviewer="human",
            lane="llm", priority=9, max_attempts=3,
            payload={"auto": bool(auto)}, depends_on=[bible],
        ),
        new_card(
            scenes, kind="gate0_scenes",
            title="Build the scene registry from the approved bible",
            show_id=show_id, role="gate0_scenes", reviewer="human",
            lane="llm", priority=9, max_attempts=3,
            payload={"auto": bool(auto)}, depends_on=[characters],
        ),
        new_card(
            card_id(show_id, "images"), kind="gate0_images",
            title="Draw character & location references", show_id=show_id,
            role="gate0_images", reviewer="human", lane="image",
            priority=8, max_attempts=2, depends_on=[scenes],
        ),
        new_card(
            card_id(show_id, "voices"), kind="gate0_voices",
            title="Record voice samples for the cast", show_id=show_id,
            role="gate0_voices", reviewer="human", lane="audio",
            priority=7, max_attempts=2, depends_on=[scenes],
        ),
    ]


def pending_for(state: dict[str, Any] | None, domain: str,
                ideas: dict[str, Any] | None = None) -> dict[str, Any] | None:
    st = state or {}
    if domain == "ideas":
        pitches = (ideas or {}).get("pitches") or []
        if pitches and not any(p.get("status") == "selected" for p in pitches):
            return {"step": "ideas"}
        return None
    if domain == "bible":
        if (st.get("bible") or {}).get("status") == "pending":
            return {"step": "bible"}
        return None
    if domain == "characters":
        for ch in st.get("characters") or []:
            if ch.get("proposal") not in ("approved",):
                return {"step": "character", "char": ch.get("name")}
        return None
    if domain == "scenes":
        if (st.get("scenes") or {}).get("status") == "pending":
            return {"step": "scenes"}
        return None
    if domain == "plan":
        if (st.get("concept") or {}).get("status") == "pending":
            return {"step": "concept"}
        if (st.get("bible") or {}).get("status") == "pending":
            return {"step": "bible"}
        for ch in st.get("characters") or []:
            if ch.get("proposal") not in ("approved",):
                return {"step": "character", "char": ch.get("name")}
        if (st.get("scenes") or {}).get("status") == "pending":
            return {"step": "scenes"}
        return None
    if domain == "images":
        for ch in st.get("characters") or []:
            if ch.get("proposal") != "approved":
                continue
            if ch.get("refs") not in ("approved", "", None):
                return {"step": "refs", "char": ch.get("name")}
        return None
    if domain == "voices":
        for ch in st.get("characters") or []:
            if ch.get("proposal") != "approved":
                continue
            if ch.get("voice") not in ("approved", "", None):
                return {"step": "voice", "char": ch.get("name")}
        return None
    return None


def status(show, domain: str | None = None) -> dict[str, Any]:
    state = show.bootstrap_state() or {}
    try:
        ideas = show.read_ideas() or {}
    except Exception:
        ideas = {}
    domains = {d: pending_for(state, d, ideas) for d in DOMAINS}
    pitches = ideas.get("pitches") or []
    if domains["images"] is None:
        from .. import scenes
        for sid in show.list_scenes():
            status = str((scenes.load_refs(show, sid) or {}).get("status") or "")
            if status in ("rejected", "pending"):
                domains["images"] = {"step": "scene", "scene": sid}
                break
    pending = domains.get(domain) if domain else (
        domains["plan"] or domains["images"] or domains["voices"])
    return {
        "complete": bool(state.get("complete")),
        "domain": domain or "",
        "pending": pending,
        "pending_by_domain": domains,
        "statuses": {
            name: (state.get(name) or {}).get("status", "")
            for name in ("concept", "bible", "scenes")
        },
        "ideas": {
            "total": len(pitches),
            "proposed": sum(1 for p in pitches if p.get("status") == "proposed"),
            "selected": sum(1 for p in pitches if p.get("status") == "selected"),
            "active": next((p.get("id") for p in pitches
                            if p.get("status") == "selected"), ""),
        },
        "characters": [
            {"name": c.get("name"), "proposal": c.get("proposal"),
             "refs": c.get("refs"), "voice": c.get("voice")}
            for c in state.get("characters") or []
        ],
    }
