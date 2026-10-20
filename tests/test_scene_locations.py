"""Canonical scene-location resolution (registry ids -> grounding refs)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import scenes
from studio.show import Show


def _show(tmp_path) -> Show:
    for sub in ("characters", "scenes"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=tmp_path)
    for sid, name in (("vixen-hangar-pit", "Vixen Hangar Pit"),
                      ("salvage-duel-arena", "Salvage Duel Arena"),
                      ("vixen-bunk-room", "Vixen Bunk Room")):
        show.write_scene({"id": sid, "name": name, "description": f"{name} description"})
        rd = scenes.refs_dir(show, sid)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / f"{sid}_ref_01.png").write_bytes(b"x")
    return show


def test_resolve_slug_exact_token_and_miss(tmp_path):
    show = _show(tmp_path)
    assert scenes.resolve_slug(show, "Salvage Duel Arena") == "salvage-duel-arena"
    assert scenes.resolve_slug(show, "vixen-hangar-pit") == "vixen-hangar-pit"
    # token overlap against the registered slug/name ("Vixen Pit" -> hangar pit)
    assert scenes.resolve_slug(show, "Vixen Pit") == "vixen-hangar-pit"
    # no confident match: precision over recall
    assert scenes.resolve_slug(show, "Broadcast Booth") is None
    assert scenes.resolve_slug(show, "") is None


def test_registry_block_lists_ids(tmp_path):
    show = _show(tmp_path)
    ids = {r["id"] for r in scenes.registry_block(show)}
    assert {"vixen-hangar-pit", "salvage-duel-arena"} <= ids


def test_canonicalize_accepts_valid_id_and_repairs_from_text(tmp_path):
    from studio.planner import _canonicalize_locations
    show = _show(tmp_path)
    plan = {"scenes": [
        {"id": "s01", "location": "somewhere", "location_id": "salvage-duel-arena"},
        {"id": "s02", "location": "Vixen Pit"},                 # repaired by text
        {"id": "s03", "location": "Broadcast Booth"},           # unknown -> empty
    ]}
    _canonicalize_locations(show, plan)
    assert plan["scenes"][0]["location_id"] == "salvage-duel-arena"
    assert plan["scenes"][1]["location_id"] == "vixen-hangar-pit"
    assert plan["scenes"][2]["location_id"] == ""


def test_resolve_location_ref_prefers_location_id(tmp_path):
    from studio.scene_anchor import resolve_location_ref
    show = _show(tmp_path)
    ref = resolve_location_ref(show, {"id": "s01", "location": "nonsense",
                                      "location_id": "salvage-duel-arena"})
    assert ref and ref.name == "salvage-duel-arena_ref_01.png"


def test_resolve_location_ref_uses_episode_plan(tmp_path):
    """A script scene without location_id resolves via the episode plan by id."""
    from studio.scene_anchor import resolve_location_ref
    show = _show(tmp_path)
    runs = show.dir / "runs" / "EP01"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "plan.json").write_text(json.dumps({
        "scenes": [{"id": "s01", "location": "Vixen Pit",
                    "location_id": "vixen-hangar-pit"}]}), encoding="utf-8")
    ref = resolve_location_ref(show, {"id": "s01", "location": "Vixen Pit"}, episode=1)
    assert ref and ref.name == "vixen-hangar-pit_ref_01.png"


def test_plan_prompt_carries_registry_and_location_id(tmp_path):
    from studio import prompts
    show = _show(tmp_path)
    reg = scenes.registry_block(show)
    p = prompts.plan_skeleton_prompt({"title": "X"}, {"episode": 1}, {}, [], 600,
                                     scene_registry=reg)
    assert "SCENE REGISTRY" in p
    assert "vixen-hangar-pit" in p
    assert "location_id" in p


class _Cfg:
    """Minimal config stub: no krea2 url, empty role maps."""

    def get(self, *keys, default=None):
        return {}


class _LocLLM:
    def chat_json(self, messages, **kw):
        return {"locations": [{
            "location": "Broadcast Booth",
            "description": "A cramped broadcast booth overlooking the arena.",
            "setting_prompt": "empty broadcast booth interior, monitors, background only",
        }]}


def test_register_location_new_idempotent_and_reuse(tmp_path):
    show = _show(tmp_path)
    sid = scenes.register_location(show, "Broadcast Booth", description="d", setting_prompt="s")
    assert sid == "broadcast-booth"
    assert (show.scenes_dir / "broadcast-booth.yaml").exists()
    # idempotent: same name resolves to the same entry, no duplicate
    assert scenes.register_location(show, "Broadcast Booth") == "broadcast-booth"
    # an existing registry location is reused, not duplicated
    assert scenes.register_location(show, "Vixen Pit") == "vixen-hangar-pit"


def test_scene_ref_citation_is_weak_not_framing():
    """The location ref is a LOOK reference: it must not force its own camera
    framing (a wide empty location plate was otherwise pasted behind every shot)."""
    from studio.render import compile_shot_prompt
    p = compile_shot_prompt(None, {}, {}, {"id": "s1", "action": "she runs"}, [],
                            scene_ref_pic=1)
    assert "weak_reference" in p
    assert "do not reproduce the reference image's wide composition" in p
    assert "fully_preserved" not in p.split("retention_analysis:")[-1]


def test_resolve_location_ref_prefers_shot_environment(tmp_path):
    """A shot in its own set (a cockpit) grounds on THAT ref, not the scene place."""
    from studio.scene_anchor import resolve_location_ref
    show = _show(tmp_path)
    cid = scenes.register_location(show, "Mira's dead cockpit", description="cockpit")
    rd = scenes.refs_dir(show, cid)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"{cid}_ref_01.png").write_bytes(b"x")
    scene = {"id": "s03", "location_id": "salvage-duel-arena", "location": "the apron"}
    shot = {"id": "s03_sh01", "references": {"scene": "Mira's dead cockpit"}}
    ref = resolve_location_ref(show, scene, shot=shot)
    assert ref and ref.name == f"{cid}_ref_01.png"
    # a shot sharing the scene's place falls back to the scene ref
    ref2 = resolve_location_ref(show, scene, shot={"id": "s03_sh02", "references": {}})
    assert ref2 and ref2.name == "salvage-duel-arena_ref_01.png"


def test_canonicalize_shot_environments_registers_each_set(tmp_path):
    from studio.planner import _canonicalize_shot_environments
    show = _show(tmp_path)
    scene = {"id": "s03", "location": "Mira's dead cockpit", "summary": "x",
             "shots": [
                 {"id": "s03_sh01", "references": {"scene": "Mira's dead cockpit"}},
                 {"id": "s03_sh02", "references": {"scene": "drop-zone apron"}},
                 {"id": "s03_sh03", "references": {"scene": "drop-zone apron"}},
             ]}
    _canonicalize_shot_environments(show, 1, scene, llm=_LocLLM(), cfg=_Cfg())
    cid = scenes.slugify("Mira's dead cockpit")
    ids = [sh["references"]["location_id"] for sh in scene["shots"]]
    assert ids[0] == cid
    assert ids[1] == ids[2] == "drop-zone-apron"   # same set reused
    assert (show.scenes_dir / f"{cid}.yaml").exists()
    assert (show.scenes_dir / "drop-zone-apron.yaml").exists()


def test_register_plan_locations_uses_scene_setting_prompt(tmp_path):
    from studio.planner import _register_plan_locations
    show = _show(tmp_path)
    plan = {"scenes": [
        {"id": "s01", "location": "New Rooftop",
         "location_setting": "empty rooftop at dusk, background only"},
    ]}
    _register_plan_locations(show, plan, llm=_LocLLM(), cfg=_Cfg())
    assert plan["scenes"][0]["location_id"] == "new-rooftop"
    import yaml
    d = yaml.safe_load((show.scenes_dir / "new-rooftop.yaml").read_text(encoding="utf-8"))
    assert d["setting_prompt"] == "empty rooftop at dusk, background only"


def test_dedupe_merges_venue_synonyms_not_cockpits(tmp_path):
    show = _show(tmp_path)
    for sid, name in (("stadium-arena-floor", "Stadium Arena Floor"),
                      ("concrete-stadium-bowl", "Concrete Stadium Bowl"),
                      ("shared-bunk-with-blank-schedule-board", "Shared Bunk (blank board)"),
                      ("mira-s-dead-cockpit", "Mira's Dead Cockpit"),
                      ("sable-s-dead-cockpit", "Sable's Dead Cockpit")):
        show.write_scene({"id": sid, "name": name, "description": name})
        rd = scenes.refs_dir(show, sid)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / f"{sid}_ref_01.png").write_bytes(b"x")
    mapping = scenes.plan_dedupe(show)
    assert mapping.get("stadium-arena-floor") == "salvage-duel-arena"
    assert mapping.get("concrete-stadium-bowl") == "salvage-duel-arena"
    assert mapping.get("shared-bunk-with-blank-schedule-board") == "vixen-bunk-room"
    # distinct cockpits never merge
    assert "mira-s-dead-cockpit" not in mapping
    assert "sable-s-dead-cockpit" not in mapping


def test_dedupe_rewrites_refs_and_removes_duplicates(tmp_path):
    show = _show(tmp_path)
    show.write_scene({"id": "stadium-arena-floor", "name": "Stadium Arena Floor"})
    rd = scenes.refs_dir(show, "stadium-arena-floor")
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "stadium-arena-floor_ref_01.png").write_bytes(b"x")
    runs = show.dir / "runs" / "EP01"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "plan.json").write_text(json.dumps({"scenes": [
        {"id": "s01", "location_id": "stadium-arena-floor"}]}), encoding="utf-8")
    log = scenes.dedupe(show)
    assert any("stadium-arena-floor" in line for line in log)
    assert not (show.scenes_dir / "stadium-arena-floor.yaml").exists()
    plan = json.loads((runs / "plan.json").read_text(encoding="utf-8"))
    assert plan["scenes"][0]["location_id"] == "salvage-duel-arena"


def test_register_plan_locations_registers_new(tmp_path):
    from studio.planner import _register_plan_locations
    show = _show(tmp_path)
    plan = {"scenes": [
        {"id": "s01", "location": "Vixen Pit"},         # existing -> reuse
        {"id": "s02", "location": "Broadcast Booth"},   # new -> register
    ]}
    new = _register_plan_locations(show, plan, llm=_LocLLM(), cfg=_Cfg())
    assert plan["scenes"][0]["location_id"] == "vixen-hangar-pit"
    assert plan["scenes"][1]["location_id"] == "broadcast-booth"
    assert "broadcast-booth" in new
    assert (show.scenes_dir / "broadcast-booth.yaml").exists()

