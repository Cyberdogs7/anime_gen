"""Tests for the eval harness (studio/evals.py): metrics, isolation, reporting."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import evals
from studio.config import Config
from studio.show import Show


def _make_show(tmp_path) -> Show:
    show_dir = tmp_path / "shows" / "demo"
    for sub in ("characters", "scenes", "continuity", "voices"):
        (show_dir / sub).mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=show_dir)
    show.write_bible({"title": "Demo", "content_policy": "mature", "runtime_target_s": 60})
    show.write_character({"id": "ren", "name": "Ren", "appearance_canonical": "x"})
    show.write_continuity({"episode": 0, "world": {"facts": []}})
    return show


# ---- pure metric helpers --------------------------------------------------

def test_similarity_identical_is_one():
    s = evals.similarity("The quick brown fox", "the   QUICK brown fox")
    assert s["ratio"] == 1.0 and s["jaccard"] == 1.0


def test_similarity_different_is_low():
    s = evals.similarity("alpha beta gamma", "completely other words entirely")
    assert s["jaccard"] == 0.0
    assert s["ratio"] < 0.6


def test_variance_zero_when_identical():
    v = evals.variance(["same script text here"] * 4)
    assert v["similarity_mean"] == 1.0
    assert v["diversity"] == 0.0
    assert v["pairs"] == 6


def test_variance_positive_when_outputs_differ():
    v = evals.variance(["scene one: a brawl", "scene one: a quiet talk",
                        "scene one: a chase"])
    assert v["similarity_mean"] < 1.0
    assert v["diversity"] > 0.0
    assert v["similarity_min"] <= v["similarity_mean"] <= v["similarity_max"]


def test_variance_needs_two_runs():
    v = evals.variance(["only one"])
    assert v["similarity_mean"] is None and v["pairs"] == 0


def test_reviews_quality_aggregates():
    reviews = {
        "slop": {"grade": "Fail", "pass": False, "review": "fix a"},
        "pacing": {"grade": "Pass", "pass": True, "review": ""},
    }
    q = evals._reviews_quality(reviews)
    assert q["reviewers"] == 2
    assert q["pass_rate"] == 0.5
    assert q["failing"] == ["slop"]
    assert q["by_reviewer"]["slop"]["grade"] == "Fail"


def test_compare_reports_computes_delta():
    a = {"variance": {"similarity_mean": 0.99, "diversity": 0.01},
         "aggregate": {"pass_rate_mean": 0.3, "failing_mean": 8, "words_mean": 100,
                       "calls_total": 40, "seconds_mean": 30}}
    b = {"variance": {"similarity_mean": 0.6, "diversity": 0.4},
         "aggregate": {"pass_rate_mean": 0.7, "failing_mean": 3, "words_mean": 120,
                       "calls_total": 44, "seconds_mean": 28}}
    cmp = evals.compare_reports(a, b, "base", "cand")
    row = {r["metric"]: r for r in cmp["rows"]}
    assert row["similarity_mean"]["delta"] == pytest.approx(-0.39)
    assert row["similarity_mean"]["higher_is_better"] is True
    assert row["failing_mean"]["delta"] == -5


# ---- end-to-end (fake stage + fake LLM) -----------------------------------

class _FakeLLM:
    """Varies its output on every call so run-to-run variance is measurable."""

    def __init__(self):
        self.n = 0
        self.seen_temperatures = []

    def chat(self, messages, **kwargs):
        self.n += 1
        self.seen_temperatures.append(kwargs.get("temperature"))
        return f"draft variant {self.n}: Ren argues with Mira about the contract."

    def chat_json(self, messages, **kwargs):
        self.n += 1
        return {"n": self.n}


def _install_fake_stage(monkeypatch, seen_roots):
    def fake_runner(show, episode, llm, cfg, quality, run_dir):
        seen_roots.append(show.dir)
        text = llm.chat([{"role": "user", "content": "write"}])
        q = ({"pass_rate": 1.0, "failing": []} if quality else {})
        return {"artifact": text, "quality": q,
                "extra": {"scenes": 3, "episode_type": "character"}}

    monkeypatch.setitem(evals._STAGES, "outline", fake_runner)
    monkeypatch.setattr(evals, "_prepare_stage_stage", lambda *a, **k: None)


def test_run_eval_end_to_end_writes_report_and_csv(tmp_path, monkeypatch):
    show = _make_show(tmp_path)
    roots: list[Path] = []
    _install_fake_stage(monkeypatch, roots)
    out = tmp_path / "evals" / "exp1"

    report = evals.run_eval("demo", 1, "outline", runs=3, cfg=Config(tmp_path),
                            llm=_FakeLLM(), out_dir=out, progress=lambda *a, **k: None)

    assert (out / "report.json").exists()
    assert (out / "metrics.csv").exists()
    assert report["variance"]["runs"] == 3
    assert report["aggregate"]["runs_ok"] == 3
    assert report["aggregate"]["calls_total"] == 3
    # Distinct outputs => real variance, not a 99%-identical collapse.
    assert report["variance"]["similarity_mean"] < 1.0
    assert report["variance"]["diversity"] > 0.0
    # Each run got its OWN scratch show dir (isolation), all distinct.
    assert len(roots) == 3
    assert len(set(roots)) == 3
    # Frozen snapshot remained untouched alongside.
    assert (out / "frozen" / "bible.yaml").exists()


def test_run_eval_temperature_override_reaches_client(tmp_path, monkeypatch):
    _make_show(tmp_path)
    roots: list[Path] = []
    _install_fake_stage(monkeypatch, roots)
    fake = _FakeLLM()
    evals.run_eval("demo", 1, "outline", runs=2, cfg=Config(tmp_path),
                   llm=fake, temperature=1.15, quality=False,
                   out_dir=tmp_path / "evals" / "t", progress=lambda *a, **k: None)
    assert fake.seen_temperatures == [1.15, 1.15]


def test_run_eval_prose_requires_a_plan(tmp_path, monkeypatch):
    _make_show(tmp_path)
    with pytest.raises(FileNotFoundError, match="stage=prose needs an approved plan"):
        evals.run_eval("demo", 1, "prose", runs=1, cfg=Config(tmp_path),
                       llm=_FakeLLM(), out_dir=tmp_path / "evals" / "p",
                       progress=lambda *a, **k: None)


def test_run_eval_records_failed_run_without_aborting(tmp_path, monkeypatch):
    _make_show(tmp_path)

    def boom(show, episode, llm, cfg, quality, run_dir):
        raise RuntimeError("model exploded")

    monkeypatch.setitem(evals._STAGES, "outline", boom)
    monkeypatch.setattr(evals, "_prepare_stage_stage", lambda *a, **k: None)
    report = evals.run_eval("demo", 1, "outline", runs=2, cfg=Config(tmp_path),
                            llm=_FakeLLM(), out_dir=tmp_path / "evals" / "boom",
                            progress=lambda *a, **k: None)
    assert report["aggregate"]["runs_ok"] == 0
    assert report["aggregate"]["runs_failed"] == 2
    assert "model exploded" in report["runs_detail"][0]["error"]
