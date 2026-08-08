"""Tests for the Stage 2a writers'-room reviewers (slop/continuity/fan_service)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.review import all_pass, run_reviewers
from studio.config import Config


class FakeReviewLLM:
    def __init__(self, slop_score: float = 0.8):
        self.slop_score = slop_score

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"].lower()
        if "scrutinize every scene" in user:
            return {"score": self.slop_score,
                    "notes": [{"scene": "s01", "item": "line", "note": "cliche"}],
                    "pass": self.slop_score <= 0.45}
        if "cross-check the script" in user:
            return {"notes": [{"severity": "HIGH", "item": "s01", "note": "eye color drift"}],
                    "pass": False}
        return {"maturity_score": 0.2, "notes": [{"scene": "s01", "note": "under-delivery"}],
                "pass": False}


def test_reviewers_detect_problems(tmp_path):
    cfg = Config(ROOT)
    script = {"episode": 1, "scenes": [{"id": "s01", "shots": [{"id": "s01_sh01"}]}]}
    results = run_reviewers(script, cfg=cfg, llm=FakeReviewLLM(), episode=1, round_no=1,
                            notes_dir=tmp_path / "reviews")
    assert set(results) == {"slop", "continuity", "fan_service"}
    assert results["slop"]["pass"] is False          # 0.8 > 0.45 threshold
    assert results["continuity"]["pass"] is False
    assert results["fan_service"]["pass"] is False
    assert all_pass(results) is False
    for r in ("slop", "continuity", "fan_service"):
        assert (tmp_path / "reviews" / f"{r}.r1.json").exists()


def test_reviewer_slop_passes_under_threshold(tmp_path):
    cfg = Config(ROOT)
    script = {"episode": 1, "scenes": []}
    results = run_reviewers(script, cfg=cfg, llm=FakeReviewLLM(slop_score=0.3),
                            episode=1, round_no=1, notes_dir=tmp_path / "reviews")
    assert results["slop"]["pass"] is True
