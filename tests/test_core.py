"""M0 smoke tests: config, db, bus round-trip, duration grid, H3 prompt compiler."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.bus import InMemoryBroker
from studio.bus.events import TEST, Event
from studio.compile.durations import snap_duration, valid_durations
from studio.compile.h3_prompt import compile_h3_prompt
from studio.config import Config


def test_config_loads_defaults():
    cfg = Config(ROOT)
    assert cfg["pipeline"]["mode"] in ("overnight", "continuous", "hybrid")
    assert cfg.get("bus", "provider", "") in ("memory", "redis")


def test_gpu_manager_reentrant_acquire(tmp_path):
    """A thread that holds the GPU for one service (storyboard holds COMFYUI for
    krea2) and then makes an LLM call must not self-deadlock on a nested acquire.
    The nested acquire runs directly; the outer holder owns VRAM."""
    from studio.gpu_manager import GPUManager, ServiceType

    gm = GPUManager(Config(tmp_path))
    gm._load = lambda *a, **k: None
    gm._unload = lambda *a, **k: None

    order = []
    with gm.acquire(ServiceType.COMFYUI):
        order.append("outer")
        with gm.acquire(ServiceType.LLM, model="m"):
            order.append("nested")
    order.append("done")
    assert order == ["outer", "nested", "done"]
    assert gm._holders == 0


def test_broker_round_trip():
    bus = InMemoryBroker("test")
    got = []
    bus.register("bus:system", "t", got.append)
    bus.publish("bus:system", Event(type=TEST, payload={"ok": True}))
    assert len(got) == 1 and got[0].type == TEST
    assert bus.round_trip()


def test_duration_grid():
    grid = valid_durations()
    assert grid[0][1] == 107 and grid[-1][1] == 345  # 17k+5 envelope
    assert snap_duration(10.0) == (14, 243, 10.125)
    assert snap_duration(5.0) == (7, 124, 5.166666666666667)


def test_h3_prompt_format():
    prompt = compile_h3_prompt(
        "x",
        [
            {"id": "a", "action": "lands", "duration_s": 5.167, "camera": "wide",
             "subjects": ["<Subject 1>"]},
            {"id": "b", "action": "hands over package", "duration_s": 5.167,
             "camera": "medium", "subjects": ["<Subject 1>"]},
        ],
        subject_definitions=["<Subject 1> is the character shown in <Picture 1>."],
        soundscape="wind", music="bass",
    )
    assert "subject_definitions:" in prompt
    assert "[Shot 1] lands" in prompt
    assert "[Shot 2] At 00:05.167, hands over package" in prompt
    assert "overall_soundscape: wind" in prompt
    assert "non_diegetic_music: bass" in prompt


def test_db_schema_and_continuity(tmp_path):
    from studio.db import DB
    db = DB(tmp_path / "test.db")
    db.set_continuity("ns", {"episode": 0})
    assert db.get_continuity("ns")["episode"] == 0
    run_id = db.create_run("ns", 1, "overnight", 480)
    assert db.latest_episode("ns") == 1
    db.close()
