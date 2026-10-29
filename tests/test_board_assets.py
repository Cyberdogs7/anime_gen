"""Per-image Gate 0 reject: asset state, retake card, parent gating."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import studio.show as show_mod
from studio.board import assets
from studio.board.api import _decide_retake, _reject_image, _reject_voice
from studio.board.cards import new_card
from studio.board.store import BoardStore
from studio.show import Show


def _show_fixture(tmp_path, monkeypatch):
    root = tmp_path / "shows" / "demo"
    refs_dir = root / "scenes" / "clinic" / "refs"
    refs_dir.mkdir(parents=True)
    image = refs_dir / "clinic_ref_01.png"
    image.write_bytes(b"png")
    (refs_dir / "refs.json").write_text(
        json.dumps({"status": "real", "refs": [image.name]}), encoding="utf-8")
    monkeypatch.setattr(show_mod, "Show",
                        lambda sid: Show(sid, root=tmp_path / "shows" / sid))
    return root, image


def _image_card(tmp_path, state="reviewing"):
    store = BoardStore(tmp_path / "b.db")
    card = new_card("g0.demo.images", kind="gate0_images",
                    title="Gate 0 images", role="gate0_images", reviewer="human",
                    lane="image", show_id="demo")
    store.create(card)
    for _to in (("in_work", "agent_verified", "reviewing")
                if state == "reviewing" else ()):
        store.advance("g0.demo.images", _to, "test")
    assert store.get("g0.demo.images")["state"] == state
    store.attach("g0.demo.images",
                 "shows/demo/scenes/clinic/refs/clinic_ref_01.png", "clinic ref")
    return store


def test_parse_scene_and_character_assets(tmp_path, monkeypatch):
    root, image = _show_fixture(tmp_path, monkeypatch)
    show = Show("demo", root=root)
    scene = assets.parse_asset(show, "shows/demo/scenes/clinic/refs/clinic_ref_01.png")
    assert scene["type"] == "scene" and scene["id"] == "clinic"
    assert assets.is_retakeable(scene)
    assert assets.parse_asset(show, "shows/demo/scenes/clinic/refs/refs.json") is None


def test_reject_image_reworks_same_card(tmp_path, monkeypatch):
    root, image = _show_fixture(tmp_path, monkeypatch)
    store = _image_card(tmp_path)
    path = "shows/demo/scenes/clinic/refs/clinic_ref_01.png"

    parent, retake = _reject_image(store, "g0.demo.images", path, "too dark")
    assert retake is None
    assert parent["id"] == "g0.demo.images"
    assert parent["state"] == "retake"
    assert parent["payload"]["notes"] == "too dark"
    assert parent["attempts"] == 0
    assert parent["artifacts"][0]["status"] == "rejected"
    assert [c["id"] for c in store.all(show_id="demo")
            if c["kind"] == "image_retake"] == []
    refs = json.loads((root / "scenes" / "clinic" / "refs" / "refs.json")
                      .read_text(encoding="utf-8"))
    assert refs["status"] == "rejected"

    # A repeat reject while still unresolved reuses the same card again.
    again_parent, again = _reject_image(store, "g0.demo.images", path, "still bad")
    assert again is None
    assert again_parent["id"] == "g0.demo.images"
    assert again_parent["state"] == "retake"
    assert [c["id"] for c in store.all(show_id="demo")
            if c["kind"] == "image_retake"] == []


def test_image_retake_tool_still_serves_legacy_cards(tmp_path, monkeypatch):
    """The image_retake tool itself is unchanged for in-flight legacy cards."""
    root, image = _show_fixture(tmp_path, monkeypatch)
    store = _image_card(tmp_path)
    path = "shows/demo/scenes/clinic/refs/clinic_ref_01.png"

    import studio.scenes as scenes_mod
    from studio.board import tools as tools_mod
    from studio.board.cards import new_card
    from studio.config import Config

    def fake_ref(show, scene, sid, cfg=None, report=None, force=False, notes=""):
        image.write_bytes(b"new")
        return True

    monkeypatch.setattr(scenes_mod, "generate_ref", fake_ref)
    monkeypatch.setattr(tools_mod, "Show",
                        lambda sid: Show(sid, root=tmp_path / "shows" / sid))
    asset = {"type": "scene", "id": "clinic", "path": path, "retakeable": True}
    retake = new_card("g0.demo.images.retake.test", kind="image_retake",
                      title="t", show_id="demo", role="image_retake",
                      reviewer="human", lane="image", priority=5,
                      payload={"asset": asset, "notes": "too dark",
                             "parent": "g0.demo.images"})
    store.upsert(retake)
    store.advance(retake["id"], "in_work", "daemon")
    role = type("Role", (), {"name": "image_retake", "tools": ("image_retake",)})()
    cfg = Config(tmp_path)
    card = store.get(retake["id"])
    ctx = tools_mod.ToolContext(store=store, card=card, role=role,
                                phase="produce", run_id="run-retake", cfg=cfg)
    result = tools_mod.call(ctx, "image_retake", {
        "asset": asset, "notes": "too dark",
        "parent": "g0.demo.images"})
    assert result["path"] == path
    assert image.read_bytes() == b"new"


def test_gate0_images_rerun_regenerates_rejected(tmp_path, monkeypatch):
    """Same-card reject: the images tool re-renders rejected assets and
    clears their rejected marker so review controls come back."""
    import types
    root, image = _show_fixture(tmp_path, monkeypatch)
    (root / "scenes" / "clinic.yaml").write_text("name: Clinic\n", encoding="utf-8")
    show = Show("demo", root=root)
    show.set_bootstrap_state({"characters": [],
                              "scenes": {"status": "approved"}})
    store = _image_card(tmp_path)
    path = "shows/demo/scenes/clinic/refs/clinic_ref_01.png"
    card, retake = _reject_image(store, "g0.demo.images", path, "too dark")
    assert retake is None
    assert card["state"] == "retake"
    store.advance(card["id"], "planned", "daemon")
    store.advance(card["id"], "in_work", "daemon")

    import studio.scenes as scenes_mod
    from studio.board import tools as tools_mod
    seen = {}

    def fake_ref(show, scene, sid, cfg=None, report=None, force=False, notes=""):
        seen["notes"] = notes
        seen["force"] = force
        image.write_bytes(b"new")
        (root / "scenes" / "clinic" / "refs" / "refs.json").write_text(
            json.dumps({"status": "real", "refs": [image.name]}), encoding="utf-8")
        return True

    monkeypatch.setattr(scenes_mod, "generate_ref", fake_ref)
    monkeypatch.setattr("studio.gpu_manager.get_gpu_manager",
                        lambda cfg: types.SimpleNamespace(unload_comfy=lambda: None))

    class FakeLLM:
        def chat_json(self, messages, **kwargs):
            assert "Current location brief (JSON)" in messages[-1]["content"]
            return {"id": "clinic", "name": "Clinic",
                    "description": "A realistic workshop",
                    "setting_prompt": "Realistic workshop interior",
                    "location": "workshop", "time_of_day": "day"}

    monkeypatch.setattr(tools_mod, "_role_llm", lambda ctx: FakeLLM())
    monkeypatch.setattr(tools_mod, "Show",
                        lambda sid: Show(sid, root=tmp_path / "shows" / sid))
    import os
    import time
    old = time.time() - 100
    os.utime(str(image), (old, old))
    from studio.config import Config
    cfg = Config(tmp_path)
    role = type("Role", (), {"name": "gate0_images", "tools": ("gate0_images",),
                             "model": ""})()
    card = store.get("g0.demo.images")
    ctx = tools_mod.ToolContext(store=store, card=card, role=role,
                                phase="produce", run_id="run-rerun", cfg=cfg)
    result = tools_mod.call(ctx, "gate0_images", {})
    assert image.read_bytes() == b"new"
    assert seen.get("notes") == "too dark"
    assert any("clinic" in str(line) for line in result.get("log", []))
    updated = store.get("g0.demo.images")
    matches = [a for a in updated["artifacts"] if a["path"] == path]
    assert matches and matches[0].get("status") in ("", None), matches


def test_scene_retake_blocks_gate0_images(tmp_path, monkeypatch):
    root, image = _show_fixture(tmp_path, monkeypatch)
    show = Show("demo", root=root)
    monkeypatch.setattr(show, "bootstrap_state",
                        lambda: {"characters": [], "scenes": {"status": "approved"}})
    refs_path = root / "scenes" / "clinic" / "refs" / "refs.json"
    refs_path.write_text(json.dumps({"status": "pending"}), encoding="utf-8")
    (root / "scenes" / "clinic.yaml").write_text("name: Clinic\n", encoding="utf-8")
    from studio.board.gate0 import status
    st = status(show, "images")
    assert st["pending"] == {"step": "scene", "scene": "clinic"}


def test_advance_voices_handles_multiple_characters():
    from studio.bootstrap import BootstrapChain

    class FakeShow:
        show_id = "demo"

        def __init__(self):
            self.state = {"characters": [
                {"name": "Rook", "voice": ""},
                {"name": "Vex", "voice": ""},
            ]}

        def bootstrap_state(self):
            return self.state

        def set_bootstrap_state(self, state):
            self.state = state

        def list_characters(self):
            return ["rook", "vex"]

        def read_character(self, cid):
            return {"id": cid, "name": cid.title()}

    chain = object.__new__(BootstrapChain)
    chain.show = FakeShow()
    chain._emit = lambda *a, **k: None
    chain.bootstrap_voice = lambda char: None
    assert len(chain._advance_voices("")) == 2


def test_parse_voice_assets(tmp_path, monkeypatch):
    root, _ = _show_fixture(tmp_path, monkeypatch)
    (root / "characters").mkdir(exist_ok=True)
    (root / "characters" / "rook.yaml").write_text("name: Rook\n", encoding="utf-8")
    wav = root / "assets" / "voice" / "rook_voice.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"wav")
    yaml = root / "voices" / "rook_voice.yaml"
    yaml.parent.mkdir(parents=True)
    yaml.write_text("id: rook_voice\n", encoding="utf-8")
    show = Show("demo", root=root)
    voice = assets.parse_asset(show, "shows/demo/assets/voice/rook_voice.wav")
    cfg = assets.parse_asset(show, "shows/demo/voices/rook_voice.yaml")
    assert voice["type"] == "voice" and voice["id"] == "rook"
    assert voice["label"] == "Rook" and assets.is_retakeable(voice)
    assert not assets.is_retakeable(cfg)
    assert assets.parse_asset(show, "shows/demo/videos/x.mp4") is None


def test_reject_one_voice_reworks_same_card(tmp_path, monkeypatch):
    root, _ = _show_fixture(tmp_path, monkeypatch)
    (root / "characters").mkdir(exist_ok=True)
    (root / "characters" / "rook.yaml").write_text("name: Rook\n", encoding="utf-8")
    wav = root / "assets" / "voice" / "rook_voice.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"old")
    show = Show("demo", root=root)
    show.set_bootstrap_state({"characters": [
        {"name": "Rook", "voice": "pending"},
    ]})
    store = BoardStore(tmp_path / "b.db")
    store.create(new_card("g0.demo.voices", kind="gate0_voices",
                          title="voices", role="gate0_voices", reviewer="human",
                          lane="audio", show_id="demo"))
    store.attach("g0.demo.voices", "shows/demo/assets/voice/rook_voice.wav",
                 "Rook voice sample",
                 asset={"type": "voice", "id": "rook", "retakeable": True})
    store.advance("g0.demo.voices", "in_work", "test")
    store.advance("g0.demo.voices", "agent_verified", "test")
    store.advance("g0.demo.voices", "reviewing", "test")
    parent, retake = _reject_voice(
        store, "g0.demo.voices", "shows/demo/assets/voice/rook_voice.wav",
        "make it male")
    # Same card goes back for rework: no voice_text card, no blocked original.
    assert retake is None
    assert parent["state"] == "retake"
    assert parent["payload"]["notes"] == "make it male"
    assert parent["payload"]["char"] == "Rook"
    assert parent["attempts"] == 0
    assert parent["artifacts"][0]["status"] == "rejected"
    assert [c["kind"] for c in store.all(show_id="demo")] == ["gate0_voices"]
    state = show.bootstrap_state()
    assert state["characters"][0]["voice"] == "rejected"


def _regenerate_show(tmp_path, monkeypatch):
    import studio.show as show_mod
    from studio.board import api as api_mod
    from studio.config import Config
    root = tmp_path / "shows" / "demo"
    (root / "characters").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(show_mod, "Show",
                        lambda sid: Show(sid, root=tmp_path / "shows" / sid))
    cfg = Config(tmp_path)
    show = Show("demo", root=root)
    show.set_bootstrap_state({"characters": [{"name": "Rook", "voice": "pending"}]})
    return show, api_mod


def test_regenerate_voice_reuses_voices_card(tmp_path, monkeypatch):
    from studio.board import api as api_mod
    from studio.board.store import BoardStore
    show, _ = _regenerate_show(tmp_path, monkeypatch)
    store = BoardStore(tmp_path / "b.db")
    store.create(new_card("g0.demo.voices.r1", kind="gate0_voices",
                          title="t", role="gate0_voices", reviewer="human",
                          lane="audio", show_id="demo", payload={"char": "Rook"}))
    store.advance("g0.demo.voices.r1", "in_work", "test")
    store.advance("g0.demo.voices.r1", "agent_verified", "test")
    store.advance("g0.demo.voices.r1", "reviewing", "test")
    card = api_mod._show_regenerate(store, "demo", "voice_text", "Rook")
    assert card["id"] == "g0.demo.voices.r1"
    assert card["kind"] == "gate0_voices"
    assert card["state"] == "retake"
    assert store.get(card["id"])["attempts"] == 0
    assert [c for c in store.all() if c["kind"] == "voice_text"] == []


def test_regenerate_voice_mints_voices_card_when_missing(tmp_path, monkeypatch):
    from studio.board import api as api_mod
    from studio.board.store import BoardStore
    show, _ = _regenerate_show(tmp_path, monkeypatch)
    store = BoardStore(tmp_path / "b.db")
    card = api_mod._show_regenerate(store, "demo", "voice_text", "Rook")
    assert card["kind"] == "gate0_voices"
    assert card["id"].startswith("g0.demo.voices.")
    assert [c for c in store.all() if c["kind"] == "voice_text"] == []
