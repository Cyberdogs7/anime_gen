"""Tests for Stage 2 + 2a: script generation and the writers'-room revision loop."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.config import Config
from studio.scriptgen import WritersRoom
from studio.show import Show


def _make_show(tmp_path) -> Show:
    for sub in ("characters", "voices", "scenes"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=tmp_path)
    show.write_character({"id": "ryou", "name": "Ryou", "appearance_canonical": "x"})
    show.write_bible({"title": "Demo", "content_policy": "mature",
                      "runtime_target_s": 10,
                      "arcs": [{"id": "a1", "name": "Arc", "beats_total": 6,
                                "beats": [{"id": "b1", "summary": "beat"}]}],
                      "mature_spec": {"quotient": "ecchi", "quotas": {},
                                      "escalation": {}, "tone_boundaries": [],
                                      "characters": {}, "scene_types": []}})
    return show


class FakeWritersLLM:
    def __init__(self):
        self.review_calls = 0

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "WRITERS' ROOM NOTES" in user:      # revision prompt
            return {"episode": 1, "scenes": [{"id": "s01", "shots": [
                {"id": "s01_sh01", "duration_s": 10, "action": "revised",
                 "dialogue": [{"char": "Ryou", "line": str(i), "on_camera": True}
                              for i in range(3)]}]}]}
        if "Write the FULL script" in user:    # script prompt
            return {"episode": 1, "scenes": [{"id": "s01", "shots": [
                {"id": "s01_sh01", "duration_s": 10, "action": "draft",
                 "dialogue": [{"char": "Ryou", "line": str(i), "on_camera": True}
                              for i in range(3)]}]}]}
        self.review_calls += 1                 # reviewers
        passed = self.review_calls > 3         # round 1 fails, round 2 passes
        return {"score": 0.1 if passed else 0.9,
                "notes": [{"scene": "s01", "note": "fix this"}], "pass": passed}


def test_writers_room_revision_loop(tmp_path):
    show = _make_show(tmp_path)
    result = WritersRoom(show, cfg=Config(tmp_path), llm=FakeWritersLLM()).run(episode=1)
    assert result["rounds"] == 2                # draft -> revise
    assert result["passed"] is True             # cleared in round 2
    r1 = show.dir / "runs" / "EP01" / "script.r1.json"
    r2 = show.dir / "runs" / "EP01" / "script.r2.json"
    assert r1.exists() and r2.exists()
    for r in ("slop", "continuity", "fan_service"):
        assert (show.dir / "runs" / "EP01" / "reviews" / f"{r}.r2.json").exists()
    # durations snapped to the H3 grid (10 -> 10.125)
    script = __import__("json").loads(r2.read_text(encoding="utf-8"))
    assert script["scenes"][0]["shots"][0]["duration_s"] == 10.125


def test_runtime_review_flags_short_scripts():
    from studio.scriptgen import _runtime_review
    short = {"scenes": [{"shots": [{"duration_s": 10.125}] * 3}]}      # ~30s vs 1320 target
    assert _runtime_review(short, 1320) is not None
    full = {"scenes": [{"shots": [{"duration_s": 10.125,
              "dialogue": [{"char": "Ryou", "line": "x", "on_camera": True}
                           for _ in range(3)]}] * 130}]}    # ~1316s, 390 lines
    assert _runtime_review(full, 1320) is None


def test_runtime_review_ignores_padded_dialogue():
    from studio.scriptgen import _runtime_review
    # Enough duration and plenty of dialogue ENTRIES, but every line is a stage
    # direction with no spoken words — must still fail the line floor.
    padded = {"scenes": [{"shots": [{"duration_s": 10.125,
               "dialogue": [{"char": "Ryou", "line": "(Grunting)", "on_camera": True},
                            {"char": "Ryou", "line": "(Internal/Unvoiced)", "on_camera": False},
                            {"char": "Ryou", "line": "(Sharp exhale)", "on_camera": True}]}
                              ] * 130}]}
    assert _runtime_review(padded, 1320) is not None
    # A delivery note prefix is fine as long as real words follow.
    spoken = {"scenes": [{"shots": [{"duration_s": 10.125,
              "dialogue": [{"char": "Ryou", "line": "(whispering) Stay close.", "on_camera": True},
                           {"char": "Ryou", "line": "Keep up.", "on_camera": True},
                           {"char": "Ryou", "line": "Move.", "on_camera": True}]}
                                ] * 130}]}
    assert _runtime_review(spoken, 1320) is None


def test_normalize_strips_padded_dialogue_lines():
    from studio.scriptgen import _normalize
    script = {"episode": 1, "cast": ["Ryou"],
              "scenes": [{"id": "s01", "shots": [{
                  "id": "s01_sh01", "duration_s": 10,
                  "dialogue": [{"char": "Ryou", "line": "(Grunting)"},
                               {"char": "Ryou", "line": "(whispering) Stay close."}],
                  "references": {"characters": ["Ryou"]}}]}]}
    _normalize(script, ["Ryou"])
    lines = script["scenes"][0]["shots"][0]["dialogue"]
    assert lines == [{"char": "Ryou", "line": "(whispering) Stay close."}]
