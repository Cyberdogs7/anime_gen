"""Regression tests for the "one decision, two stores" class of bug.

Each test pins a place where an approval/gate decision is (or was) written to one
store but read from another, so the pipeline and the UI could disagree.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import studio.config as _cfg
from studio.show import Show


def _rooted_show(tmp_path, monkeypatch, show_id="demo") -> Show:
    """A show whose dir is what ``Show(show_id)`` resolves to under the patched
    config, so code that rebuilds the Show from the id sees the same files."""
    monkeypatch.setattr(_cfg, "_default", _cfg.Config(tmp_path))
    show = Show(show_id)
    (show.dir / "runs" / "EP01").mkdir(parents=True, exist_ok=True)
    return show


class _FakeChain:
    def __init__(self, *a, **k):
        pass

    def _emit(self, event):
        pass

    def advance(self, *a, **k):
        return []


# --- #11: script-review override (tuple treated as a dict) --------------------

def test_approve_script_override_actually_writes(tmp_path, monkeypatch):
    from studio import approval
    show = _rooted_show(tmp_path, monkeypatch)
    p = show.dir / "runs" / "EP01" / "script.r1.json"
    p.write_text(json.dumps({"scenes": [],
                             "_review": {"passed": False, "rounds": 1,
                                         "notes": [{"note": "x"}]}}),
                 encoding="utf-8")
    approval.approve_script("demo", "EP01")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["_review"]["passed"] is True
    assert data["_review"]["override"] is True


# --- #7: failed script review blocks the manual storyboard/render path --------

def test_storyboard_blockers_include_script_review(tmp_path, monkeypatch):
    from studio.planner import approve_plan, approve_scene_details
    from studio.storyboard import storyboard_blockers
    show = _rooted_show(tmp_path, monkeypatch)
    runs = show.dir / "runs" / "EP01"
    (runs).mkdir(parents=True, exist_ok=True)
    # plan + scene details approved, but the script failed review.
    _write_plan(show, status="pending")
    _write_details(show, status="pending")
    approve_plan(show, 1, override=True)
    approve_scene_details(show, 1)
    (runs / "script.r1.json").write_text(json.dumps({
        "scenes": [], "_review": {"passed": False, "rounds": 2, "notes": []}}),
        encoding="utf-8")
    blockers = storyboard_blockers(show, 1)
    assert any("review" in b for b in blockers)


# --- #1: story state is derived from the real gates, never the mirror --------

def test_story_state_ignores_stale_mirror_file(tmp_path, monkeypatch):
    from studio import approval
    show = _rooted_show(tmp_path, monkeypatch)
    # plan still pending, but story.approval.json claims approved.
    _write_plan(show, status="pending")
    approval._set_story("demo", "EP01", "approved", "signed off earlier")
    assert approval.story_state("demo", "EP01")["status"] == "pending"


def test_approve_story_flips_the_real_gates(tmp_path, monkeypatch):
    from studio import approval
    from studio.planner import read_episode_plan
    show = _rooted_show(tmp_path, monkeypatch)
    _write_plan(show, status="pending")
    _write_details(show, status="pending")
    approval.approve_story("demo", "EP01")
    assert read_episode_plan(show, 1)["status"] == "approved"
    assert approval.story_state("demo", "EP01")["status"] in ("pending", "approved")
    # With plan + details approved and no script yet, story is still pending
    # (script gate), and once a passing script exists it reads approved.
    runs = show.dir / "runs" / "EP01"
    (runs / "script.r1.json").write_text(json.dumps({"scenes": []}), encoding="utf-8")
    assert approval.story_state("demo", "EP01")["status"] == "approved"


# --- #4: per-shot decisions/reports are cleared on regeneration --------------

def test_clear_scenes_downstream_drops_stale_shot_state(tmp_path, monkeypatch):
    from studio.planner import _clear_scenes_downstream
    show = _rooted_show(tmp_path, monkeypatch)
    runs = show.dir / "runs" / "EP01"
    (runs / "shots.approval.json").write_text('{"s01_sh01": {"status": "approved"}}',
                                              encoding="utf-8")
    (runs / "prompts.json").write_text("{}", encoding="utf-8")
    (runs / "vcheck").mkdir(parents=True, exist_ok=True)
    (runs / "vcheck" / "summary.json").write_text("{}", encoding="utf-8")
    (runs / "qc").mkdir(parents=True, exist_ok=True)
    (runs / "qc" / "summary.json").write_text("{}", encoding="utf-8")
    _clear_scenes_downstream(show, 1)
    assert not (runs / "shots.approval.json").exists()
    assert not (runs / "prompts.json").exists()
    assert not (runs / "vcheck").exists()
    assert not (runs / "qc").exists()


# --- #12: CLI character approval means the same as the dashboard -------------

def test_cli_approve_character_sets_proposal_refs_voice(tmp_path, monkeypatch):
    import studio.cli as cli
    import studio.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, "BootstrapChain", _FakeChain)
    show = _rooted_show(tmp_path, monkeypatch)
    show.set_bootstrap_state({"characters": [
        {"name": "Ryou", "proposal": "", "refs": "", "voice": ""}]})
    show.write_character({"id": "ryou", "name": "Ryou", "appearance_canonical": "x"})
    rd = show.character_refs_dir("ryou")
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "refs.json").write_text(json.dumps({
        "status": "real", "refs": ["r.png"], "variants": {"base": "r.png"}}),
        encoding="utf-8")
    (rd / "r.png").write_bytes(b"x")
    args = argparse.Namespace(show="demo", step="character", char="Ryou")
    rc = cli._cmd_approve(args)
    assert rc == 0
    entry = show.bootstrap_state()["characters"][0]
    assert entry["proposal"] == "approved"
    assert entry["refs"] == "approved"
    assert entry["voice"] == "approved"


# --- helpers ------------------------------------------------------------------

def _write_plan(show: Show, status: str = "pending") -> None:
    runs = show.dir / "runs" / "EP01"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "plan.json").write_text(json.dumps({
        "episode": 1, "status": status, "scenes": [],
        "plan_review": {"passed": True, "rounds": 1, "notes": []}}), encoding="utf-8")


def _write_details(show: Show, status: str = "pending") -> None:
    runs = show.dir / "runs" / "EP01"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "scene_details.json").write_text(json.dumps({
        "status": status, "scenes": []}), encoding="utf-8")
