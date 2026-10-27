"""Daemon: launch per lane, route to reviewer, retake cap, milestone expansion."""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.board import daemon as daemon_mod
from studio.board.agent import AgentResult
from studio.board.cards import new_card
from studio.board.daemon import BoardDaemon
from studio.board.store import BoardStore
from studio.config import Config


class FakeAgent:
    def __init__(self, result):
        self.result = result

    def run(self):
        return self.result

    def system_prompt(self):
        return "fake"


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _daemon(store, factory, cfg=None):
    cfg = cfg or Config(ROOT)
    return BoardDaemon(store, cfg, agent_factory=factory)


def test_producer_then_reviewer_accepts(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "write_trace", lambda *a, **k: tmp_path)
    store = BoardStore(tmp_path / "b.db")
    store.create(new_card("c1", kind="outline", title="t", role="showrunner",
                          reviewer="outline_reviewer", lane="llm",
                          show_id="demo", episode=1))
    results = {
        "produce": AgentResult(status="verified", summary="written"),
        "review": AgentResult(status="pass", verdict="pass", notes="good"),
    }
    cfg = Config(ROOT)
    # Keep this test on the worker path; exec reviewers are covered separately.
    cfg.data["agents"]["roles"]["outline_reviewer"]["exec"] = False
    d = _daemon(store, lambda role, card, phase, run_id: FakeAgent(results[phase]),
                cfg=cfg)
    d.tick()
    assert _wait(lambda: (store.get("c1") or {}).get("state") == "agent_verified")
    d.tick()
    assert _wait(lambda: (store.get("c1") or {}).get("state") == "accepted")
    assert len(store.runs(card_id="c1")) == 2


def test_reviewer_fail_hits_retake_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "write_trace", lambda *a, **k: tmp_path)
    store = BoardStore(tmp_path / "b.db")
    store.create(new_card("c1", kind="outline", title="t", role="showrunner",
                          reviewer="outline_reviewer", lane="llm",
                          show_id="demo", episode=1, max_attempts=1))
    results = {
        "produce": AgentResult(status="verified"),
        "review": AgentResult(status="retake", verdict="fail", notes="weak"),
    }
    cfg = Config(ROOT)
    cfg.data["agents"]["roles"]["outline_reviewer"]["exec"] = False
    d = _daemon(store, lambda role, card, phase, run_id: FakeAgent(results[phase]),
                cfg=cfg)
    d.tick()
    assert _wait(lambda: (store.get("c1") or {}).get("state") == "agent_verified")
    d.tick()
    assert _wait(lambda: (store.get("c1") or {}).get("state") == "retake")
    d.tick()
    assert (store.get("c1") or {}).get("state") == "blocked"


def test_lane_capacity_holds_second_card(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "write_trace", lambda *a, **k: tmp_path)
    store = BoardStore(tmp_path / "b.db")
    for cid in ("c1", "c2"):
        store.create(new_card(cid, kind="outline", title=cid, role="showrunner",
                              lane="llm", show_id="demo", episode=1))
    release = threading.Event()

    class BlockingAgent:
        def run(self):
            release.wait(3)
            return AgentResult(status="verified")

        def system_prompt(self):
            return ""

    cfg = Config(ROOT)
    cfg.data["agents"]["lanes"]["llm"]["capacity"] = 1
    d = _daemon(store, lambda *a: BlockingAgent(), cfg=cfg)
    d.tick()
    d.tick()
    states = {cid: store.get(cid)["state"] for cid in ("c1", "c2")}
    assert sorted(states.values()) == ["in_work", "planned"]
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        d.tick()
        if all(store.get(c)["state"] == "accepted" for c in ("c1", "c2")):
            break
        time.sleep(0.02)
    assert all(store.get(c)["state"] == "accepted" for c in ("c1", "c2"))


def test_render_milestone_expands_and_rolls_up(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "write_trace", lambda *a, **k: tmp_path)
    import studio.render as render_mod
    monkeypatch.setattr(render_mod, "_latest_script", lambda show, ep: {
        "scenes": [{"shots": [{"id": "s01"}, {"id": "s02"}]}]})
    import studio.board.tools as board_tools
    monkeypatch.setattr(board_tools, "call", lambda ctx, name, args: {"ok": True})
    store = BoardStore(tmp_path / "b.db")
    store.create(new_card("ep01.render", kind="render", title="render",
                          role="render_exec", lane="video", show_id="demo", episode=1))
    results = {
        "produce": AgentResult(status="verified"),
        "review": AgentResult(status="pass", verdict="pass"),
    }
    d = _daemon(store, lambda role, card, phase, run_id: FakeAgent(results[phase]))
    deadline = time.time() + 5
    while time.time() < deadline:
        d.tick()
        kids = [c for c in store.all() if (c.get("payload") or {}).get("parent") == "ep01.render"]
        if store.get("ep01.render")["state"] == "accepted" and len(kids) == 2:
            break
        time.sleep(0.02)
    assert store.get("ep01.render")["state"] == "accepted"
    kids = [c for c in store.all() if (c.get("payload") or {}).get("parent") == "ep01.render"]
    assert len(kids) == 2 and all(k["state"] == "accepted" for k in kids)
    assert all(u["done"] for u in store.get("ep01.render")["units"])


def test_request_episode_is_idempotent(tmp_path):
    from studio.board.templates import episode_cards
    from studio.board.daemon import request_episode

    store = BoardStore(tmp_path / "b.db")
    first = request_episode(store, "demo", 3)
    second = request_episode(store, "demo", 3)
    assert len(first) == len(second) == len(episode_cards("demo", 3))
    assert {c["id"] for c in first} == {c["id"] for c in second}
    assert store.get("demo.ep03.script")["depends_on"] == ["demo.ep03.outline"]
    # No skeleton card exists in the agent-first flow, so the outline must not
    # wait on one.
    assert store.get("demo.ep03.outline")["depends_on"] == []


def test_qc_failure_creates_fix_card(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "write_trace", lambda *a, **k: tmp_path)
    import studio.board.tools as board_tools
    monkeypatch.setattr(board_tools, "call",
                        lambda ctx, name, args: {"failures": [{"shot_id": "s01"}]})
    store = BoardStore(tmp_path / "b.db")
    store.create(new_card("ep01.qc", kind="qc", title="qc", role="qc_exec",
                          lane="vision", show_id="demo", episode=1,
                          payload={"tool": "run_episode_qc", "args": {}}))
    d = _daemon(store, lambda *a: FakeAgent(AgentResult(status="verified")))
    deadline = time.time() + 5
    while time.time() < deadline:
        d.tick()
        if (store.get("ep01.qc") or {}).get("state") == "blocked":
            break
        time.sleep(0.02)
    assert store.get("ep01.qc")["state"] == "blocked"
    fixes = [c for c in store.all() if c.get("kind") == "fix"]
    assert fixes and fixes[0]["role"] == "fixer"
    assert fixes[0]["payload"]["failures"] == [{"shot_id": "s01"}]


def test_apply_qc_fixes_requeues_the_qc_card(tmp_path, monkeypatch):
    import studio.board.tools as board_tools
    from studio.board.roles import get_role
    from studio.board.tools import ToolContext
    from studio.config import Config

    monkeypatch.setattr("studio.approval.reject_shot", lambda *a, **k: [])
    cfg = Config(ROOT)
    store = BoardStore(tmp_path / "b.db")
    qc = new_card("ep01.qc", kind="qc", title="qc", role="qc_exec",
                  lane="vision", show_id="demo", episode=1,
                  payload={"tool": "run_episode_qc", "args": {}}, max_attempts=4)
    store.create(qc)
    assert store.claim("ep01.qc", "runqc", "planned", "in_work")
    store.advance("ep01.qc", "blocked", "test", "failed")
    fix = new_card("ep01.qc.fix.1", kind="fix", title="fix", role="fixer",
                   lane="llm", show_id="demo", episode=1,
                   payload={"failures": [{"shot_id": "s01",
                                          "vcheck": {"issues": ["bad mouth"]}}],
                            "qc_card": "ep01.qc",
                            "render_milestone": "ep01.render"})
    store.create(fix)
    ctx = ToolContext(store=store, card=fix, role=get_role(cfg, "fixer"),
                      phase="produce", run_id="runfix", llm=None, cfg=cfg)
    res = board_tools.call(ctx, "apply_qc_fixes", {})
    assert len(res["render_cards"]) == 1
    new_id = res["render_cards"][0]
    assert store.get(new_id)["role"] == "render_exec"
    qc2 = store.get("ep01.qc")
    assert qc2["state"] == "planned"
    assert qc2["depends_on"] == [new_id]
    assert qc2["attempts"] == 1


def test_outline_skeleton_expands_into_beats_cards(tmp_path, monkeypatch):
    """An accepted skeleton becomes one beats card per batch + the merge waits."""
    import studio.planner as planner_mod

    monkeypatch.setattr(planner_mod, "read_plan_skeleton",
                        lambda show, ep: {"scenes": [{"id": f"s{i:02d}"}
                                                    for i in range(1, 6)]})
    monkeypatch.setattr(daemon_mod, "write_trace", lambda *a, **k: tmp_path)
    store = BoardStore(tmp_path / "b.db")
    store.create(new_card("demo.ep01.outline.skeleton", kind="outline_skeleton",
                          title="skel", role="showrunner", lane="llm",
                          show_id="demo", episode=1, state="accepted"))
    store.create(new_card("demo.ep01.outline", kind="outline", title="merge",
                          role="showrunner", reviewer="outline_reviewer",
                          lane="llm", show_id="demo", episode=1,
                          state="inbox",
                          depends_on=["demo.ep01.outline.skeleton"]))
    cfg = Config(ROOT)
    cfg.data["pipeline"]["plan_beats_batch"] = 2
    # Keep the beats on the fake worker path; the exec path is tested via the
    # planner tests, which call the staged tools directly.
    cfg.data["agents"]["roles"]["outline_writer"]["exec"] = False
    d = _daemon(store, lambda *a: FakeAgent(AgentResult(status="blocked")), cfg=cfg)
    d.tick()
    beats = [c for c in store.all() if c.get("kind") == "outline_beats"]
    assert [c["id"] for c in beats] == [
        "demo.ep01.outline.beats.01", "demo.ep01.outline.beats.02",
        "demo.ep01.outline.beats.03"]
    assert store.get("demo.ep01.outline")["depends_on"] == [c["id"] for c in beats]
    assert store.get("demo.ep01.outline")["state"] == "planned"
