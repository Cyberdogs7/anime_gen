"""Event envelope + catalog. See DESIGN.md §6.4."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# Streams (Redis stream keys / logical topics).
STREAM_RUN = "bus:run"
STREAM_DEV = "bus:dev"
STREAM_SCRIPT = "bus:script"
STREAM_KEYFRAMES = "bus:keyframes"
STREAM_SHOTS = "bus:shots"
STREAM_DIALOGUE = "bus:dialogue"
STREAM_ASSEMBLY = "bus:assembly"
STREAM_QC = "bus:qc"
STREAM_APPROVAL = "bus:approval"
STREAM_DELIVERY = "bus:delivery"
STREAM_SYSTEM = "bus:system"
STREAM_DEAD = "bus:dead"

# --- run lifecycle ---
RUN_REQUESTED = "RunRequested"
RUN_STARTED = "RunStarted"
RUN_PROGRESS = "RunProgress"
RUN_ABORTED = "RunAborted"
RUN_COMPLETED = "RunCompleted"

# --- stage events ---
PLAN_READY = "PlanReady"
PLAN_FAILED = "PlanFailed"
SCRIPT_READY = "ScriptReady"
SCRIPT_REVIEW_REQUESTED = "ScriptReviewRequested"
SCRIPT_NOTES_READY = "ScriptNotesReady"
SCRIPT_REVISION_REQUESTED = "ScriptRevisionRequested"
SCRIPT_REVISED = "ScriptRevised"
SCRIPT_FAILED = "ScriptFailed"
KEYFRAMES_READY = "KeyframesReady"
REF_BANK_READY = "RefBankReady"
KEYFRAMES_FAILED = "KeyframesFailed"
SHOT_SCHEDULED = "ShotScheduled"
SHOT_CLAIMED = "ShotClaimed"
SHOT_RENDERED = "ShotRendered"
SHOT_FAILED = "ShotFailed"
RETAKE_REQUESTED = "RetakeRequested"
SHOT_SKIPPED = "ShotSkipped"
DIALOGUE_READY = "DialogueReady"
DIALOGUE_FAILED = "DialogueFailed"
ASSEMBLY_REQUESTED = "AssemblyRequested"
ASSEMBLY_READY = "AssemblyReady"
ASSEMBLY_FAILED = "AssemblyFailed"
QC_STARTED = "QcStarted"
SHOT_QC_PASSED = "ShotQcPassed"
SHOT_QC_FAILED = "ShotQcFailed"
QC_REPORT_READY = "QcReportReady"

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
NODE_UP = "NodeUp"
NODE_DOWN = "NodeDown"
HEARTBEAT = "Heartbeat"
TEST = "Test"  # used by `verify` round-trip


@dataclass
class Event:
    """Every bus message. See DESIGN.md §6.4 envelope."""

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
            "event_id": self.event_id,
            "type": self.type,
            "ts": self.ts,
            "run_id": self.run_id,
            "show_id": self.show_id,
            "episode": self.episode,
            "source_node": self.source_node,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            type=data["type"],
            show_id=data.get("show_id", ""),
            payload=data.get("payload", {}),
            event_id=data.get("event_id", str(uuid.uuid4())),
            ts=data.get("ts", ""),
            run_id=data.get("run_id", ""),
            episode=data.get("episode", 0),
            source_node=data.get("source_node", ""),
            correlation_id=data.get("correlation_id", ""),
        )


def new_event(type_: str, show_id: str = "", payload: dict[str, Any] | None = None,
              **extra: Any) -> Event:
    """Convenience factory."""
    return Event(type=type_, show_id=show_id, payload=payload or {}, **extra)
