"""Board store: transitions, claims, lanes, runs, recovery."""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.board.cards import new_card
from studio.board.store import BoardStore


def _card(cid="c1", **kw):
    base = dict(kind="outline", title="t", role="showrunner", lane="llm",
                show_id="demo", episode=1, reviewer="outline_reviewer")
    base.update(kw)
    return new_card(cid, **base)


def test_create_get_and_illegal_transition(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.create(_card())
    assert store.get("c1")["state"] == "planned"
    with pytest.raises(ValueError):
        store.advance("c1", "accepted", "human")   # planned -> accepted is not legal
    store.advance("c1", "in_work", "human")
    store.advance("c1", "agent_verified", "human")
    store.advance("c1", "reviewing", "human")
    assert store.advance("c1", "accepted", "human")["state"] == "accepted"
    assert [h["to_state"] for h in store.history("c1")][-1] == "accepted"


def test_claim_is_atomic_and_releasable(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.create(_card())
    assert store.claim("c1", "run-a", "planned", "in_work") is True
    assert store.claim("c1", "run-b", "planned", "in_work") is False
    store.release("c1", "run-a")
    assert store.get("c1")["claimed_by"] is None
    assert store.claim("c1", "run-b", "planned", "in_work") is True


def test_artifacts_notes_and_units(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.create(_card(units=[{"id": "s01", "label": "s01", "done": False}]))
    store.attach("c1", "runs/EP01/plan.json", "outline", "json")
    store.note("c1", "run-a", "hello")
    store.check_units("c1", ["s01"], "run-a")
    card = store.get("c1")
    assert card["artifacts"][0]["path"] == "runs/EP01/plan.json"
    assert card["notes"][0]["text"] == "hello"
    assert card["units"][0]["done"] is True


def test_runs_lane_load_and_stale_recovery(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.create(_card())
    store.claim("c1", "run-a", "planned", "in_work")
    store.start_run("run-a", "c1", "showrunner", "produce", "llm", "m")
    assert store.lane_load("llm") == 1
    store.finish_run("run-a", "done")
    assert store.lane_load("llm") == 0

    store.advance("c1", "agent_verified", "human")
    store.advance("c1", "retake", "human")
    store.advance("c1", "planned", "human")
    store.claim("c1", "run-b", "planned", "in_work")
    store.start_run("run-b", "c1", "showrunner", "produce", "llm", "m")
    # backdate the run so it is stale
    with store._lock:
        store._conn.execute("UPDATE board_runs SET started=? WHERE id=?",
                            (time.time() - 9999, "run-b"))
        store._conn.commit()
    recovered = store.recover_stale(60)
    assert recovered == ["run-b"]
    card = store.get("c1")
    assert card["state"] == "blocked" and card["claimed_by"] is None


def test_recover_missing_fails_orphaned_runs(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.create(_card())
    store.claim("c1", "run-a", "planned", "in_work")
    store.start_run("run-a", "c1", "showrunner", "produce", "llm", "m")
    assert store.recover_missing({"run-a"}) == []
    assert store.get("c1")["state"] == "in_work"
    assert store.recover_missing(set()) == ["run-a"]
    card = store.get("c1")
    assert card["state"] == "blocked" and card["claimed_by"] is None
    assert store.runs(card_id="c1")[0]["state"] == "failed"


def test_requeue_review_returns_to_reviewer_without_a_producer(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.create(_card(state="blocked"))
    store.update("c1", attempts=2, notes=[{"by": "review", "text": "old"}])
    card = store.requeue_review("c1")
    assert card["state"] == "agent_verified"
    assert card["attempts"] == 0
    assert card["notes"] == []
    assert store.requeue_review("missing") is None

def test_delete_show_purges_board_rows(tmp_path, monkeypatch):
    """Deleting a show must also remove its board cards, history and runs so the
    supervisor cannot relaunch work (and LLM calls) for a show that is gone."""
    import studio.config as cfg_mod
    from studio.config import get_config
    from studio.shows import delete_show

    monkeypatch.setattr(cfg_mod, "_default", cfg_mod.Config(tmp_path))
    cfg = get_config()
    (cfg.shows_dir / "demo").mkdir(parents=True)

    store = BoardStore(cfg.db_path)
    store.create(_card())
    store.claim("c1", "run-a", "planned", "in_work")
    store.start_run("run-a", "c1", "showrunner", "produce", "llm", "m")
    store.create(_card("c2", show_id="keep"))
    store.close()

    delete_show("demo")

    store = BoardStore(cfg.db_path)
    assert store.get("c1") is None
    assert store.history("c1") == []
    assert store.run("run-a") is None
    assert store.get("c2") is not None
