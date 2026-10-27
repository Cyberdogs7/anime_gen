"""Agent loop: JSON action protocol, tool allowlist, trace, final verdict."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.board.agent import Agent, parse_action
from studio.board.cards import new_card
from studio.board.roles import Role
from studio.board.store import BoardStore
from studio.config import Config


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        return self.replies.pop(0)


def _role(tools):
    return Role(name="showrunner", kind="producer", lane="llm", model="m",
                temperature=0.0, max_steps=6, tools=tuple(tools))


def test_parse_action_variants():
    assert parse_action('{"action":"x","args":{}}')["action"] == "x"
    assert parse_action('```json\n{"final":{"status":"verified"}}\n```')["final"]["status"] == "verified"
    assert parse_action('thinking... {"final":{"status":"blocked"}} trailing')["final"]["status"] == "blocked"


def test_agent_calls_tool_then_finishes(tmp_path):
    store = BoardStore(tmp_path / "b.db")
    card = new_card("c1", kind="outline", title="t", role="showrunner",
                    lane="llm", show_id="demo", episode=1)
    store.create(card)
    llm = FakeLLM([
        json.dumps({"action": "add_note", "args": {"text": "hello"}}),
        json.dumps({"final": {"status": "verified", "summary": "done"}}),
    ])
    agent = Agent(role=_role(["add_note"]), card=card, store=store,
                  phase="produce", run_id="r1", llm=llm, cfg=Config(tmp_path))
    result = agent.run()
    assert result.status == "verified"
    assert result.steps == 2
    assert store.get("c1")["notes"][0]["text"] == "hello"
    assert [t["action"] for t in result.trace] == ["add_note", "final"]


def test_agent_rejects_tool_outside_allowlist_and_recovers(tmp_path):
    store = BoardStore(tmp_path / "b.db")
    card = new_card("c1", kind="outline", title="t", role="showrunner",
                    lane="llm", show_id="demo", episode=1)
    store.create(card)
    llm = FakeLLM([
        json.dumps({"action": "render_shot", "args": {"shot_id": "s01"}}),
        json.dumps({"final": {"status": "verified"}}),
    ])
    agent = Agent(role=_role(["add_note"]), card=card, store=store,
                  phase="produce", run_id="r1", llm=llm, cfg=Config(tmp_path))
    result = agent.run()
    assert result.status == "verified"
    assert result.trace[0]["ok"] is False
    assert "PermissionError" in result.trace[0]["error"]


def test_agent_review_verdict(tmp_path):
    store = BoardStore(tmp_path / "b.db")
    card = new_card("c1", kind="outline", title="t", role="showrunner",
                    lane="llm", show_id="demo", episode=1)
    store.create(card)
    role = Role(name="outline_reviewer", kind="reviewer", lane="llm", model="m",
                temperature=0.0, max_steps=4, tools=("add_note",))
    llm = FakeLLM([json.dumps({"final": {"verdict": "fail", "notes": "weak hook"}})])
    result = Agent(role=role, card=card, store=store, phase="review",
                   run_id="r2", llm=llm, cfg=Config(tmp_path)).run()
    assert result.verdict == "fail" and result.notes == "weak hook"
    assert result.ok is False


def test_agent_step_budget_blocks(tmp_path):
    store = BoardStore(tmp_path / "b.db")
    card = new_card("c1", kind="outline", title="t", role="showrunner",
                    lane="llm", show_id="demo", episode=1)
    store.create(card)
    role = Role(name="showrunner", kind="producer", lane="llm", model="m",
                temperature=0.0, max_steps=2, tools=("add_note",))
    llm = FakeLLM([json.dumps({"action": "add_note", "args": {"text": "a"}})] * 2)
    result = Agent(role=role, card=card, store=store, phase="produce",
                   run_id="r1", llm=llm, cfg=Config(tmp_path)).run()
    assert result.status == "blocked"
    assert "budget" in result.error
