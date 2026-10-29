"""Card and lane model for the agent-first studio board.

A card is one unit of work with an owner role, a lane (the concurrency
resource it consumes), a reviewer role, and a state. The board is the single
source of truth: agents never advance their own card, the daemon does it from
the agent's final verdict.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

#: Legal card states.
STATES = (
    "inbox",          # created, not yet planned
    "planned",        # ready for a producer agent
    "in_work",        # producer running
    "agent_verified",  # producer finished with proof
    "reviewing",      # reviewer running
    "accepted",       # reviewer passed (or no reviewer)
    "retake",         # reviewer failed; producer may retry
    "blocked",        # needs a human
    "deferred",       # parked by a human
)

TERMINAL = frozenset({"accepted"})

#: Allowed transitions. A producer may only reach ``agent_verified`` /
#: ``retake`` / ``blocked``; only a reviewer (or the daemon for an unreviewed
#: card) reaches ``accepted``.
LEGAL: dict[str, frozenset[str]] = {
    "inbox": frozenset({"planned", "blocked", "deferred"}),
    "planned": frozenset({"in_work", "blocked", "deferred"}),
    "in_work": frozenset({"agent_verified", "retake", "blocked"}),
    "agent_verified": frozenset({"reviewing", "accepted", "retake", "blocked"}),
    "reviewing": frozenset({"accepted", "retake", "blocked"}),
    "retake": frozenset({"planned", "blocked"}),
    "blocked": frozenset({"planned", "deferred"}),
    "deferred": frozenset({"planned"}),
    # Human reopen: an accepted Gate 0 card can go back for rework when a
    # late issue surfaces. Only the human reject actions drive this; the
    # daemon never moves cards out of accepted.
    "accepted": frozenset({"retake"}),
}

#: Lanes are concurrency resources, not departments: one render at a time on a
#: GPU, N LLM calls at once, etc. Configured in config/agents.yaml.
LANES = ("llm", "image", "video", "audio", "io", "vision")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def can_move(from_state: str, to_state: str) -> bool:
    return to_state in LEGAL.get(from_state, frozenset())


def new_card(
    card_id: str,
    *,
    kind: str,
    title: str,
    role: str,
    lane: str,
    show_id: str = "",
    episode: int = 0,
    reviewer: str = "",
    state: str = "planned",
    priority: int = 3,
    payload: dict[str, Any] | None = None,
    units: list[dict[str, Any]] | None = None,
    depends_on: list[str] | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Build a normalized card dict."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    if lane not in LANES:
        raise ValueError(f"unknown lane {lane!r}")
    ts = now()
    return {
        "id": card_id,
        "kind": kind,
        "title": title,
        "show_id": show_id,
        "episode": int(episode or 0),
        "role": role,
        "reviewer": reviewer,
        "lane": lane,
        "state": state,
        "priority": int(priority),
        "payload": dict(payload or {}),
        "result": {},
        "artifacts": [],
        "notes": [],
        "units": list(units or []),
        "depends_on": list(depends_on or []),
        "attempts": 0,
        "max_attempts": int(max_attempts),
        "claimed_by": None,
        "claimed_at": None,
        "created": ts,
        "updated": ts,
    }


def validate(card: dict[str, Any]) -> dict[str, Any]:
    """Raise on a malformed card; return it unchanged otherwise."""
    for key in ("id", "kind", "role", "lane", "state"):
        if not str(card.get(key) or "").strip():
            raise ValueError(f"card missing {key}")
    if card["state"] not in STATES:
        raise ValueError(f"unknown state {card['state']!r}")
    if card["lane"] not in LANES:
        raise ValueError(f"unknown lane {card['lane']!r}")
    return card
