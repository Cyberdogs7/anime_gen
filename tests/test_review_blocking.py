"""Tests for the free-form review → revision path (failing_reviews / revise_script)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.config import Config
from studio.planner import revise_script
from studio.review import failing_reviews, review_notes_text
from studio.show import Show


def test_failing_reviews_and_notes_text():
    results = {"slop": {"pass": False, "grade": "Fail", "review": "fix a"},
               "pacing": {"pass": True, "grade": "Pass", "review": ""}}
    assert failing_reviews(results) == {"slop": "fix a"}
    assert review_notes_text(results) == "[slop] fix a"
    assert review_notes_text({"x": {"pass": True, "review": "ok"}}) == ""


def _show(tmp_path) -> Show:
    d = tmp_path / "shows" / "demo"
    (d / "characters").mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=d)
    show.write_bible({"title": "Demo", "content_policy": "mature", "runtime_target_s": 20})
    show.write_character({"id": "ren", "name": "Ren", "appearance_canonical": "x"})
    show.write_continuity({"episode": 0})
    return show


def _script() -> dict:
    return {
        "episode": 1, "summary": "x", "cast": ["Ren"],
        "scenes": [{"id": "s01", "location": "dojo", "summary": "x", "characters": ["Ren"],
                    "shots": [{"id": "s01_sh01", "duration_s": 10, "action": "a",
                               "dialogue": [{"char": "Ren", "line": "One."}],
                               "references": {"characters": ["Ren"]}}]}],
        "_review": {"passed": False, "rounds": 1,
                    "reviews": {"slop": {"grade": "Fail", "pass": False,
                                         "review": "add nudity in the bath scene"}}},
    }


class _RevLLM:
    def chat_json(self, messages, **kw):
        # the revision prompt asks for {"script": {...}}
        assert "REVIEW NOTES" in messages[-1]["content"]
        return {"script": _script()}

    def chat(self, messages, **kw):
        return "{}"


def test_revise_script_writes_next_revision_from_notes(tmp_path):
    show = _show(tmp_path)
    runs = show.dir / "runs" / "EP01"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "script.r1.json").write_text(json.dumps(_script()), encoding="utf-8")

    review = _script()["_review"]
    out = revise_script(show, 1, _script(), review, cfg=Config(tmp_path), llm=_RevLLM())
    assert out.get("scenes")
    assert (runs / "script.r2.json").exists()
    # the persisted revision is unreviewed (fresh verdict expected)
    assert "_review" not in out


def test_revise_script_noop_when_nothing_failed(tmp_path):
    show = _show(tmp_path)
    script = _script()
    out = revise_script(show, 1, script,
                        {"slop": {"pass": True, "grade": "Pass", "review": ""}},
                        cfg=Config(tmp_path), llm=_RevLLM())
    assert out is script
