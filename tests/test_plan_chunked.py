"""Tests for the chunked episode-outline generator (skeleton + beats phases)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.config import Config
from studio.planner import generate_episode_plan
from studio.show import Show


def _show(tmp_path) -> Show:
    for sub in ("characters", "voices", "scenes", "runs", "continuity"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=tmp_path)
    show.write_character({"id": "ryou", "name": "Ryou", "appearance_canonical": "x"})
    show.write_bible({
        "title": "Demo", "content_policy": "mature", "runtime_target_s": 1320,
        "overall_plotline": {"id": "op", "name": "Overall", "characters": ["Ryou"],
                             "summary": "the driving force"},
        "plotlines": [
            {"id": "p1", "name": "Rivalry", "kind": "character", "characters": ["Ryou"],
             "summary": "daily rivalry", "status": "active", "last_seen_episode": 0},
        ],
        "mature_spec": {"quotient": "ecchi", "quotas": {}, "escalation": {},
                        "tone_boundaries": [], "characters": {}, "scene_types": []}})
    return show


class ChunkLLM:
    """Fake that produces a 12-scene skeleton then expands beats in batches."""

    def __init__(self):
        self.calls = []

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "full 12-episode season" in user or "full season of high-level" in user:
            return {
                "season": 1, "title": "Season One",
                "season_arc": {"premise": "p", "central_conflict": "c",
                               "escalation": "e", "finale_payoff": "f"},
                "episodes": [
                    {"episode": i, "title": f"EP{i}", "episode_type": "character",
                     "logline": "A day in the life.",
                     "featured_plotlines": ["p1"], "new_plotline": None,
                     "key_beats": ["beat a", "beat b"],
                     "cliffhanger": "hook", "character_arc": "grows"}
                    for i in range(1, 13)],
            }
        if "The season plan has FIXED this episode" in user:
            return {"episode": 1, "episode_type": "character",
                    "featured_plotlines": [{"id": "p1", "role": "advanced"}],
                    "new_plotline": None, "synopsis": "A day in the life."}
        if "Development stage" in user:
            return {"episode": 1, "episode_type": "character",
                    "featured_plotlines": [{"id": "op", "role": "advanced"}],
                    "new_plotline": None, "synopsis": "A day in the life."}
        if "EPISODE OUTLINE for narrative" in user:        # structure reviewer
            return {"grade": "Pass", "review": ""}
        if "This is an EPISODE OUTLINE" in user:           # outline writers'-room reviewer
            return {"grade": "Pass", "review": ""}
        if "REVISE the EPISODE OUTLINE" in user:           # review revision
            return {"episode": 1, "title": "Test", "episode_type": "character",
                    "threat_of_the_week": "none", "plot": "fixed",
                    "characters": ["Ryou"],
                    "plotline_updates": {"active_plotline_progress": "x",
                                         "dormant_plotline_beat": "y"},
                    "scenes": [{"id": f"s{i:02d}", "location": f"loc{i}",
                                "time_of_day": "Day", "summary": f"scene {i}",
                                "characters": ["Ryou"],
                                "beats": ["Setup: a", "Change: b", "Consequence: c"]}
                               for i in range(1, 13)]}
        self.calls.append("skeleton" if "Write ONE episode's" in user else "beats")
        if "Write ONE episode's" in user:
            return {"episode": 1, "title": "Test", "episode_type": "character",
                    "threat_of_the_week": "none", "plot": "A day in the life.",
                    "characters": ["Ryou"],
                    "plotline_updates": {"active_plotline_progress": "x",
                                         "dormant_plotline_beat": "y"},
                    "scenes": [{"id": f"s{i:02d}", "location": f"loc{i}",
                                "time_of_day": "Day", "summary": f"scene {i}",
                                "characters": ["Ryou"]} for i in range(1, 13)]}
        after = user.split("SCENES TO EXPAND (this batch):\n")[1]
        scenes = json.JSONDecoder().raw_decode(after)[0]
        return {"scenes": [{**s, "beats": [f"Setup: {s['id']} a",
                                          f"Change: {s['id']} b",
                                          f"Consequence: {s['id']} c"]} for s in scenes]}


def test_chunked_plan_generates_full_scene_outline(tmp_path):
    """The outline is written in two phases: one skeleton call, then per-batch
    beats — producing a full 12-scene outline (not capped at 6)."""
    show = _show(tmp_path)
    llm = ChunkLLM()
    plan = generate_episode_plan(show, 1, llm=llm, cfg=Config(tmp_path))
    assert "skeleton" in llm.calls and "beats" in llm.calls
    scenes = plan.get("scenes", [])
    assert len(scenes) == 12
    for sc in scenes:
        assert len(sc.get("beats", [])) == 3
        assert any("Setup" in b for b in sc["beats"])
        assert any("Change" in b for b in sc["beats"])
        assert any("Consequence" in b for b in sc["beats"])
    # The beats came from Phase 2 (not the skeleton's one-liners).
    assert plan["scenes"][0]["beats"][0] == "Setup: s01 a"


def test_chunked_plan_beats_batch_size(tmp_path):
    """plan_beats_batch controls scenes per beat call (2 default -> 6 calls for 12)."""
    show = _show(tmp_path)
    cfg = Config(tmp_path)
    cfg.data["pipeline"]["plan_beats_batch"] = 4
    llm = ChunkLLM()
    plan = generate_episode_plan(show, 1, llm=llm, cfg=cfg)
    beats_calls = [c for c in llm.calls if c == "beats"]
    assert len(beats_calls) == 3          # 12 scenes / 4 per call
    assert len(plan["scenes"]) == 12


def test_staged_outline_skeleton_beats_merge(tmp_path):
    """The board's staged path writes the same 12-scene outline as one shot."""
    from studio.planner import (generate_plan_beats, generate_plan_skeleton,
                                merge_outline_stages, read_episode_plan)

    show = _show(tmp_path)
    cfg = Config(tmp_path)
    cfg.data["pipeline"]["plan_beats_batch"] = 4
    llm = ChunkLLM()
    sk = generate_plan_skeleton(show, 1, llm=llm, cfg=cfg)
    assert sk["scenes"] == 12
    ids = sk["scene_ids"]
    for i in range(0, len(ids), 4):
        generate_plan_beats(show, 1, ids[i:i + 4], llm=llm, cfg=cfg)
    merge_outline_stages(show, 1, llm=llm, cfg=cfg)
    plan = read_episode_plan(show, 1)
    assert len(plan["scenes"]) == 12
    assert plan["scenes"][0]["beats"][0] == "Setup: s01 a"
    assert plan["status"] == "pending"
    assert "plan_review" not in plan


def test_staged_outline_revision_applies_notes(tmp_path):
    """A merge retake rewrites the outline in one call, not the whole pipeline."""
    from studio.planner import (generate_plan_skeleton, merge_outline_stages,
                                read_episode_plan)

    show = _show(tmp_path)
    cfg = Config(tmp_path)
    llm = ChunkLLM()
    generate_plan_skeleton(show, 1, llm=llm, cfg=cfg)
    merge_outline_stages(show, 1, llm=llm, cfg=cfg, notes="make it darker")
    plan = read_episode_plan(show, 1)
    assert plan["plot"] == "fixed"
    assert plan["rejected_notes"] == "make it darker"
    assert len(plan["scenes"]) == 12
    assert "plan_review" not in plan


class _RewriteLLM(ChunkLLM):
    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "REWRITE FEEDBACK" in user:
            return {"scenes": [{
                "id": "s01", "summary": "darker opening", "characters": ["Ryou"],
                "beats": ["Setup: dark", "Change: darker",
                          "Consequence: darkest"]}]}
        return super().chat_json(messages, **kwargs)


def test_rewrite_one_scene_updates_plan_and_beat_file(tmp_path):
    from studio import epstate
    from studio.planner import (generate_plan_beats, generate_plan_skeleton,
                                merge_outline_stages, read_episode_plan,
                                rewrite_plan_scene)

    show = _show(tmp_path)
    cfg = Config(tmp_path)
    cfg.data["pipeline"]["plan_beats_batch"] = 4
    llm = _RewriteLLM()
    sk = generate_plan_skeleton(show, 1, llm=llm, cfg=cfg)
    ids = sk["scene_ids"]
    for i in range(0, len(ids), 4):
        generate_plan_beats(show, 1, ids[i:i + 4], llm=llm, cfg=cfg)
    merge_outline_stages(show, 1, llm=llm, cfg=cfg)

    out = rewrite_plan_scene(show, 1, "s01", "make it darker", llm=llm, cfg=cfg)
    assert out["scene_id"] == "s01"
    plan = read_episode_plan(show, 1)
    assert plan["scenes"][0]["summary"] == "darker opening"
    assert plan["scenes"][0]["beats"][0] == "Setup: dark"
    assert plan["scenes"][1]["summary"] == "scene 2"  # other scenes untouched

    beats = []
    for path in (show.dir / "runs" / "EP01").glob("plan.beats.*.json"):
        beats += (epstate.read_json(path, {}) or {}).get("scenes", [])
    s01 = next(s for s in beats if s.get("id") == "s01")
    assert s01["beats"][0] == "Setup: dark"
