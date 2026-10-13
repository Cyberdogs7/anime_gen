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
    EXPECTED = {"slop", "continuity", "fan_service", "voice", "exposition",
                "pacing", "stakes", "visual", "agency", "serial", "repetition"}
    assert set(results) == EXPECTED
    # The fake returns notes for every reviewer -> every gated reviewer fails.
    assert all_pass(results) is False
    for r in EXPECTED:
        assert (tmp_path / "reviews" / f"{r}.r1.json").exists()


def test_added_reviewer_prompts_emit_severity():
    """The *_block: high gates only work if reviewer schemas emit `severity`.

    Regression guard: the voice/exposition/pacing/stakes/visual/agency/serial
    reviewers are gated with block_high, but their schemas once omitted severity,
    so the gates could never fire (voice distinctness was never enforced).
    """
    from studio import prompts
    calls = {
        "voice": prompts.voice_review_prompt({"scenes": []}, []),
        "exposition": prompts.exposition_review_prompt({"scenes": []}),
        "pacing": prompts.pacing_review_prompt({"scenes": []}, 1320),
        "stakes": prompts.stakes_review_prompt({"scenes": []}, []),
        "visual": prompts.visual_review_prompt({"scenes": []}),
        "agency": prompts.agency_review_prompt({"scenes": []}, []),
        "serial": prompts.serial_review_prompt({"scenes": []}, {}, [], []),
    }
    for name, prompt in calls.items():
        assert '"severity": "HIGH|MEDIUM|LOW"' in prompt, name
    assert "MUST carry a 'severity'" in callable_reviewer_system()


def callable_reviewer_system():
    from studio import prompts
    return prompts.reviewer_system("voice")


def test_reviewer_slop_passes_under_threshold(tmp_path):
    cfg = Config(ROOT)
    script = {"episode": 1, "scenes": []}
    results = run_reviewers(script, cfg=cfg, llm=FakeReviewLLM(slop_score=0.3),
                            episode=1, round_no=1, notes_dir=tmp_path / "reviews")
    assert results["slop"]["pass"] is True


def test_additional_reviewer_block_modes(tmp_path):
    """The new reviewers honor block_high gates (all reviewers now use block_high)."""
    from studio.review import run_reviewers

    class ModeLLM:
        def __init__(self):
            self.calls = 0
        def chat_json(self, messages, **kwargs):
            user = messages[-1]["content"]
            self.calls += 1
            if "SERIAL ARC & GROWTH" in user:
                # serial_block is 'high' -> a LOW note should not fail it.
                return {"score": 0.3, "pass": True,
                        "notes": [{"severity": "LOW", "item": "arc",
                                   "note": "minor nit"}]}
            # All reviewers now use block_high -> LOW notes pass, HIGH notes fail.
            return {"score": 0.6, "pass": True,
                    "notes": [{"severity": "LOW", "item": "x", "note": "nit"}]}

    cfg = Config(ROOT)
    llm = ModeLLM()
    script = {"episode": 1, "scenes": []}
    results = run_reviewers(script, cfg=cfg, llm=llm, episode=1, round_no=1,
                            notes_dir=tmp_path / "reviews")
    assert results["serial"]["pass"] is True          # low note, block_high
    assert results["voice"]["pass"] is True           # low note, block_high
    assert results["exposition"]["pass"] is True

    # Now test that HIGH notes still block.
    class HighNoteLLM:
        def chat_json(self, messages, **kwargs):
            return {"score": 0.6, "pass": True,
                    "notes": [{"severity": "HIGH", "item": "x", "note": "critical"}]}

    llm2 = HighNoteLLM()
    results2 = run_reviewers(script, cfg=cfg, llm=llm2, episode=1, round_no=2,
                             notes_dir=tmp_path / "reviews2")
    assert results2["voice"]["pass"] is False         # HIGH note -> block


def test_plan_reviewers_run_structure_reviewer(tmp_path):
    from studio.review import run_plan_reviewers

    class FakePlanReviewLLM:
        def chat_json(self, messages, **kwargs):
            return {"score": 0.9,
                    "notes": [{"scene": "overall", "item": "episode 1",
                               "severity": "HIGH",
                               "note": "cold open, zero context"}],
                    "pass": False}

    cfg = Config(ROOT)
    plan = {"episode": 1, "plot": "battle",
            "scenes": [{"id": "s01", "summary": "mech attack", "characters": ["Blade"],
                        "beats": ["a", "b", "c"]}]}
    results = run_plan_reviewers(plan, cfg=cfg, llm=FakePlanReviewLLM(),
                                 episode=1, round_no=1, notes_dir=tmp_path / "reviews")
    assert set(results) == {"structure"}
    assert results["structure"]["pass"] is False
    assert results["structure"]["notes"][0]["note"] == "cold open, zero context"
    assert (tmp_path / "reviews" / "structure.r1.json").exists()


def test_plan_reviewer_gate_ignores_model_pass_verdict(tmp_path):
    """A structure reviewer that writes notes while claiming pass=True is a
    broken reviewer — the GATE must still fail so the outline is revised."""
    from studio.review import run_plan_reviewers

    class LyingReviewerLLM:
        def chat_json(self, messages, **kwargs):
            return {"score": 0.5,
                    "notes": [{"scene": "s01", "item": "characters",
                               "severity": "HIGH",
                               "note": "Dahlia in beats but missing from character list"}],
                    "pass": True}   # model contradicts itself

    cfg = Config(ROOT)
    plan = {"episode": 1, "plot": "battle",
            "scenes": [{"id": "s01", "summary": "mech attack", "characters": ["Blade"],
                        "beats": ["a", "b", "c"]}]}
    results = run_plan_reviewers(plan, cfg=cfg, llm=LyingReviewerLLM(),
                                 episode=1, round_no=1, notes_dir=tmp_path / "reviews")
    assert results["structure"]["pass"] is False   # HIGH note -> block


def test_plan_reviewer_low_notes_block_by_default(tmp_path):
    """Zero-issue default (structure_block='any'): ANY note blocks the outline —
    LOW/MEDIUM are no longer waived, so no known problem ships."""
    from studio.review import run_plan_reviewers

    class AdvisoryLLM:
        def chat_json(self, messages, **kwargs):
            return {"score": 0.7,
                    "notes": [{"scene": "s01", "item": "beats",
                               "severity": "LOW",
                               "note": "minor tone inconsistency"}],
                    "pass": True}

    cfg = Config(ROOT)
    plan = {"episode": 1, "plot": "battle",
            "scenes": [{"id": "s01", "summary": "mech attack", "characters": ["Blade"],
                        "beats": ["a", "b", "c"]}]}
    results = run_plan_reviewers(plan, cfg=cfg, llm=AdvisoryLLM(),
                                 episode=1, round_no=1, notes_dir=tmp_path / "reviews")
    assert results["structure"]["pass"] is False   # any note -> block


def test_plan_reviewer_high_mode_allows_low_notes(tmp_path):
    """structure_block='high' restores HIGH-only blocking (LOW stays advisory)."""
    from studio.review import run_plan_reviewers

    class AdvisoryLLM:
        def chat_json(self, messages, **kwargs):
            return {"score": 0.7,
                    "notes": [{"scene": "s01", "item": "beats",
                               "severity": "LOW",
                               "note": "minor tone inconsistency"}],
                    "pass": True}

    cfg = Config(ROOT)
    cfg["reviewers"].setdefault("thresholds", {})["structure_block"] = "high"
    plan = {"episode": 1, "plot": "battle",
            "scenes": [{"id": "s01", "summary": "mech attack", "characters": ["Blade"],
                        "beats": ["a", "b", "c"]}]}
    results = run_plan_reviewers(plan, cfg=cfg, llm=AdvisoryLLM(),
                                 episode=1, round_no=1, notes_dir=tmp_path / "reviews")
    assert results["structure"]["pass"] is True   # LOW note -> advisory under 'high'
