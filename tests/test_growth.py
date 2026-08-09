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
                "featured_plotlines": [{"id": "op", "role": "advanced"}],
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
                                      episodes_since_new=3)
    assert "INTRODUCE a new plotline THIS episode" in text
    quiet = prompts.development_prompt(1, None, [], [], continuity, [], [],
                                       cadence=3, episodes_since_new=1)
    assert "INTRODUCE a new plotline THIS episode" not in quiet
