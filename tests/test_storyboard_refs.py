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


def _write_plan(show: Show, episode: int, objects: list[str]) -> None:
    """Write a minimal plan.json with the given objects list."""
    plan_dir = show.dir / "runs" / f"EP{episode:02d}"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = {"episode": episode, "objects": objects, "scenes": []}
    (plan_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")


def test_llm_recurring_objects_reads_from_plan(tmp_path):
    show = _show(tmp_path)
    _write_plan(show, 1, ["The Hellbringer Cannon", "Blade's Motorcycle"])
    objs = _llm_recurring_objects(show, 1, {})
    assert objs == ["The Hellbringer Cannon", "Blade's Motorcycle"]


def test_llm_recurring_objects_empty_when_no_plan(tmp_path):
    show = _show(tmp_path)
    objs = _llm_recurring_objects(show, 1, {})
    assert objs == []


def test_llm_recurring_objects_deduplicates(tmp_path):
    show = _show(tmp_path)
    _write_plan(show, 1, ["Cannon", "Cannon", "Motorcycle"])
    objs = _llm_recurring_objects(show, 1, {})
    assert objs == ["Cannon", "Motorcycle"]


def test_llm_recurring_objects_caps_at_six(tmp_path):
    show = _show(tmp_path)
    _write_plan(show, 1, [f"Object {i}" for i in range(10)])
    objs = _llm_recurring_objects(show, 1, {})
    assert len(objs) == 6


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

    def fake_keyframe(client, wf, prompt, seed, out_path, aspect_ratio=None, **kwargs):
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

    def fake_keyframe(client, wf, prompt, seed, out_path, aspect_ratio=None, **kwargs):
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
    _write_plan(show, 1, ["Power Armor"])
    shot = {"action": "Blade powers up the Power Armor", "camera": "close-up"}
    refs = _shot_object_refs(show, 1, shot)
    assert any(p.endswith("power-armor.png") for p in refs)
    # object not in plan yields no ref even if file exists
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


def test_delete_costume_removes_variant(tmp_path):
    """Deleting a costume drops its ref image + registry entries; base is protected."""
    import json as _json
    from studio.storyboard import delete_costume

    show = _show(tmp_path)
    rd = show.character_refs_dir("dahlia")
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "red.png").write_bytes(b"x")
    (rd / "blue.png").write_bytes(b"x")
    (rd / "refs.json").write_text(_json.dumps({
        "variants": {"base": "base.png", "Red Suit": "red.png", "Blue Suit": "blue.png"},
        "prompts": {"Red Suit": "a red suit", "Blue Suit": "a blue suit"},
        "approved": ["Red Suit"],
    }), encoding="utf-8")

    delete_costume(show, "dahlia", "Red Suit")

    data = _json.loads((rd / "refs.json").read_text(encoding="utf-8"))
    assert "Red Suit" not in data["variants"]
    assert "Red Suit" not in data["prompts"]
    assert "Red Suit" not in data["approved"]
    assert not (rd / "red.png").exists()
    assert (rd / "blue.png").exists()          # sibling untouched

    # base outfit is protected
    with pytest.raises(ValueError):
        delete_costume(show, "dahlia", "base")

    # deleting a missing variant is a no-op
    delete_costume(show, "dahlia", "nope")
    assert "Blue Suit" in data["variants"]
