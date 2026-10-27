"""Event catalog + local audit log (no broker).

The old bus is gone. Gate-0 approval events are still useful as an append-only
record, so they are written to ``shows/<id>/events.jsonl`` (or
``runs/events.jsonl`` for show-less events). Nothing consumes them.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- approval chain (bootstrap + runtime gates) ---
CONCEPT_PENDING, CONCEPT_APPROVED, CONCEPT_REJECTED = "ConceptPending", "ConceptApproved", "ConceptRejected"
BIBLE_PENDING, BIBLE_APPROVED, BIBLE_REJECTED = "BiblePending", "BibleApproved", "BibleRejected"
CHAR_PROPOSAL_PENDING, CHAR_PROPOSAL_APPROVED, CHAR_PROPOSAL_REJECTED = (
    "CharacterProposalPending", "CharacterProposalApproved", "CharacterProposalRejected",
)
SCENE_REGISTRY_PENDING, SCENE_REGISTRY_APPROVED, SCENE_REGISTRY_REJECTED = (
    "SceneRegistryPending", "SceneRegistryApproved", "SceneRegistryRejected",
)
CHAR_REFS_PENDING, CHAR_REFS_APPROVED, CHAR_REFS_REJECTED = (
    "CharacterRefsPending", "CharacterRefsApproved", "CharacterRefsRejected",
)
VOICE_SAMPLE_PENDING, VOICE_APPROVED, VOICE_REJECTED = (
    "VoiceSamplePending", "VoiceApproved", "VoiceRejected",
)
COSTUME_APPROVED, COSTUME_REJECTED = "CostumeApproved", "CostumeRejected"
OBJECT_REF_PENDING, OBJECT_REF_APPROVED, OBJECT_REF_REJECTED = (
    "ObjectRefPending", "ObjectRefApproved", "ObjectRefRejected",
)
BOOTSTRAP_COMPLETE = "BootstrapComplete"
PLAN_PENDING, PLAN_APPROVED, PLAN_REJECTED = "PlanPending", "PlanApproved", "PlanRejected"
SCRIPT_PENDING, SCRIPT_APPROVED, SCRIPT_REJECTED = "ScriptPending", "ScriptApproved", "ScriptRejected"
SHOT_PENDING, SHOT_APPROVED, SHOT_REJECTED = "ShotPending", "ShotApproved", "ShotRejected"
EPISODE_PENDING, EPISODE_APPROVED, EPISODE_REJECTED = "EpisodePending", "EpisodeApproved", "EpisodeRejected"

# --- delivery / system ---
EPISODE_DELIVERED = "EpisodeDelivered"
CONTINUITY_UPDATED = "ContinuityUpdated"
BUDGET_EXHAUSTED = "BudgetExhausted"
NODE_UP, NODE_DOWN = "NodeUp", "NodeDown"
HEARTBEAT = "Heartbeat"
TEST = "Test"

_LOCK = threading.Lock()


@dataclass
class Event:
    type: str
    show_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    run_id: str = ""
    episode: int = 0
    source_node: str = ""
    correlation_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "type": self.type, "ts": self.ts,
            "run_id": self.run_id, "show_id": self.show_id,
            "episode": self.episode, "source_node": self.source_node,
            "correlation_id": self.correlation_id, "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def new_event(type_: str, show_id: str = "",
              payload: dict[str, Any] | None = None, **extra: Any) -> Event:
    return Event(type=type_, show_id=show_id, payload=payload or {}, **extra)


def record(event: Event, base_dir: Path | str | None = None) -> None:
    """Append one event to the local audit log. Never raises."""
    if base_dir is None:
        from .config import get_config
        base_dir = get_config().root
    root = Path(base_dir)
    target = (root / "shows" / event.show_id) if event.show_id else (root / "runs")
    try:
        target.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with (target / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(event.to_json() + "\n")
    except Exception:
        pass
