"""Tests for series growth: new-plotline introduction, LLM review gate, canon persistence,
continuity-merged state tracker, and dialogue retention for unsheeted script-cast members."""
import json
import sys
from pathlib import Path

import pytest

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
        "plotlines": [{"id": "p1", "name": "Plot 1", "characters": ["Ryou"],
                       "status": "active", "summary": "a standing thread"}],
        "mature_spec": {"quotient": "ecchi", "quotas": {}, "escalation": {},
                        "tone_boundaries": [], "characters": {}, "scene_types": []}})
    return show


class GrowthLLM:
    """Development LLM that proposes a new plotline; growth reviewer approves."""
    def __init__(self, approve: bool = True):
        self.approve = approve

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "Series-Growth reviewer" in user and "proposed a NEW plotline" in user:
            return {"approved": self.approve,
                    "notes": ["fits the world"] if self.approve else ["conflicts with bible"],
                    "plotline": {"id": "new_thread", "name": "New Thread",
                                 "characters": ["Ryou", "Mira"], "summary": "a new mystery"}}
        if "Series-Growth reviewer" in user and "new supporting character" in user:
            return {"approved": True, "notes": ["distinct"]}
        if "Flesh out a NEW supporting anime character" in user:
            return {"name": "Mira", "appearance_canonical": "short violet hair, grey eyes",
                    "personality": ["curious"], "traits_for_llm": "soft-spoken",
                    "voice": {"mode": "manual", "voice_description": "soft alto"}}
        return {"episode": 1,
                "featured_plotlines": [{"id": "op", "role": "advanced"}] +
                                       ([{"id": "mech", "role": "advanced"}]
                                        if "ALREADY FIXED: BATTLE" in user else []),
                "new_plotline": {"id": "new_thread", "name": "New Thread",
                                 "characters": ["Ryou", "Mira"], "summary": "a new mystery"},
                "synopsis": "A new mystery surfaces."}


# --- A1: state tracker merges continuity-only plotlines --------------------

def test_state_tracker_merges_continuity_plotlines(tmp_path):
    from studio.planner import state_tracker_data
    show = _make_show(tmp_path)
    cont = show.read_continuity()
    cont.setdefault("plotlines", []).append({
        "id": "grown", "name": "Grown", "characters": ["Mira"],
        "status": "active", "summary": "added later", "last_seen_episode": 2})
    show.write_continuity(cont)
    st = state_tracker_data(show, 3)
    assert "grown" in st["active"], "continuity-only plotline must appear in active"
    assert "new_thread" not in st["active"]


# --- A3: dialogue retained for script-cast members without a sheet ----------

def test_normalize_keeps_unsheetd_script_cast_dialogue(tmp_path):
    from studio.scriptgen import _normalize
    script = {"cast": ["Ryou", "Mira"], "scenes": [{"id": "s01", "shots": [
        {"id": "s01_sh01", "duration_s": 10.125, "action": "x",
         "dialogue": [{"char": "Mira", "line": "hello", "on_camera": True}]}]}]}
    out = _normalize(script, ["Ryou"])
    assert out["scenes"][0]["shots"][0]["dialogue"][0]["char"] == "Mira"


# --- growth: approve / reject / persist --------------------------------------

def test_development_is_read_only_then_commits_on_approval(tmp_path):
    """develop_episode must NOT write continuity/bible (the EP01 regen-pollution
    bug), and the plotline commit must only happen when the plan is approved."""
    from studio.development import develop_episode
    from studio.growth import commit_plotline_on_approval

    show = _make_show(tmp_path)
    before_bible = json.dumps(show.read_bible(), sort_keys=True)
    before_cont = json.dumps(show.read_continuity(), sort_keys=True)

    import random
    # Seed so the roll selects NEW (the fake LLM always invents new_thread).
    dev = develop_episode(show, 1, llm=GrowthLLM(approve=True), cfg=Config(tmp_path),
                          rng=random.Random(1))
    assert isinstance(dev, dict) and dev.get("synopsis")
    assert dev["featured"] and "op" in dev["featured"]
    if dev["new_plotline"]:
        assert dev["new_plotline"]["id"] == "new_thread"

    # Development is READ-ONLY: nothing persisted.
    assert json.dumps(show.read_bible(), sort_keys=True) == before_bible
    assert json.dumps(show.read_continuity(), sort_keys=True) == before_cont
    assert "Mira" not in {show.read_character(cid).get("name")
                          for cid in show.list_characters()}

    # Approval commits: featured last_seen, new plotline into bible + continuity,
    # and its new character sheet.
    commit_plotline_on_approval(show, dev["featured"], dev["new_plotline"], 1,
                                llm=GrowthLLM(approve=True), cfg=Config(tmp_path))
    ids = [p.get("id") for p in show.read_bible().get("plotlines", [])]
    assert "new_thread" in ids
    cont = show.read_continuity()
    assert any(p.get("id") == "new_thread" for p in cont.get("plotlines", []))
    sheets = {show.read_character(cid).get("name")
              for cid in show.list_characters()}
    assert "Mira" in sheets

    # Idempotent: committing the same plotline again doesn't duplicate it.
    commit_plotline_on_approval(show, dev["featured"], dev["new_plotline"], 1,
                                llm=GrowthLLM(approve=True), cfg=Config(tmp_path))
    ids2 = [p.get("id") for p in show.read_bible().get("plotlines", [])]
    assert ids2.count("new_thread") == 1
    cont2 = show.read_continuity()
    assert [p.get("id") for p in cont2.get("plotlines", [])].count("new_thread") == 1


def test_process_new_plotline_approves_and_persists_to_bible(tmp_path):
    from studio.growth import process_new_plotline
    show = _make_show(tmp_path)
    proposal = {"id": "new_thread", "name": "New Thread",
                "characters": ["Ryou", "Mira"], "summary": "a new mystery"}
    result = process_new_plotline(show, proposal, 1, llm=GrowthLLM(approve=True),
                                  cfg=Config(tmp_path))
    assert result["approved"] is True
    bible = show.read_bible()
    ids = [p.get("id") for p in bible.get("plotlines", [])]
    assert "new_thread" in ids, "approved plotline must land in the bible"
    cont = show.read_continuity()
    assert any(p.get("id") == "new_thread" for p in cont.get("plotlines", []))
    # new character sheet created, voice = manual
    sheets = {c.get("name"): c for c in
              (show.read_character(cid) for cid in show.list_characters())}
    assert "Mira" in sheets
    assert sheets["Mira"]["voice"]["mode"] == "manual"


def test_process_new_plotline_reject_does_not_touch_canon(tmp_path):
    from studio.growth import process_new_plotline
    show = _make_show(tmp_path)
    proposal = {"id": "bad", "name": "Bad", "characters": ["Ryou"], "summary": "no"}
    result = process_new_plotline(show, proposal, 1, llm=GrowthLLM(approve=False),
                                  cfg=Config(tmp_path))
    assert result["approved"] is False
    ids = [p.get("id") for p in show.read_bible().get("plotlines", [])]
    assert "bad" not in ids
    names = {show.read_character(cid).get("name") for cid in show.list_characters()}
    assert names == {"Ryou"}


# --- B: cadence pressure appears in the development prompt ------------------

def test_development_prompt_cadence_rule(tmp_path):
    from studio import prompts
    show = _make_show(tmp_path)
    continuity = show.read_continuity()
    text = prompts.development_prompt(1, {"id": "op", "name": "Overall",
                                          "characters": ["Ryou"], "summary": "s"},
                                      show.read_bible().get("plotlines", []),
                                      [], continuity, [], [], cadence=3,
                                      episodes_since_new=3, new_roll=True)
    assert "roll selected a NEW plotline" in text
    quiet = prompts.development_prompt(1, None, [], [], continuity, [], [],
                                       cadence=3, episodes_since_new=1)
    assert "roll selected a NEW plotline" not in quiet
    assert "NOT rolled this episode" in quiet   # schema says new_plotline is null


def test_character_episodes_exclude_battle_plotlines(tmp_path):
    """A character episode must deterministically exclude battle/combat plotlines
    from what development can feature — no more re-indexing on Apex-734."""
    from studio.development import _plotline_state, develop_episode

    show = _make_show(tmp_path)
    # Add a battle plotline + a character plotline.
    b = show.read_bible()
    b["plotlines"] = [
        {"id": "rival", "name": "Rivalry", "kind": "character",
         "characters": ["Ryou"], "summary": "daily rivalry", "status": "active",
         "last_seen_episode": 0},
        {"id": "mech", "name": "Mech Rampage", "kind": "battle",
         "characters": ["Apex-734"], "summary": "giant mech attack", "status": "active",
         "last_seen_episode": 0},
    ]
    show.write_bible(b)

    pl = _plotline_state(show, b)
    kinds = {p.get("id"): p.get("kind") for p in pl}
    assert kinds == {"rival": "character", "mech": "battle"}

    # Force a deterministic roll with a fixed seed so the outcome is repeatable.
    import random
    rng = random.Random(12345)
    # Fresh episode, no battle yet: battle cadence = 3 means battle plotlines are
    # NOT in the pool -> cannot be featured regardless of the roll.
    for _ in range(20):
        dev = develop_episode(show, 1, llm=GrowthLLM(approve=True), cfg=Config(tmp_path),
                              rng=rng)
        assert dev["episode_type"] == "character"
        assert "mech" not in dev["featured"], "battle plotline leaked into character episode"


def test_battle_cadence_allows_battle_when_due(tmp_path):
    """After battle_cadence episodes without a battle, battle plotlines enter the
    pool and CAN be featured (with a battle arc length)."""
    from studio.development import develop_episode

    show = _make_show(tmp_path)
    b = show.read_bible()
    b["plotlines"] = [
        {"id": "rival", "name": "Rivalry", "kind": "character",
         "characters": ["Ryou"], "summary": "daily rivalry", "status": "active",
         "last_seen_episode": 0},
        {"id": "mech", "name": "Mech Rampage", "kind": "battle",
         "characters": ["Apex-734"], "summary": "giant mech attack", "status": "active",
         "last_seen_episode": 0},
    ]
    show.write_bible(b)
    cont = show.read_continuity()
    cont["last_battle_episode"] = 1   # last battle was episode 1
    show.write_continuity(cont)

    import random
    # Episode 4: episodes_since_battle = 3 >= battle cadence 3 -> battle plotline
    # is in the pool. Roll many times and confirm the mech can be selected.
    any_battle = False
    rng = random.Random(999)
    for _ in range(40):
        dev = develop_episode(show, 4, llm=GrowthLLM(approve=True), cfg=Config(tmp_path),
                              rng=rng)
        if dev["episode_type"] == "battle" and "mech" in dev["featured"]:
            any_battle = True
            break
    assert any_battle, "battle plotline should be featureable when cadence allows"


def test_arc_roll_and_commit_bookkeeping(tmp_path):
    """The roll selects a fixed number of plotlines (running arcs override, NEW is
    an option) and approval applies arc/cooldown bookkeeping."""
    from studio.development import (develop_episode, roll_episode_plotlines,
                                    _plotline_state)
    from studio.growth import commit_plotline_on_approval

    show = _make_show(tmp_path)
    b = show.read_bible()
    b["plotlines"] = [
        {"id": "rival", "name": "Rivalry", "kind": "character",
         "characters": ["Ryou"], "summary": "daily rivalry", "status": "active",
         "last_seen_episode": 0},
        {"id": "debt", "name": "Debt", "kind": "character",
         "characters": ["Ryou"], "summary": "a debt", "status": "active",
         "last_seen_episode": 0},
    ]
    show.write_bible(b)

    # Running arc overrides: mark "rival" as mid-arc with 2 episodes left.
    from studio.development import _plotline_state as _pl_state
    merged = _pl_state(show, show.read_bible())
    for p in merged:
        if p.get("id") == "rival":
            p["arc_remaining"] = 2
    cont = show.read_continuity()
    cont["plotlines"] = merged
    show.write_continuity(cont)

    pl = _plotline_state(show, b)
    roll = roll_episode_plotlines(show, 2, pl, cfg=Config(tmp_path),
                                  rng=__import__("random").Random(7))
    assert "rival" in roll["featured"], "running arc must always be featured"
    assert len(roll["featured"]) >= 1 and len(roll["featured"]) <= 4
    # Every featured non-running plotline got a fresh arc length 1-6.
    for pid in roll["featured"]:
        if pid != "rival" and pid in roll["arc_lengths"]:
            assert 1 <= roll["arc_lengths"][pid] <= 6

    # Commit on approval: running arc decrements 2 -> 1.
    commit_plotline_on_approval(show, roll["featured"], None, 2, cfg=Config(tmp_path),
                                episode_type=roll["episode_type"],
                                arc_lengths=roll["arc_lengths"],
                                new_arc_length=roll["new_arc_length"])
    cont = show.read_continuity()
    rival = next(p for p in cont["plotlines"] if p.get("id") == "rival")
    assert rival["arc_remaining"] == 1
    assert rival["cooldown_remaining"] in (0, None)


def test_roll_excludes_overall_duplicate(tmp_path):
    """A plotline that duplicates the overall plotline by name is the same thread
    and must not be rolled separately (the blades_romance / blades-romance case)."""
    from studio.development import roll_episode_plotlines, _plotline_state

    show = _make_show(tmp_path)
    b = show.read_bible()
    b["overall_plotline"] = {"id": "romance_u", "name": "The Romance",
                             "characters": ["Ryou"], "summary": "the driving force"}
    b["plotlines"] = [
        {"id": "romance-h", "name": "The Romance", "kind": "character",
         "characters": ["Ryou"], "summary": "same thread", "status": "active",
         "last_seen_episode": 0},
        {"id": "rival", "name": "Rivalry", "kind": "character",
         "characters": ["Ryou"], "summary": "daily rivalry", "status": "active",
         "last_seen_episode": 0},
    ]
    show.write_bible(b)

    import random
    pl = _plotline_state(show, b)
    for _ in range(50):
        roll = roll_episode_plotlines(show, 1, pl, cfg=Config(tmp_path),
                                      rng=random.Random(42), overall=b["overall_plotline"])
        assert "romance-h" not in roll["featured"], "overall-duplicate must not be rolled"


def test_arc_completion_triggers_cooldown(tmp_path):
    """When a running arc reaches 0 on approval, the plotline enters cooldown and
    is no longer selectable."""
    from studio.development import develop_episode, roll_episode_plotlines
    from studio.growth import commit_plotline_on_approval

    show = _make_show(tmp_path)
    b = show.read_bible()
    b["plotlines"] = [
        {"id": "debt", "name": "Debt", "kind": "character",
         "characters": ["Ryou"], "summary": "a debt", "status": "active",
         "last_seen_episode": 0},
        {"id": "rival", "name": "Rivalry", "kind": "character",
         "characters": ["Ryou"], "summary": "daily rivalry", "status": "active",
         "last_seen_episode": 0},
    ]
    show.write_bible(b)
    from studio.development import _plotline_state as _pl_state
    merged = _pl_state(show, show.read_bible())
    for p in merged:
        if p.get("id") == "rival":
            p["arc_remaining"] = 1   # one episode left
    cont = show.read_continuity()
    cont["plotlines"] = merged
    show.write_continuity(cont)

    import random
    from studio.development import _plotline_state
    # Force a roll that features "rival" (running arc overrides -> guaranteed).
    pl = _plotline_state(show, show.read_bible())
    roll = roll_episode_plotlines(show, 5, pl, cfg=Config(tmp_path), rng=random.Random(3))
    assert "rival" in roll["featured"]

    commit_plotline_on_approval(show, roll["featured"], None, 5, cfg=Config(tmp_path),
                                episode_type=roll["episode_type"],
                                arc_lengths=roll["arc_lengths"],
                                new_arc_length=roll["new_arc_length"])
    cont = show.read_continuity()
    rival = next(p for p in cont["plotlines"] if p.get("id") == "rival")
    assert rival["arc_remaining"] == 0
    assert rival["cooldown_remaining"] == 4   # entered cooldown

    # A NEW plotline from the roll is persisted with its arc on approval.
    cont["plotlines"][0].setdefault("cooldown_remaining", 0)
    show.write_continuity(cont)
