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
        if "Development stage" in user:
            return {"episode": 1, "episode_type": "character",
                    "featured_plotlines": [{"id": "op", "role": "advanced"}],
                    "new_plotline": None, "synopsis": "A day in the life."}
        if "narrative and structural coherence" in user:   # structure reviewer
            return {"score": 0.2, "notes": [], "pass": True}
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
        self.calls.append("skeleton" if "OUTLINE SKELETON" in user else "beats")
        if "OUTLINE SKELETON" in user:
            return {"episode": 1, "title": "Test", "episode_type": "character",
                    "threat_of_the_week": "none", "plot": "A day in the life.",
                    "characters": ["Ryou"],
                    "plotline_updates": {"active_plotline_progress": "x",
                                         "dormant_plotline_beat": "y"},
                    "scenes": [{"id": f"s{i:02d}", "location": f"loc{i}",
                                "time_of_day": "Day", "summary": f"scene {i}",
                                "characters": ["Ryou"]} for i in range(1, 13)]}
        scenes = json.loads(
            user.split("SCENES TO EXPAND (this batch):\n")[1].split("\n\nReturn")[0])
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
