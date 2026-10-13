"""Storyboard/render builds must be gated on approvals and be cancellable."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import render, storyboard
from studio.planner import _clear_scenes_downstream
from studio.show import Show


def _show(tmp_path) -> Show:
    for sub in ("characters", "voices", "scenes", "runs", "continuity"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "runs" / "EP01").mkdir(parents=True, exist_ok=True)
    return Show("demo", root=tmp_path)


def test_storyboard_blockers_until_approved(tmp_path):
    show = _show(tmp_path)
    b = storyboard.storyboard_blockers(show, 1)
    assert "episode outline not approved" in b
    assert "scene details not approved" in b
    assert "no script" in b

    runs = show.dir / "runs" / "EP01"
    (runs / "plan.json").write_text(json.dumps({"status": "approved"}), encoding="utf-8")
    (runs / "scene_details.json").write_text(
        json.dumps({"status": "approved", "scenes": [{"id": "s01"}]}), encoding="utf-8")
    (runs / "script.r1.json").write_text(
        json.dumps({"episode": 1, "scenes": []}), encoding="utf-8")
    assert storyboard.storyboard_blockers(show, 1) == []


def test_build_storyboard_blocks_without_approval(tmp_path):
    storyboard.STORYBOARD_JOBS.pop("demo", None)
    show = _show(tmp_path)
    storyboard.build_storyboard(show, 1)          # no thread, no GPU: returns blocked
    st = storyboard.storyboard_status("demo")
    assert st["state"] == "blocked"
    assert "not approved" in st["detail"]


def test_build_render_blocks_without_approval(tmp_path):
    render.RENDER_JOBS.pop("demo", None)
    show = _show(tmp_path)
    render.build_render(show, 1)
    assert render.render_status("demo")["state"] == "blocked"


def test_stop_sets_stopped():
    storyboard.STORYBOARD_JOBS["demo"] = {"state": "running"}
    storyboard.stop_storyboard("demo")
    assert storyboard.STORYBOARD_JOBS["demo"]["state"] == "stopped"
    render.RENDER_JOBS["demo"] = {"state": "running"}
    render.stop_render("demo")
    assert render.RENDER_JOBS["demo"]["state"] == "stopped"


def test_clear_downstream_stops_running_jobs(tmp_path):
    show = _show(tmp_path)
    storyboard.STORYBOARD_JOBS["demo"] = {"state": "running"}
    render.RENDER_JOBS["demo"] = {"state": "running"}
    _clear_scenes_downstream(show, 1)
    assert storyboard.STORYBOARD_JOBS["demo"]["state"] == "stopped"
    assert render.RENDER_JOBS["demo"]["state"] == "stopped"
