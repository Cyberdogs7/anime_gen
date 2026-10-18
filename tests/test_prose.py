"""Tests for the prose-first pipeline (studio/prose.py)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.prose import parse_prose_scenes, pseudo_script
from studio.show import Show


PROSE = """=== s01 | Dojo | Ryou, Mira ===
Morning light. Ryou stretches while Mira watches from the door.

RYOU: You gonna stare all day or spar?
MIRA: (flat) Spar. Then you buy noodles.

=== s02 | Rooftop | Ryou ===
Wind. No one speaks.
(silent)
"""


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


def test_parse_prose_scenes_splits_headers_and_dialogue():
    scenes = parse_prose_scenes(PROSE)
    assert [s["id"] for s in scenes] == ["s01", "s02"]
    assert scenes[0]["location"] == "Dojo"
    assert scenes[0]["characters"] == ["Ryou", "Mira"]
    assert len(scenes[0]["dialogue"]) == 2
    assert scenes[0]["dialogue"][1]["delivery"] == "flat"
    assert scenes[1]["silent"] is True


def test_pseudo_script_shapes_reviewers_input():
    scenes = parse_prose_scenes(PROSE)
    script = pseudo_script(1, scenes, "demo plot")
    assert script["episode"] == 1
    assert len(script["scenes"]) == 2
    s01 = script["scenes"][0]
    assert s01["shots"]  # paragraphs become placeholder shots
    assert sum(len(sh.get("dialogue", [])) for sh in s01["shots"]) == 2


def test_episode_prose_prompt_is_not_json():
    from studio.prompts import episode_prose_prompt, prose_revision_prompt
    p = episode_prose_prompt({"tone": [], "genre": []}, [], [], {}, 1320, [], "")
    assert "NO JSON" in p or "No JSON" in p
    assert "=== s01" in p
    assert "Return ONLY valid JSON" not in p
    r = prose_revision_prompt("=== s01 | Dojo | Ryou ===\nHi.", "[slop] fix it")
    assert "NO JSON" in r or "No JSON" in r


def test_prose_review_loop_revises_prose(tmp_path):
    from studio.prose import review_episode_prose, write_prose_review_loop
    show = _make_show(tmp_path)

    class FakeReviewLLM:
        def __init__(self):
            self.calls = 0
        def chat_json(self, messages, **kwargs):
            self.calls += 1
            # 11 reviewers round 1 fail, round 2+ pass
            passed = self.calls > 11
            return {"score": 0.0 if passed else 0.9, "pass": passed,
                    "notes": [] if passed else [{"severity": "HIGH", "scene": "s01",
                                                 "note": "merge the spar setup faster"}]}

    class FakeChatLLM(FakeReviewLLM):
        def chat(self, messages, **kwargs):
            return PROSE  # revision echoes valid prose

    llm = FakeChatLLM()
    from studio.config import Config
    prose, reviews = write_prose_review_loop(show, 1, PROSE, llm=llm,
                                             cfg=Config(tmp_path), max_revisions=2)
    assert "=== s01" in prose
    assert llm.calls > 11  # second round ran
    assert (show.dir / "runs" / "EP01" / "reviews" / "slop.pr1.json").exists()


def test_prose_dialogue_spine_is_cast_resolved(tmp_path):
    from studio.prose import parse_prose_scenes, write_prose_dialogue_spine
    show = _make_show(tmp_path)
    scenes = parse_prose_scenes(PROSE)
    spine = write_prose_dialogue_spine(show, 1, scenes)
    # "MIRA" resolves to the canonical cast name; unknown speakers pass through.
    assert spine["s01"][0]["char"] == "Ryou"
    assert spine["s01"][1]["delivery"] == "flat"
    assert "s02" in spine  # silent scene kept with []


def test_assemble_runs_the_scene_details_path(tmp_path, monkeypatch):
    """Assemble builds shots from the APPROVED scene details (narrative + dialogue)
    — no prose pass, and the approved dialogue is carried verbatim into the spine.
    Checkpoints still resume."""
    import json as _json
    import studio.config as _cfg
    from studio.config import Config
    from studio.show import Show

    monkeypatch.setattr(_cfg, "_default", _cfg.Config(tmp_path))
    cfg = Config(tmp_path)
    show = Show("demo")
    for sub in ("characters", "voices", "scenes"):
        (show.dir / sub).mkdir(parents=True, exist_ok=True)
    show.write_character({"id": "ryou", "name": "Ryou", "appearance_canonical": "x"})
    show.write_bible({"title": "Demo", "content_policy": "mature",
                      "runtime_target_s": 30,
                      "mature_spec": {"quotient": "ecchi", "quotas": {},
                                      "escalation": {}, "tone_boundaries": [],
                                      "characters": {}, "scene_types": []}})
    d = show.dir / "runs" / "EP01"
    (d / "scenes").mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(_json.dumps(
        {"episode": 1, "status": "approved", "plot": "demo",
         "characters": ["Ryou"], "scenes": []}), encoding="utf-8")
    (d / "scene_details.json").write_text(_json.dumps(
        {"status": "approved", "scenes": [
            {"id": "s01", "location": "Dojo", "summary": "x",
             "narrative": "They spar.", "characters": ["Ryou"],
             "dialogue_beats": [{"char": "Ryou", "line": "Spar now."}]},
            {"id": "s02", "location": "Dojo", "summary": "y",
             "narrative": "They rest.", "characters": ["Ryou"],
             "dialogue_beats": [{"char": "Ryou", "line": "Rest."}]}]}),
        encoding="utf-8")
    # Shot checkpoints so no shot-pass LLM is needed.
    for sid in ("s01", "s02"):
        (d / "scenes" / f"{sid}_shots.json").write_text(_json.dumps(
            {"id": sid, "location": "Dojo", "summary": "x",
             "shots": [{"id": f"{sid}_sh01", "duration_s": 8.0,
                        "action": "they spar",
                        "dialogue": [{"char": "Ryou", "line": "Spar now."}],
                        "references": {"characters": ["Ryou"]}}]}),
            encoding="utf-8")

    class FakeLLM:
        def chat(self, messages, **kwargs):
            return PROSE
        def chat_json(self, messages, **kwargs):
            return {"score": 0.0, "pass": True, "notes": []}

    from studio.planner import assemble_episode_script
    script = assemble_episode_script(show, 1, llm=FakeLLM(), cfg=cfg)
    # No prose pass anymore.
    assert not (d / "episode_prose.txt").exists()
    # Scenes come from the scene details.
    assert [s["id"] for s in script["scenes"]] == ["s01", "s02"]
    # The approved dialogue was carried verbatim into the spine.
    dlg = (d / "dialogue.txt").read_text(encoding="utf-8")
    assert "Ryou: Spar now." in dlg and "Ryou: Rest." in dlg
    assert (d / "script.r1.json").exists()
