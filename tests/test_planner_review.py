"""Tests for the chunked planner's writers'-room review loop (planner._writer_review)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.config import Config
from studio.planner import _writer_review
from studio.show import Show


def _make_show(tmp_path) -> Show:
    for sub in ("characters", "voices", "scenes"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=tmp_path)
    show.write_character({"id": "ryou", "name": "Ryou", "appearance_canonical": "x"})
    show.write_bible({"title": "Demo", "content_policy": "mature",
                      "runtime_target_s": 30,
                      "mature_spec": {"quotient": "ecchi", "quotas": {},
                                      "escalation": {}, "tone_boundaries": [],
                                      "characters": {}, "scene_types": []}})
    return show


class FakeChunkLLM:
    def __init__(self):
        self.review_calls = 0

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "WRITERS' ROOM NOTES" in user:      # revision prompt
            return {"episode": 1, "scenes": [{"id": "s01", "shots": [
                {"id": "s01_sh01", "duration_s": 10.125, "action": "revised",
                 "dialogue": [], "references": {"characters": ["Ryou"]}}]}]}
        self.review_calls += 1                 # reviewers
        passed = self.review_calls > 3         # round 1 fails, round 2 passes
        return {"score": 0.1 if passed else 0.9,
                "notes": [{"scene": "s01", "note": "fix this"}], "pass": passed}


def test_chunked_writers_room_revises_and_writes_rounds(tmp_path):
    show = _make_show(tmp_path)
    script = {"episode": 1, "summary": "x", "cast": ["Ryou"],
              "scenes": [{"id": "s01", "shots": [
                  {"id": "s01_sh01", "duration_s": 10.125, "action": "draft",
                   "dialogue": [], "references": {"characters": ["Ryou"]}}]}]}
    final = _writer_review(show, 1, script, llm=FakeChunkLLM(), cfg=Config(tmp_path),
                           max_revisions=2)
    runs = show.dir / "runs" / "EP01"
    assert (runs / "script.r2.json").exists()          # revised
    assert (runs / "reviews" / "slop.r2.json").exists()
    assert (runs / "reviews" / "continuity.r2.json").exists()
    saved = json.loads((runs / "script.r2.json").read_text(encoding="utf-8"))
    assert saved["scenes"][0]["shots"][0]["action"] == "revised"


def test_chunked_review_passes_on_round_one(tmp_path):
    show = _make_show(tmp_path)
    script = {"episode": 1, "summary": "x", "cast": ["Ryou"], "scenes": []}

    class PassingLLM:
        def chat_json(self, messages, **kwargs):
            return {"score": 0.1, "notes": [], "pass": True}

    final = _writer_review(show, 1, script, llm=PassingLLM(), cfg=Config(tmp_path),
                           max_revisions=2)
    runs = show.dir / "runs" / "EP01"
    assert not (runs / "script.r2.json").exists()      # no revision needed
    assert (runs / "reviews" / "slop.r1.json").exists()


def test_reject_scene_details_cascades_downstream(tmp_path):
    """Rejecting scenes must invalidate everything derived from them (shots,
    script, storyboard, video) while keeping plan.json and the treatments, so a
    re-approve regenerates shots from the NEW treatments instead of reusing stale
    checkpoints keyed by scene id."""
    import json as _json
    import shutil
    from studio.planner import (generate_scene_details, read_scene_details,
                                reject_scene_details)

    show = _make_show(tmp_path)
    runs = show.dir / "runs" / "EP01"
    sc_dir = runs / "scenes"
    for sub in ("scenes", "reviews", "storyboard", "video"):
        (runs / sub).mkdir(parents=True, exist_ok=True)
    (runs / "plan.json").write_text(_json.dumps({"episode": 1, "status": "approved",
                                                 "scenes": [{"id": "s01"}]}),
                                    encoding="utf-8")
    # Simulated prior state: treatments + everything downstream already built.
    (sc_dir / "s01.json").write_text(_json.dumps({"id": "s01", "narrative": "old"}),
                                     encoding="utf-8")
    (sc_dir / "s01_shots.json").write_text(_json.dumps({"id": "s01", "shots": []}),
                                           encoding="utf-8")
    (sc_dir / "s01.p3.json").write_text(_json.dumps({"shots": []}), encoding="utf-8")
    (runs / "script.r2.json").write_text("{}", encoding="utf-8")
    (runs / "storyboard" / "s01_sh01.png").write_bytes(b"x")
    (runs / "video" / "s01_sh01.mp4").write_bytes(b"x")
    details_path = runs / "scene_details.json"
    details_path.write_text(_json.dumps({"status": "approved", "scenes": [
        {"id": "s01", "narrative": "old"}]}), encoding="utf-8")

    reject_scene_details(show, 1, "too much padding in the dialogue")

    assert _json.loads(details_path.read_text(encoding="utf-8"))["status"] == "rejected"
    assert (runs / "plan.json").exists()                # plan untouched
    assert (sc_dir / "s01.json").exists()               # treatment kept (rewritten next)
    assert not (sc_dir / "s01_shots.json").exists()     # stale shots dropped
    assert not (sc_dir / "s01.p3.json").exists()        # partial pass checkpoints dropped
    assert not (runs / "script.r2.json").exists()
    assert not (runs / "storyboard").exists()
    assert not (runs / "video").exists()

    # The regenerate-with-notes path then rewrites every treatment.
    class FakeDetailLLM:
        def chat_json(self, messages, **kwargs):
            return {"id": "s01", "narrative": "rewritten", "beats": [],
                    "dialogue_beats": [], "location": "", "time_of_day": "", "summary": ""}

    generate_scene_details(show, 1, llm=FakeDetailLLM(), cfg=Config(tmp_path), notes="fix")
    assert read_scene_details(show, 1)["scenes"][0]["narrative"] == "rewritten"


def test_clear_scene_shots_keeps_approved_details(tmp_path):
    """Regenerate-shots must clear the derived artifacts but leave plan + scene
    details approved, so a rebuild reuses the SAME treatments."""
    import json as _json
    from studio.planner import clear_scene_shots, read_scene_details

    show = _make_show(tmp_path)
    runs = show.dir / "runs" / "EP01"
    sc_dir = runs / "scenes"
    for sub in ("scenes", "storyboard", "video"):
        (runs / sub).mkdir(parents=True, exist_ok=True)
    (runs / "plan.json").write_text(_json.dumps({"episode": 1, "status": "approved"}),
                                    encoding="utf-8")
    details_path = runs / "scene_details.json"
    details_path.write_text(_json.dumps({"status": "approved", "scenes": [
        {"id": "s01", "narrative": "approved treatment"}]}), encoding="utf-8")
    (sc_dir / "s01_shots.json").write_text(_json.dumps({"id": "s01", "shots": []}),
                                           encoding="utf-8")
    (sc_dir / "s01.p5.json").write_text(_json.dumps({"shots": []}), encoding="utf-8")
    (runs / "script.r2.json").write_text("{}", encoding="utf-8")
    (runs / "storyboard" / "s01_sh01.png").write_bytes(b"x")

    clear_scene_shots(show, 1)

    assert _json.loads(details_path.read_text(encoding="utf-8"))["status"] == "approved"
    assert (runs / "plan.json").exists()
    assert not (sc_dir / "s01_shots.json").exists()
    assert not (sc_dir / "s01.p5.json").exists()
    assert not (runs / "script.r2.json").exists()
    assert not (runs / "storyboard").exists()


def test_scene_pass_prompt_carries_director_notes():
    from studio.prompts import scene_pass_prompt

    pass_cfg = {"name": "dialogue", "job": "DIALOGUE WRITER",
                "schema": {"shots": []}, "instructions": "Write lines.",
                "fields": ["dialogue"], "cast_key": "personality"}
    p = scene_pass_prompt(pass_cfg, {"title": "Demo"}, "ep summary", {"id": "s01"},
                          [{"name": "Ryou", "personality": ["quiet"]}],
                          [], 110, notes="stop writing parenthetical grunts")
    assert "stop writing parenthetical grunts" in p
    assert "DIRECTOR'S FEEDBACK" in p


class FakePlanReviewLLM:
    """Simulates the plan review loop: review fails on round 1, passes round 2;
    the revision prompt returns a corrected outline that still has scenes."""

    def __init__(self):
        self.review_calls = 0
        self.revision_calls = 0

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "Review this EPISODE OUTLINE for narrative and structural coherence" in user:
            self.review_calls += 1
            passed = self.review_calls > 1
            return {"score": 0.1 if passed else 0.9,
                    "notes": [] if passed else [{"scene": "overall", "item": "episode 1",
                                                 "note": "cold open, zero context"}],
                    "pass": passed}
        if "REVISE the EPISODE OUTLINE" in user:      # review-driven revision
            self.revision_calls += 1
            return {"episode": 1, "title": "Fixed", "plot": "fixed plot",
                    "characters": ["Ryou"], "scenes": [
                        {"id": "s01", "location": "dojo", "time_of_day": "dawn",
                         "summary": "intro", "characters": ["Ryou"], "beats": ["a", "b", "c"]}]}
        return {"episode": 1, "title": "Draft", "plot": "draft plot",
                "characters": ["Ryou"], "scenes": [
                    {"id": "s01", "location": "dojo", "time_of_day": "dawn",
                     "summary": "draft", "characters": ["Ryou"], "beats": ["a", "b", "c"]}]}


def test_plan_review_loop_revises_until_pass(tmp_path):
    from studio.planner import _plan_review_loop

    show = _make_show(tmp_path)
    llm = FakePlanReviewLLM()
    cfg = Config(tmp_path)
    plan = {"episode": 1, "title": "Draft", "plot": "draft plot",
            "characters": ["Ryou"], "scenes": [
                {"id": "s01", "location": "dojo", "time_of_day": "dawn",
                 "summary": "draft", "characters": ["Ryou"], "beats": ["a", "b", "c"]}]}
    system = "system"
    final = _plan_review_loop(show, 1, plan, llm, cfg, "m", system,
                              show.read_bible(), "synopsis", {}, ["Ryou"], {}, [])
    assert llm.review_calls == 2
    assert llm.revision_calls == 1
    assert final["plan_review"]["passed"] is True
    assert final["plan_review"]["rounds"] == 2
    assert final["plot"] == "fixed plot"
    runs = show.dir / "runs" / "EP01" / "reviews"
    assert (runs / "structure.r1.json").exists()
    assert (runs / "structure.r2.json").exists()


def test_plan_review_loop_marks_failure_after_max_revisions(tmp_path):
    from studio.planner import _plan_review_loop

    show = _make_show(tmp_path)
    cfg = Config(tmp_path)

    class AlwaysFail:
        def __init__(self):
            self.review_calls = 0
        def chat_json(self, messages, **kwargs):
            user = messages[-1]["content"]
            if "Review this EPISODE OUTLINE" in user:
                self.review_calls += 1
                return {"score": 0.9, "notes": [{"scene": "overall", "item": "x",
                                                 "note": "still broken"}], "pass": False}
            return {"episode": 1, "plot": "still broken", "characters": ["Ryou"], "scenes": [
                {"id": "s01", "location": "dojo", "time_of_day": "dawn",
                 "summary": "x", "characters": ["Ryou"], "beats": ["a", "b", "c"]}]}

    llm = AlwaysFail()
    plan = {"episode": 1, "plot": "broken", "characters": ["Ryou"], "scenes": [
        {"id": "s01", "location": "dojo", "time_of_day": "dawn",
         "summary": "x", "characters": ["Ryou"], "beats": ["a", "b", "c"]}]}
    final = _plan_review_loop(show, 1, plan, llm, cfg, "m", "system",
                              show.read_bible(), "synopsis", {}, ["Ryou"], {}, [])
    assert final["plan_review"]["passed"] is False
    assert final["plan_review"]["rounds"] == cfg.get("reviewers", "plan_max_revisions", 4)
    assert final["plan_review"]["notes"]               # reviewer notes preserved


def test_approve_plan_refuses_failed_review(tmp_path):
    import pytest
    from studio.planner import approve_plan, read_episode_plan, reject_plan

    show = _make_show(tmp_path)
    runs = show.dir / "runs" / "EP01"
    runs.mkdir(parents=True, exist_ok=True)
    plan = {"episode": 1, "status": "pending", "plot": "broken",
            "plan_review": {"rounds": 2, "passed": False,
                            "notes": [{"scene": "overall", "item": "x",
                                       "note": "cold open zero context"}]}}
    (runs / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="failed structural review"):
        approve_plan(show, 1)
    assert read_episode_plan(show, 1)["status"] == "pending"

    plan["plan_review"]["passed"] = True
    (runs / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    approve_plan(show, 1)
    assert read_episode_plan(show, 1)["status"] == "approved"


def test_reconciler_regenerates_rejected_plan_from_notes(tmp_path, monkeypatch):
    """A rejected plan must regenerate from its rejection notes instead of being
    re-approved as-is — the bug that let the broken EP01 outline flow through."""
    from studio.episode_repair import _advance_episode

    show = _make_show(tmp_path)
    runs = show.dir / "runs" / "EP01"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "plan.json").write_text(json.dumps({
        "episode": 1, "status": "rejected",
        "rejected_notes": "starts in battle, zero context"}), encoding="utf-8")

    captured = {}

    def fake_generate(show, episode, llm=None, cfg=None, notes=""):
        captured["notes"] = notes
        # The regenerated plan clears the review gate; reconciler then approves.
        (runs / "plan.json").write_text(json.dumps({
            "episode": 1, "status": "pending", "rejected_notes": notes,
            "plot": "fixed",
            "plan_review": {"rounds": 1, "passed": True, "notes": []}}), encoding="utf-8")
        return {"episode": 1}

    # Keep the reconciler from drifting into scene-detail generation (real LLM).
    monkeypatch.setattr("studio.episode_repair.generate_episode_plan", fake_generate)
    monkeypatch.setattr("studio.episode_repair.generate_scene_details",
                        lambda show, episode, **kw: [])
    monkeypatch.setattr("studio.episode_repair.assemble_episode_script",
                        lambda show, episode, **kw: {"episode": episode})
    _advance_episode(show, 1)
    plan = json.loads((runs / "plan.json").read_text(encoding="utf-8"))
    assert captured["notes"] == "starts in battle, zero context"
    assert plan["status"] == "approved"
    assert plan["rejected_notes"] == "starts in battle, zero context"


def test_reconciler_does_not_approve_failed_review_plan(tmp_path, monkeypatch):
    """An outline that failed structural review must NOT auto-approve — the
    reconciler halts and holds it for a human instead of rebuilding from it."""
    from studio.episode_repair import _advance_episode

    show = _make_show(tmp_path)
    runs = show.dir / "runs" / "EP01"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "plan.json").write_text(json.dumps({
        "episode": 1, "status": "pending", "plot": "broken",
        "plan_review": {"rounds": 2, "passed": False,
                        "notes": [{"scene": "overall", "item": "x",
                                   "note": "cold open zero context"}]}}), encoding="utf-8")

    _advance_episode(show, 1)
    plan = json.loads((runs / "plan.json").read_text(encoding="utf-8"))
    assert plan["status"] == "pending"          # NOT approved
