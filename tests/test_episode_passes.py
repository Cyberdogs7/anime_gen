"""Twelve single-responsibility episode passes: chain shape, gates, roles."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.board.agent import AgentResult
from studio.board.cards import new_card
from studio.board.daemon import BoardDaemon, request_episode
from studio.board.roles import load_roles
from studio.board.store import BoardStore
from studio.config import Config

EXPECTED_KINDS = ["outline", "locations", "cast", "outfits", "objects",
                  "narrative", "dialogue", "shots", "camera", "action",
                  "sound", "h3prompts", "keyframes", "render", "qc",
                  "assemble"]

GATED = {"outline", "dialogue", "camera", "h3prompts"}


def test_episode_chain_shape(tmp_path):
    store = BoardStore(tmp_path / "b.db")
    cards = request_episode(store, "demo", 1)
    assert [c["kind"] for c in cards] == EXPECTED_KINDS
    # Linear dependency chain.
    prev = None
    for c in cards:
        deps = c.get("depends_on") or []
        assert deps == ([prev] if prev else []), c["id"]
        prev = c["id"]
    # Every text pass has a producer reviewer; exec tail keeps its tools.
    for c in cards:
        kind = c["kind"]
        if kind in ("keyframes", "qc", "assemble"):
            assert (c.get("payload") or {}).get("tool"), kind
        elif kind == "render":
            # Milestone: expands into per-shot cards, carries no tool.
            assert not (c.get("payload") or {}).get("tool"), kind
        elif kind == "outline":
            assert c["reviewer"] == "outline_reviewer"
        else:
            assert c["reviewer"].endswith("_reviewer"), kind
    # Human gates on exactly the four boundary stages.
    for c in cards:
        has_gate = bool((c.get("payload") or {}).get("human_gate"))
        assert has_gate == (c["kind"] in GATED), c["id"]
    # Idempotent.
    again = request_episode(store, "demo", 1)
    assert len(store.all()) == len(cards)
    assert len(again) == len(cards)


def test_all_pass_roles_resolve(tmp_path):
    store = BoardStore(tmp_path / "b.db")
    cards = request_episode(store, "demo", 1)
    roles = load_roles(Config(ROOT))
    for c in cards:
        assert c["role"] in roles, c["id"]
        reviewer = c.get("reviewer") or ""
        if reviewer and reviewer != "human":
            assert reviewer in roles, c["id"]


def _daemon(store):
    return BoardDaemon(store, Config(ROOT), agent_factory=lambda *a: None)


def _gated_card(store, gated: bool):
    payload = {"human_gate": True} if gated else {}
    card = new_card("x1", kind="dialogue", title="t", role="dialogue_writer",
                    reviewer="dialogue_reviewer", lane="llm",
                    show_id="demo", episode=1, payload=payload)
    store.create(card)
    store.advance("x1", "in_work", "test", "")
    store.advance("x1", "agent_verified", "test", "")
    store.update("x1", claimed_by="run1")
    return store.get("x1")


def test_human_gate_routes_pass_to_reviewing(tmp_path):
    store = BoardStore(tmp_path / "b.db")
    d = _daemon(store)
    d._apply("run1", _gated_card(store, True), None, "review",
             AgentResult(status="verified", verdict="pass", summary="ok"))
    assert store.get("x1")["state"] == "reviewing"


def test_no_gate_routes_pass_to_accepted(tmp_path):
    store = BoardStore(tmp_path / "b.db")
    d = _daemon(store)
    d._apply("run1", _gated_card(store, False), None, "review",
             AgentResult(status="verified", verdict="pass", summary="ok"))
    assert store.get("x1")["state"] == "accepted"


def test_review_fail_still_retakes_with_gate(tmp_path):
    store = BoardStore(tmp_path / "b.db")
    d = _daemon(store)
    d._apply("run1", _gated_card(store, True), None, "review",
             AgentResult(status="verified", verdict="fail", notes="flat"))
    assert store.get("x1")["state"] == "retake"
