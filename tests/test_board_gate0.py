"""Gate 0 split: pending order per domain, card shape, human routing."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.board import daemon as daemon_mod
from studio.board.agent import AgentResult
from studio.board.daemon import BoardDaemon
from studio.board.gate0 import bootstrap_cards, pending_for
from studio.board.store import BoardStore
from studio.config import Config


def test_pending_per_domain():
    st = {"concept": {"status": "pending"}}
    assert pending_for(st, "plan") == {"step": "concept"}
    assert pending_for(st, "images") is None
    st = {"concept": {"status": "approved"}, "bible": {"status": "pending"}}
    assert pending_for(st, "plan") == {"step": "bible"}
    st = {"concept": {"status": "approved"}, "bible": {"status": "approved"},
          "characters": [{"name": "Aki", "proposal": "pending",
                          "refs": "", "voice": ""}]}
    assert pending_for(st, "plan") == {"step": "character", "char": "Aki"}
    st["characters"][0]["proposal"] = "approved"
    st["characters"][0]["refs"] = "real"
    st["scenes"] = {"status": "approved"}
    assert pending_for(st, "images") == {"step": "refs", "char": "Aki"}
    assert pending_for(st, "voices") is None
    st["characters"][0]["refs"] = "approved"
    st["characters"][0]["voice"] = "pending"
    assert pending_for(st, "voices") == {"step": "voice", "char": "Aki"}
    st["characters"][0]["voice"] = "approved"
    assert pending_for(st, "plan") is None
    assert pending_for(st, "images") is None
    assert pending_for(st, "voices") is None


def test_bootstrap_cards_are_three_with_plan_dependency():
    cards = bootstrap_cards("demo", brief="a heist", auto=True)
    assert [c["id"] for c in cards] == [
        "g0.demo.plan", "g0.demo.images", "g0.demo.voices"]
    assert cards[0]["reviewer"] == "human"
    assert cards[0]["payload"] == {"brief": "a heist", "auto": True}
    assert cards[1]["depends_on"] == ["g0.demo.plan"]
    assert cards[2]["depends_on"] == ["g0.demo.plan"]
    assert [c["lane"] for c in cards] == ["llm", "image", "audio"]


class FakeAgent:
    def run(self):
        return AgentResult(status="verified")

    def system_prompt(self):
        return ""


def test_human_card_routes_to_reviewing_and_is_not_launched(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "write_trace", lambda *a, **k: tmp_path)
    store = BoardStore(tmp_path / "b.db")
    store.create(bootstrap_cards("demo")[0])
    cfg = Config(ROOT)
    d = BoardDaemon(store, cfg, agent_factory=lambda *a: FakeAgent())
    d.tick()
    deadline = time.time() + 5
    while time.time() < deadline and store.get("g0.demo.plan")["state"] != "agent_verified":
        time.sleep(0.02)
    assert store.get("g0.demo.plan")["state"] == "agent_verified"
    d.tick()
    assert store.get("g0.demo.plan")["state"] == "reviewing"
    d.tick()
    assert store.get("g0.demo.plan")["state"] == "reviewing"
    assert not [r for r in store.runs(card_id="g0.demo.plan") if r["phase"] == "review"]
