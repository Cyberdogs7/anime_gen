"""Tests for the review-to-prompt learning loop (studio.learn + director_notes wiring)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.config import Config
from studio.learn import aggregate_reviews, distill_rules, summarize
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


def _write_reviews(show: Show):
    rdir = show.dir / "runs" / "EP01" / "reviews"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "continuity.r2.json").write_text(json.dumps({
        "round": 2, "score": 0.0, "pass": False, "notes": [
            {"severity": "HIGH", "scene": "s05", "item": "second spike",
             "note": "s05_sh18 creates a second numbered past-ninety sync; keep single-spike canon"},
            {"severity": "LOW", "scene": "s01", "item": "label",
             "note": "gantry label varies; unify names"}]}), encoding="utf-8")
    (rdir / "slop.r2.json").write_text(json.dumps({
        "round": 2, "score": 0.9, "pass": False, "notes": [
            {"severity": "HIGH", "scene": "s02",
             "note": "[Scene s02] Tessa opens every line with Mm... + number; rewrite with full sentences"}]}),
        encoding="utf-8")


def test_learn_aggregates_high_first(tmp_path):
    show = _make_show(tmp_path)
    _write_reviews(show)
    notes = aggregate_reviews(show.dir, 1)
    assert len(notes) == 3
    # HIGH severity sorts before LOW regardless of reviewer name.
    assert [n["severity"] for n in notes] == ["HIGH", "HIGH", "LOW"]
    s = summarize(notes)
    assert s["total"] == 3 and s["high"] == 2
    assert s["by_reviewer"]["continuity"]["HIGH"] == 1


def test_learn_distills_deduped_rules(tmp_path):
    show = _make_show(tmp_path)
    _write_reviews(show)
    notes = aggregate_reviews(show.dir, 1)
    rules = distill_rules(notes, high_only=True)
    assert len(rules) == 2
    assert all(r.startswith("[") for r in rules)
    # Promoting twice must not duplicate director_notes.
    from studio.planner import add_director_note, read_director_notes
    for r in rules:
        add_director_note(show, r)
    first = read_director_notes(show)
    for r in rules:
        add_director_note(show, r)
    assert read_director_notes(show) == first


def test_scene_detail_carries_standing_constraints():
    from studio.prompts import scene_detail_prompt
    p = scene_detail_prompt({"title": "Demo"}, {"scenes": []}, [], None, "",
                            director_constraints=["keep single-spike canon"])
    assert "keep single-spike canon" in p
    assert "STANDING CONSTRAINTS" in p


def test_effective_shot_notes_merges_standing_and_feedback(tmp_path):
    from studio.planner import _effective_shot_notes, add_director_note
    show = _make_show(tmp_path)
    add_director_note(show, "never invent a second numbered sync spike")
    merged = _effective_shot_notes(show, "fix s05_sh18")
    assert "never invent a second numbered sync spike" in merged
    assert "fix s05_sh18" in merged
    assert "STANDING DIRECTOR CONSTRAINTS" in merged


def test_writer_review_stores_full_notes_with_reviewer(tmp_path):
    from studio.planner import _writer_review
    show = _make_show(tmp_path)

    class FailingLLM:
        def chat_json(self, messages, **kwargs):
            return {"score": 0.9,
                    "notes": [{"severity": "HIGH", "scene": "s01", "note": "a"},
                              {"severity": "HIGH", "scene": "s02", "note": "b"},
                              {"severity": "HIGH", "scene": "s03", "note": "c"},
                              {"severity": "HIGH", "scene": "s04", "note": "d"}],
                    "pass": False}

    _writer_review(show, 1, {"episode": 1, "summary": "x", "cast": ["Ryou"], "scenes": []},
                   llm=FailingLLM(), cfg=Config(tmp_path), max_revisions=1)
    saved = json.loads((show.dir / "runs" / "EP01" / "script.r1.json").read_text(encoding="utf-8"))
    # All 11 reviewers run; each returns 4 notes -> full set stored, not [:3].
    assert len(saved["_review"]["notes"]) > 3
    assert all("reviewer" in n for n in saved["_review"]["notes"])
    # HIGHs sort first.
    assert saved["_review"]["notes"][0]["severity"] == "HIGH"


def test_prose_prompt_does_not_teach_numbered_spike():
    from studio.prompts import episode_prose_prompt
    p = episode_prose_prompt({"tone": [], "genre": []}, [], [], {}, 1320, [], "")
    assert "Ninety-four already" not in p
    assert "second numbered spike" in p or "past-ninety" in p
