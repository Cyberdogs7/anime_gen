"""Tests for the writers'-room reviewers (free-form grade + review contract)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.review import all_pass, run_reviewers
from studio.config import Config

EXPECTED = {"slop", "continuity", "fan_service", "voice", "exposition",
            "pacing", "stakes", "visual", "agency", "serial", "repetition"}


class FailLLM:
    def chat_json(self, messages, **kwargs):
        return {"grade": "Fail", "review": "needs work: fix the thing in s01"}


class PassLLM:
    def chat_json(self, messages, **kwargs):
        return {"grade": "Pass", "review": ""}


def _script():
    return {"episode": 1, "scenes": [{"id": "s01", "shots": [{"id": "s01_sh01"}]}]}


def test_reviewers_run_and_fail_on_a_failing_verdict(tmp_path):
    cfg = Config(ROOT)
    results = run_reviewers(_script(), cfg=cfg, llm=FailLLM(), episode=1, round_no=1,
                            notes_dir=tmp_path / "reviews")
    assert set(results) == EXPECTED
    assert all_pass(results) is False
    for r in EXPECTED:
        assert (tmp_path / "reviews" / f"{r}.r1.json").exists()
    # LLM reviewers fail on a Fail verdict; repetition is deterministic (1 scene -> pass).
    for r in EXPECTED - {"repetition"}:
        assert results[r]["grade"] == "Fail"
        assert results[r]["pass"] is False


def test_reviewers_all_pass_when_verdict_is_pass(tmp_path):
    cfg = Config(ROOT)
    results = run_reviewers(_script(), cfg=cfg, llm=PassLLM(), episode=1, round_no=1,
                            notes_dir=tmp_path / "reviews")
    assert all_pass(results) is True
    assert all(v["grade"] == "Pass" for v in results.values())


def test_plan_structure_reviewer_uses_grade(tmp_path):
    from studio.review import run_plan_reviewers

    class FakePlanLLM:
        def chat_json(self, messages, **kwargs):
            return {"grade": "Fail", "review": "cold open with zero context"}

    cfg = Config(ROOT)
    plan = {"episode": 1, "plot": "battle",
            "scenes": [{"id": "s01", "summary": "mech attack", "characters": ["Blade"],
                        "beats": ["a", "b", "c"]}]}
    results = run_plan_reviewers(plan, cfg=cfg, llm=FakePlanLLM(),
                                 episode=1, round_no=1, notes_dir=tmp_path / "reviews")
    assert set(results) == {"structure"}
    assert results["structure"]["pass"] is False
    assert results["structure"]["review"] == "cold open with zero context"
    assert (tmp_path / "reviews" / "structure.r1.json").exists()
