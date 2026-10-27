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


def _image_card(tmp_path):
    store = BoardStore(tmp_path / "b.db")
    card = new_card("g0.demo.images", kind="gate0_images",
                    title="Gate 0 images", role="gate0_images",
                    reviewer="human", lane="image", show_id="demo")
    store.create(card)
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


def test_reject_one_image_creates_one_retake_and_blocks_parent(tmp_path, monkeypatch):
    root, image = _show_fixture(tmp_path, monkeypatch)
    store = _image_card(tmp_path)
    path = "shows/demo/scenes/clinic/refs/clinic_ref_01.png"

    parent, retake = _reject_image(store, "g0.demo.images", path, "too dark")
    assert retake["kind"] == "image_retake"
    assert retake["payload"]["parent"] == "g0.demo.images"
    assert retake["payload"]["notes"] == "too dark"
    assert parent["artifacts"][0]["status"] == "rejected"
    assert parent["artifacts"][0]["retake_id"] == retake["id"]
    refs = json.loads((root / "scenes" / "clinic" / "refs" / "refs.json")
                      .read_text(encoding="utf-8"))
    assert refs["status"] == "rejected"
    assert [c["id"] for c in assets.open_retakes(store, "g0.demo.images")] == [retake["id"]]

    again_parent, again = _reject_image(store, "g0.demo.images", path, "still bad")
    assert again["id"] == retake["id"]
    assert again_parent["artifacts"][0]["status"] == "rejected"

    store.advance(retake["id"], "in_work", "daemon")
    store.advance(retake["id"], "agent_verified", "daemon")
    store.advance(retake["id"], "reviewing", "daemon")
    accepted = _decide_retake(store, store.get(retake["id"]), "approve", "")
    assert accepted["state"] == "accepted"
    refs = json.loads((root / "scenes" / "clinic" / "refs" / "refs.json")
                      .read_text(encoding="utf-8"))
    assert refs["status"] == "approved" and refs["approved"] is True
    assert assets.open_retakes(store, "g0.demo.images") == []
    updated = store.get("g0.demo.images")
    assert updated["artifacts"][0]["status"] == "accepted"


def test_reject_retake_requeues_then_blocks_at_cap(tmp_path, monkeypatch):
    _show_fixture(tmp_path, monkeypatch)
    store = _image_card(tmp_path)
    path = "shows/demo/scenes/clinic/refs/clinic_ref_01.png"
    _, retake = _reject_image(store, "g0.demo.images", path, "too dark")
    store.advance(retake["id"], "in_work", "daemon")
    store.advance(retake["id"], "agent_verified", "daemon")
    store.advance(retake["id"], "reviewing", "daemon")
    out = _decide_retake(store, store.get(retake["id"]), "reject", "still dark")
    assert out["state"] == "retake"
    out = _decide_retake(store, store.get(retake["id"]), "reject", "still dark")
    assert out["state"] == "blocked"


def test_image_retake_tool_archives_and_marks_pending(tmp_path, monkeypatch):
    root, image = _show_fixture(tmp_path, monkeypatch)
    store = _image_card(tmp_path)
    path = "shows/demo/scenes/clinic/refs/clinic_ref_01.png"
    parent, retake = _reject_image(store, "g0.demo.images", path, "too dark")

    import studio.scenes as scenes_mod
    from studio.board import tools as tools_mod
    from studio.board.cards import new_card

    def fake_ref(show, scene, sid, cfg=None, report=None, force=False, notes=""):
        image.write_bytes(b"new")
        return True

    monkeypatch.setattr(scenes_mod, "generate_ref", fake_ref)
    monkeypatch.setattr(tools_mod, "Show",
                        lambda sid: Show(sid, root=tmp_path / "shows" / sid))
    store.advance(retake["id"], "in_work", "daemon")
    role = type("Role", (), {"name": "image_retake", "tools": ("image_retake",)})()
    cfg = type("Cfg", (), {"root": tmp_path})()
    card = store.get(retake["id"])
    ctx = tools_mod.ToolContext(store=store, card=card, role=role,
                                phase="produce", run_id="run-retake", cfg=cfg)
    result = tools_mod.call(ctx, "image_retake", {
        "asset": card["payload"]["asset"], "notes": "too dark",
        "parent": "g0.demo.images"})
    assert result["path"] == path
    assert image.read_bytes() == b"new"
    refs = json.loads((root / "scenes" / "clinic" / "refs" / "refs.json")
                      .read_text(encoding="utf-8"))
    assert refs["status"] == "pending"
    parent = store.get("g0.demo.images")
    assert parent["artifacts"][0]["status"] == "pending"
    current = store.get(retake["id"])
    assert len(current["artifacts"]) == 2
    assert current["artifacts"][0]["caption"] == "rejected before retake"


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


def test_reject_one_voice_creates_voice_retake(tmp_path, monkeypatch):
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
                 "Rook voice sample")
    parent, retake = _reject_voice(
        store, "g0.demo.voices", "shows/demo/assets/voice/rook_voice.wav",
        "too flat")
    assert retake["kind"] == "voice_text"
    assert retake["payload"]["parent"] == "g0.demo.voices"
    assert retake["payload"]["notes"] == "too flat"
    assert parent["artifacts"][0]["status"] == "rejected"
    assert assets.open_children(store, "g0.demo.voices")[0]["id"] == retake["id"]
    state = show.bootstrap_state()
    assert state["characters"][0]["voice"] == "rejected"
    again_parent, again = _reject_voice(
        store, "g0.demo.voices", "shows/demo/assets/voice/rook_voice.wav", "x")
    assert again["id"] == retake["id"]
