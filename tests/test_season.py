"""Tests for the season-first script hierarchy.

Season (12 high-level plot points) -> Episode synopsis -> Outline -> Scenes -> Shots.
The season entry is strict canon; rolls are gone.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.config import Config
from studio.show import Show


def _make_show(tmp_path) -> Show:
    for sub in ("characters", "voices", "scenes"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=tmp_path)
    show.write_character({"id": "ryou", "name": "Ryou", "appearance_canonical": "x"})
    show.write_bible({
        "title": "Demo", "content_policy": "mature", "runtime_target_s": 30,
        "overall_plotline": {"id": "op", "name": "Overall", "characters": ["Ryou"],
                             "summary": "the driving force"},
        "plotlines": [{"id": "p1", "name": "Rivalry", "characters": ["Ryou"],
                       "status": "active", "summary": "daily rivalry"}],
        "mature_spec": {"quotient": "ecchi", "quotas": {}, "escalation": {},
                        "tone_boundaries": [], "characters": {}, "scene_types": []}})
    return show


class _NullLLM:
    """No-network stub: new-location briefs resolve to nothing."""

    def chat_json(self, messages, **kwargs):
        return {"locations": []}


class SeasonLLM:
    """Answers season-plan + season-synopsis + outline + review calls."""

    def __init__(self):
        self.season_calls = 0

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "full 12-episode season" in user:
            self.season_calls += 1
            return {
                "season": 1, "title": "Season One",
                "season_arc": {"premise": "p", "central_conflict": "c",
                               "escalation": "e", "finale_payoff": "f"},
                "episodes": [
                    {"episode": i, "title": f"EP{i}", "episode_type": "character",
                     "logline": f"logline {i}",
                     "featured_plotlines": ["p1"], "new_plotline": None,
                     "key_beats": [f"beat a {i}", f"beat b {i}"],
                     "cliffhanger": f"hook {i}", "character_arc": "grows"}
                    for i in range(1, 13)],
            }
        if "The season plan has FIXED this episode" in user:
            return {"episode": 1, "episode_type": "character",
                    "featured_plotlines": [{"id": "p1", "role": "advanced"}],
                    "new_plotline": None,
                    "synopsis": "Ryou faces the rivalry head-on."}
        if "Series-Growth reviewer" in user:
            return {"approved": False, "notes": ["no"], "plotline": {}}
        if "narrative and structural coherence" in user:
            return {"score": 0.1, "notes": [], "pass": True}
        if "OUTLINE SKELETON" in user:
            return {"episode": 1, "title": "EP1", "episode_type": "character",
                    "threat_of_the_week": "none", "plot": "Ryou faces the rivalry.",
                    "characters": ["Ryou"],
                    "plotline_updates": {"active_plotline_progress": "x",
                                         "dormant_plotline_beat": "y"},
                    "scenes": [{"id": "s01", "location": "loc1",
                                "time_of_day": "Day", "summary": "scene one",
                                "characters": ["Ryou"]}]}
        if "SCENES TO EXPAND" in user:
            import json as _json
            scenes = _json.loads(
                user.split("SCENES TO EXPAND (this batch):\n")[1].split("\n\nReturn")[0])
            return {"scenes": [{**s, "beats": ["Setup: a", "Change: b",
                                               "Consequence: c"]} for s in scenes]}
        raise AssertionError(f"unexpected LLM call: {user[:120]}")


def test_season_plan_generates_twelve_episodes(tmp_path):
    from studio.season import generate_season_plan
    show = _make_show(tmp_path)
    llm = SeasonLLM()
    data = generate_season_plan(show, 1, 12, llm=llm, cfg=Config(tmp_path),
                                start_episode=1)
    assert len(data["episodes"]) == 12
    assert data["episodes"][0]["episode"] == 1
    assert data["episodes"][-1]["episode"] == 12
    assert show.season_path(1).exists()
    entry = [e for e in data["episodes"] if e["episode"] == 1][0]
    assert entry["featured_plotlines"] == ["p1"]
    assert entry["episode_type"] == "character"


def test_develop_episode_uses_season_entry_not_rolls(tmp_path):
    from studio.development import develop_episode
    show = _make_show(tmp_path)
    show.write_season({
        "season": 1, "status": "approved", "num_episodes": 12, "start_episode": 1,
        "season_arc": {}, "episodes": [
            {"episode": 1, "title": "Premiere", "episode_type": "battle",
             "logline": "A threat arrives.", "featured_plotlines": ["p1"],
             "new_plotline": None, "key_beats": ["a threat arrives", "ryou responds"],
             "cliffhanger": "hook", "character_arc": "grows"}]},
        1)
    llm = SeasonLLM()
    dev = develop_episode(show, 1, llm=llm, cfg=Config(tmp_path))
    assert dev["episode_type"] == "battle"
    assert dev["featured"] == ["p1"]
    assert dev["season"] == 1
    assert "threat" in dev["synopsis"].lower() or "rivalry" in dev["synopsis"].lower()


def test_approve_plan_refuses_season_type_drift(tmp_path):
    from studio.planner import approve_plan, _plan_path
    import json
    show = _make_show(tmp_path)
    show.write_season({
        "season": 1, "status": "approved", "num_episodes": 12, "start_episode": 1,
        "season_arc": {}, "episodes": [
            {"episode": 1, "title": "Quiet Day", "episode_type": "character",
             "logline": "daily life.", "featured_plotlines": ["p1"],
             "new_plotline": None, "key_beats": ["daily life"],
             "cliffhanger": "", "character_arc": ""}]},
        1)
    plan = {"episode": 1, "episode_type": "battle", "threat_of_the_week": "mech",
            "plot": "a battle", "characters": ["Ryou"], "cast": ["Ryou"],
            "scenes": [{"id": "s01", "location": "l", "summary": "s",
                        "characters": ["Ryou"], "beats": ["a"]}],
            "plan_review": {"passed": True, "rounds": 1, "notes": []},
            "status": "pending",
            "_plan_dev": {"featured": ["p1"], "new_plotline": None,
                          "episode_type": "battle"},
            "_season": {"season": 1, "entry": {
                "episode": 1, "episode_type": "character",
                "featured_plotlines": ["p1"]}}}
    p = _plan_path(show, 1)
    p.write_text(json.dumps(plan), encoding="utf-8")
    try:
        approve_plan(show, 1)
    except ValueError as exc:
        assert "season" in str(exc).lower()
    else:
        raise AssertionError("expected season mismatch to block approval")
    # Human override still works.
    out = approve_plan(show, 1, override=True, llm=_NullLLM(), cfg=Config(tmp_path))
    assert out["status"] == "approved"
    assert "_season" not in out and "_plan_dev" not in out
