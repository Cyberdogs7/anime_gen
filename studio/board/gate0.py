"""Gate 0 as three board cards, one GPU service each.

plan   (LLM):   concept -> bible -> character proposals -> scene registry
images (Comfy): character refs + scene refs
voices (TTS):   voice samples
"""
from __future__ import annotations

from typing import Any

from .cards import new_card

DOMAINS = ("plan", "images", "voices")


def card_id(show_id: str, domain: str) -> str:
    return f"g0.{show_id}.{domain}"


def bootstrap_cards(show_id: str, brief: str = "",
                    auto: bool = False) -> list[dict[str, Any]]:
    plan = card_id(show_id, "plan")
    return [
        new_card(
            plan, kind="gate0_plan", title="Build the show bible, cast & scenes",
            show_id=show_id, role="gate0_plan", reviewer="human",
            lane="llm", priority=9, max_attempts=1,
            payload={"brief": brief, "auto": bool(auto)},
        ),
        new_card(
            card_id(show_id, "images"), kind="gate0_images",
            title="Draw character & location references", show_id=show_id,
            role="gate0_images", reviewer="human", lane="image",
            priority=8, max_attempts=2, depends_on=[plan],
        ),
        new_card(
            card_id(show_id, "voices"), kind="gate0_voices",
            title="Record voice samples for the cast", show_id=show_id,
            role="gate0_voices", reviewer="human", lane="audio",
            priority=7, max_attempts=2, depends_on=[plan],
        ),
    ]


def pending_for(state: dict[str, Any] | None, domain: str) -> dict[str, Any] | None:
    st = state or {}
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
    domains = {d: pending_for(state, d) for d in DOMAINS}
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
        "characters": [
            {"name": c.get("name"), "proposal": c.get("proposal"),
             "refs": c.get("refs"), "voice": c.get("voice")}
            for c in state.get("characters") or []
        ],
    }
