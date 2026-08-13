"""Tests for storyboard object-ref injection and per-ref IPAdapter weights."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.config import Config
from studio.show import Show
from studio.storyboard import (_llm_recurring_objects, _shot_object_refs,
                               _shot_ref_weights)


def _show(tmp_path) -> Show:
    show = Show("demo", root=tmp_path)
    show.write_bible({"title": "Demo", "content_policy": "mature"})
    show.write_character({"id": "dahlia", "name": "Dahlia",
                          "appearance_canonical": "x"})
    od = tmp_path / "runs" / "EP01" / "objects"
    od.mkdir(parents=True, exist_ok=True)
    (od / "power-armor.png").write_bytes(b"x")
    (od / "hardsuit.png").write_bytes(b"x")
    return show


class _KeyItemsLLM:
    """The model returns only KEY items — the whole point of the LLM extraction."""

    def __init__(self, objects):
        self.objects = objects
        self.prompt = ""

    def chat_json(self, messages, **kwargs):
        self.prompt = messages[-1]["content"]
        return {"objects": self.objects}


def test_llm_recurring_objects_keeps_key_items_only(tmp_path):
    show = _show(tmp_path)
    script = {"summary": "The crew tries to steal the Hellbringer Cannon.",
              "scenes": [{"shots": [
                  {"action": "Blade drives the Hellbringer Cannon", "camera": "Shallow Depth Of Field"},
                  {"action": "Dahlia secures the Hellbringer Cannon", "camera": "Rack Focus"},
              ]}]}
    llm = _KeyItemsLLM(["The Hellbringer Cannon", "Blade's Motorcycle"])
    objs = _llm_recurring_objects(show, 1, script, cfg=Config(tmp_path), llm=llm)
    assert objs == ["The Hellbringer Cannon", "Blade's Motorcycle"]
    # The prompt gave the model the whole story + warned about camera language.
    assert "EPISODE STORY" in llm.prompt
    assert "Hellbringer Cannon" in llm.prompt


def test_llm_recurring_objects_never_falls_back_to_regex(tmp_path):
    show = _show(tmp_path)
    script = {"scenes": [{"shots": [
        {"action": "uses the Power Armor", "camera": ""},
        {"action": "dodges the Power Armor", "camera": ""},
    ]}]}

    class FailingLLM:
        def chat_json(self, messages, **kwargs):
            raise RuntimeError("LLM down")

    # On LLM failure we return [] — a bogus object is worse than none. There is
    # NO regex fallback that could resurrect "Power Armor".
    objs = _llm_recurring_objects(show, 1, script, cfg=Config(tmp_path), llm=FailingLLM())
    assert objs == []


def test_llm_recurring_objects_dedups_near_duplicates(tmp_path):
    show = _show(tmp_path)
    script = {"scenes": [{"shots": [{"action": "x", "camera": ""}]}]}
    llm = _KeyItemsLLM(["Hellbringer Cannon", "Hellbringer Heavy Cannon"])
    objs = _llm_recurring_objects(show, 1, script, cfg=Config(tmp_path), llm=llm)
    # Near-duplicate picks collapse to the shorter, cleaner name.
    assert objs == ["Hellbringer Cannon"]


def test_llm_recurring_objects_rejects_scenery_even_if_llm_says_so(tmp_path):
    show = _show(tmp_path)
    script = {"scenes": [{"shots": [{"action": "x", "camera": ""}]}]}
    # The 12B showrunner sometimes lists debris despite the prompt. The junk
    # filter must drop it deterministically — key items survive.
    llm = _KeyItemsLLM(["Pulverized Ferrocrete Dust Chunks", "Shattered Rebar Girders",
                        "The Hellbringer Cannon"])
    objs = _llm_recurring_objects(show, 1, script, cfg=Config(tmp_path), llm=llm)
    assert objs == ["The Hellbringer Cannon"]


def test_llm_recurring_objects_rejects_structural_scenery(tmp_path):
    show = _show(tmp_path)
    script = {"scenes": [{"shots": [{"action": "x", "camera": ""}]}]}
    llm = _KeyItemsLLM(["Collapsed Viaduct Edge Supports", "Blade's Motorcycle"])
    objs = _llm_recurring_objects(show, 1, script, cfg=Config(tmp_path), llm=llm)
    assert objs == ["Blade's Motorcycle"]


def test_recurring_objects_prompt_is_bounded():
    from studio.prompts import recurring_objects_prompt
    # A full 130-shot episode log must fit a local 12B model's context window —
    # the oversized prompt (63KB) used to make every extraction silently fail.
    script = {"summary": "s", "scenes": [
        {"id": "s%02d" % (i + 1), "location": "loc", "shots": [
            {"id": "s%02d_sh%02d" % (i + 1, j + 1),
             "action": "Blade drives the Hellbringer Cannon " * 3} for j in range(11)]
         } for i in range(12)]}
    p = recurring_objects_prompt(script, ["Blade"], ["loc"], "s")
    assert len(p) < 40000


def test_approved_names_requires_real_ref(tmp_path):
    from studio.casting import _approved_names
    show = Show("demo", root=tmp_path)
    show.write_character({"id": "apex", "name": "Apex", "appearance_canonical": "x"})  # sheet, no ref
    show.write_character({"id": "blade", "name": "Blade", "appearance_canonical": "x"})
    br = show.character_refs_dir("blade")
    br.mkdir(parents=True, exist_ok=True)
    (br / "blade_ref_01.png").write_bytes(b"x")
    (br / "refs.json").write_text(json.dumps(
        {"status": "real", "refs": ["blade_ref_01.png"], "variants": {"base": "blade_ref_01.png"}}),
        encoding="utf-8")
    # A sheet alone is NOT an approved ref.
    assert _approved_names(show) == {"Blade"}


def test_create_missing_character_refs_fills_sheet_without_ref(tmp_path, monkeypatch):
    """The threat of the week has a sheet but no ref image — the ref pass must
    render the ref anyway instead of skipping because the sheet exists."""
    import json as _json
    import studio.comfy_workflows as cw
    import studio.remote.ops as ops_mod
    from pathlib import Path as _Path
    from studio import casting

    show = Show("demo", root=tmp_path)
    show.write_bible({"title": "Demo", "content_policy": "mature"})
    # Existing sheet, NO ref image.
    show.write_character({"id": "apex-734", "name": "Apex-734",
                          "appearance_canonical": "colossal bio-mech"})
    runs = tmp_path / "runs" / "EP01"
    runs.mkdir(parents=True)
    (runs / "script.r1.json").write_text(_json.dumps(
        {"episode": 1, "cast": ["Blade", "Apex-734"], "scenes": []}), encoding="utf-8")
    # Blade is approved (has a real ref); Apex-734 must be detected as missing.
    show.write_character({"id": "blade", "name": "Blade", "appearance_canonical": "x"})
    br = show.character_refs_dir("blade")
    br.mkdir(parents=True, exist_ok=True)
    (br / "blade_ref_01.png").write_bytes(b"x")
    (br / "refs.json").write_text(_json.dumps(
        {"status": "real", "refs": ["blade_ref_01.png"], "variants": {"base": "blade_ref_01.png"}}),
        encoding="utf-8")

    monkeypatch.setattr(ops_mod.ServiceOps, "_krea2_client", lambda self: (None, None))
    monkeypatch.setattr(cw, "load_workflow", lambda p: {})

    def fake_keyframe(client, wf, prompt, seed, out_path, aspect_ratio=None):
        _Path(out_path).write_bytes(b"png")

    monkeypatch.setattr(cw, "generate_keyframe", fake_keyframe)

    created = casting.create_missing_character_refs(show, 1, cfg=Config(tmp_path))
    assert "Apex-734" in created
    apex = show.character_refs_dir("apex-734")
    assert (apex / "apex-734_ref_01.png").exists()
    assert (apex / "refs.json").exists()


def test_unit_name_depluralizes_group_characters():
    from studio.casting import _unit_name, _unit_key
    assert _unit_name("Chitinous Marauder Pods (x6)") == "Chitinous Marauder Pod"
    assert _unit_name("Chitinous Marauder Pods") == "Chitinous Marauder Pod"
    assert _unit_name("Swarm Drones (12)") == "Swarm Drone"
    assert _unit_name("Blade") == "Blade"
    assert _unit_name("Dahlia") == "Dahlia"
    # A designation number is NOT a group count — it must survive.
    assert _unit_name("Apex-734") == "Apex-734"
    assert _unit_name("Apex-734 (The Apex Scrapper's Rampage)") == "Apex-734 (The Apex Scrapper's Rampage)"
    # The script's plural name matches the unit's ref.
    assert _unit_key("Chitinous Marauder Pods (x6)") == _unit_key("Chitinous Marauder Pod")


def test_char_ref_map_resolves_group_script_names(tmp_path):
    from studio.storyboard import _char_ref_map, _shot_refs
    show = Show("demo", root=tmp_path)
    show.write_character({"id": "chitinous-marauder-pod", "name": "Chitinous Marauder Pod",
                          "appearance_canonical": "x"})
    rd = show.character_refs_dir("chitinous-marauder-pod")
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "pod_ref_01.png").write_bytes(b"x")
    (rd / "refs.json").write_text(json.dumps(
        {"status": "real", "refs": ["pod_ref_01.png"], "variants": {"base": "pod_ref_01.png"}}),
        encoding="utf-8")
    # Shot names the plural group; the ref still resolves to the single pod.
    names, refs = _shot_refs(show, {"references": {"characters": ["Chitinous Marauder Pods (x6)"]}})
    assert len(refs) == 1
    assert refs[0].endswith("pod_ref_01.png")


def test_regenerate_character_ref_from_feedback(tmp_path, monkeypatch):
    """Feedback on a base ref revises the appearance and re-renders the image —
    works for episode-supporting characters that aren't in the bootstrap state."""
    from pathlib import Path as _Path
    import studio.comfy_workflows as cw
    import studio.remote.ops as ops_mod
    from studio import casting

    show = Show("demo", root=tmp_path)
    show.write_bible({"title": "Demo", "content_policy": "mature"})
    show.write_character({"id": "apex-734", "name": "Apex-734",
                          "appearance_canonical": "colossal bio-mech"})
    rd = show.character_refs_dir("apex-734")
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "apex-734_ref_01.png").write_bytes(b"old")
    (rd / "refs.json").write_text(json.dumps(
        {"status": "real", "refs": ["apex-734_ref_01.png"], "variants": {"base": "apex-734_ref_01.png"}}),
        encoding="utf-8")

    monkeypatch.setattr(ops_mod.ServiceOps, "_krea2_client", lambda self: (None, None))
    monkeypatch.setattr(cw, "load_workflow", lambda p: {})

    def fake_keyframe(client, wf, prompt, seed, out_path, aspect_ratio=None):
        _Path(out_path).write_bytes(b"new")

    monkeypatch.setattr(cw, "generate_keyframe", fake_keyframe)

    class FakeLLM:
        def chat(self, messages, **kwargs):
            return '{"appearance_canonical": "revised look: sleeker chassis"}'

    ok = casting.regenerate_character_ref(show, "Apex-734", "less bulk",
                                          cfg=Config(tmp_path), llm=FakeLLM())
    assert ok
    assert "revised look" in show.read_character("apex-734")["appearance_canonical"]
    assert (rd / "apex-734_ref_01.png").read_bytes() == b"new"


def test_shot_object_refs_match_by_slug(tmp_path):
    show = _show(tmp_path)
    shot = {"action": "Blade powers up the Power Armor", "camera": "close-up"}
    refs = _shot_object_refs(show, 1, shot)
    assert any(p.endswith("power-armor.png") for p in refs)
    # a non-recurring / absent object yields no ref
    shot2 = {"action": "talks", "camera": ""}
    assert _shot_object_refs(show, 1, shot2) == []


def test_shot_ref_weights_speaker_dominates():
    shot = {"dialogue": [{"char": "Lily", "on_camera": True}]}
    w = _shot_ref_weights(shot, ["Blade", "Lily"], n_char_refs=2,
                          n_obj_refs=1, has_prev_kf=True)
    # speaker Lily strongest; Blade second; object light; prev keyframe weakest
    assert w[1] == 0.95
    assert w[0] == 0.9
    assert w[2] == 0.55
    assert w[3] == 0.45


def test_shot_ref_weights_no_speaker_first_char_dominates():
    w = _shot_ref_weights({}, ["Blade", "Lily"], n_char_refs=2,
                          n_obj_refs=0, has_prev_kf=False)
    assert w == [0.95, 0.8]   # first char = primary, second tapers
