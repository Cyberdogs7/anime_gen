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


def test_bootstrap_cards_are_staged_with_idea_picks_first():
    cards = bootstrap_cards("demo", brief="a heist", auto=True)
    assert [c["id"] for c in cards] == [
        "g0.demo.ideas", "g0.demo.bible", "g0.demo.characters",
        "g0.demo.scenes", "g0.demo.images", "g0.demo.voices"]
    assert cards[0]["reviewer"] == "human"
    assert cards[0]["payload"] == {"brief": "a heist", "n": 5, "auto": True,
                                  "auto_id": False}
    auto = bootstrap_cards("demo2", brief="b", auto_id=True)
    assert auto[0]["payload"]["auto_id"] is True
    assert auto[1]["payload"] == {"auto": False}
    assert cards[0].get("depends_on") in (None, [])
    assert cards[1]["depends_on"] == ["g0.demo.ideas"]
    assert cards[2]["depends_on"] == ["g0.demo.bible"]
    assert cards[3]["depends_on"] == ["g0.demo.characters"]
    assert cards[4]["depends_on"] == ["g0.demo.scenes"]
    assert cards[5]["depends_on"] == ["g0.demo.scenes"]
    assert [c["lane"] for c in cards] == ["llm", "llm", "llm", "llm",
                                          "image", "audio"]


def test_pending_per_staged_domain():
    ideas = {"pitches": [{"id": "p1", "status": "proposed"}]}
    assert pending_for({}, "ideas", ideas) == {"step": "ideas"}
    picked = {"pitches": [{"id": "p1", "status": "selected"},
                          {"id": "p2", "status": "proposed"}]}
    assert pending_for({}, "ideas", picked) is None
    assert pending_for({}, "ideas", {}) is None
    st = {"concept": {"status": "approved"}, "bible": {"status": "pending"}}
    assert pending_for(st, "bible") == {"step": "bible"}
    st["bible"] = {"status": "approved"}
    assert pending_for(st, "bible") is None
    st["characters"] = [{"name": "Aki", "proposal": "pending",
                         "refs": "", "voice": ""}]
    assert pending_for(st, "characters") == {"step": "character", "char": "Aki"}
    st["characters"][0]["proposal"] = "approved"
    assert pending_for(st, "characters") is None
    st["scenes"] = {"status": "pending"}
    assert pending_for(st, "scenes") == {"step": "scenes"}
    st["scenes"] = {"status": "approved"}
    assert pending_for(st, "scenes") is None


class FakeAgent:
    def run(self):
        return AgentResult(status="verified")

    def system_prompt(self):
        return ""


def test_human_card_routes_to_reviewing_and_is_not_launched(tmp_path, monkeypatch):
    from studio.board import tools as tools_mod
    from studio.board.tools import Tool
    monkeypatch.setattr(daemon_mod, "write_trace", lambda *a, **k: tmp_path)
    # Stage cards run exec tools in-daemon: stub the ideas tool so the
    # routing test exercises state flow without touching disk or the LLM.
    monkeypatch.setitem(tools_mod.TOOLS, "gate0_ideas",
                        Tool(name="gate0_ideas", description="stub",
                             args={}, handler=lambda ctx, args: {"log": ["stub"]},
                             long=True))
    store = BoardStore(tmp_path / "b.db")
    store.create(bootstrap_cards("demo")[0])
    cfg = Config(ROOT)
    d = BoardDaemon(store, cfg, agent_factory=lambda *a: FakeAgent())
    d.tick()
    deadline = time.time() + 5
    while time.time() < deadline and store.get("g0.demo.ideas")["state"] != "agent_verified":
        time.sleep(0.02)
    assert store.get("g0.demo.ideas")["state"] == "agent_verified"
    d.tick()
    assert store.get("g0.demo.ideas")["state"] == "reviewing"
    d.tick()
    assert store.get("g0.demo.ideas")["state"] == "reviewing"
    assert not [r for r in store.runs(card_id="g0.demo.ideas") if r["phase"] == "review"]
