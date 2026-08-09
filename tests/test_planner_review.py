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
