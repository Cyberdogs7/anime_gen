"""Reject notes must rewrite the character sheet, not just the render prompt."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import refs as refs_mod
from studio.config import Config
from studio.show import Show


class FakeLLM:
    def __init__(self, script):
        self.script = script

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "BODY-ONLY identity" in user:
            return {"identity_canonical": self.script.get(
                "identity", "masculine man with broad shoulders")}
        if "DEFAULT OUTFIT" in user:
            return {"wardrobe_canonical": self.script.get(
                "wardrobe", "dark tailored suit")}
        raise AssertionError(f"unexpected prompt: {user[:80]}")


def _show(tmp_path):
    for sub in ("characters", "voices", "scenes"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    cfg = Config(tmp_path)
    show = Show("demo", root=tmp_path)
    show.write_character({
        "id": "kai", "name": "Kai",
        "appearance_canonical": "soft feminine woman in a floral dress",
        "identity_canonical": "soft feminine woman",
        "wardrobe_canonical": "floral dress",
        "voice": {"mode": "designed", "voice_description": "soft voice"},
    })
    return show, cfg


def test_revise_identity_applies_gender_notes(tmp_path):
    show, cfg = _show(tmp_path)
    llm = FakeLLM({})
    out = refs_mod._revise_identity(
        llm, "m", "Kai", "soft feminine woman", "make it male")
    # The revision itself carries the fix; callers persist it to the sheet.
    assert out.startswith("masculine")


def _nude_approved(show, cid="kai"):
    rd = show.character_refs_dir(cid)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"{cid}_nude.png").write_bytes(b"fake")
    (rd / "refs.json").write_text(json.dumps({
        "variants": {"nude": f"{cid}_nude.png"}, "approved": ["nude"]}),
        encoding="utf-8")


def test_regenerate_base_rewrites_sheet_before_render(tmp_path, monkeypatch):
    show, cfg = _show(tmp_path)
    _nude_approved(show)
    renders = []

    def fake_render(*args, **kwargs):
        renders.append(args)
        Path(args[5]).write_bytes(b"fake")
        return True

    monkeypatch.setattr(refs_mod, "_render_costume_via_qwen_edit", fake_render)
    llm = FakeLLM({"wardrobe": "dark tailored suit",
                   "identity": "soft feminine woman"})
    char = show.read_character("kai")
    ok = refs_mod.regenerate_base(show, "kai", char, cfg=cfg, llm=llm,
                                  notes="give him a dark suit")
    assert ok is True
    reread = show.read_character("kai")
    assert "suit" in reread["wardrobe_canonical"]
    assert "masculine" not in reread["identity_canonical"]
    assert renders, "expected a re-render call"


def test_regenerate_base_rerolls_nude_on_body_change(tmp_path, monkeypatch):
    show, cfg = _show(tmp_path)
    _nude_approved(show)

    def fake_nude(show, cid, name, cfg=None, **kwargs):
        rd = show.character_refs_dir(cid)
        (rd / f"{cid}_nude.png").write_bytes(b"new-nude")
        data = json.loads((rd / "refs.json").read_text(encoding="utf-8"))
        data["approved"] = [a for a in data.get("approved", []) if a != "nude"]
        (rd / "refs.json").write_text(json.dumps(data), encoding="utf-8")
        return str(rd / f"{cid}_nude.png")

    def fake_render(*args, **kwargs):
        Path(args[5]).write_bytes(b"fake")
        return True

    monkeypatch.setattr(refs_mod, "ensure_nude_ref", fake_nude)
    monkeypatch.setattr(refs_mod, "_render_costume_via_qwen_edit", fake_render)
    llm = FakeLLM({"wardrobe": "dark tailored suit"})
    char = show.read_character("kai")
    ok = refs_mod.regenerate_base(show, "kai", char, cfg=cfg, llm=llm,
                                  notes="make him male")
    assert ok is True
    reread = show.read_character("kai")
    # Body feedback rewrites the identity and re-rolls the nude root.
    assert "masculine" in reread["identity_canonical"]
    assert (rd_new := (show.character_refs_dir("kai") / "kai_nude.png").read_bytes()) == b"new-nude"


def test_refresh_voice_rewrites_identity_and_text(tmp_path, monkeypatch):
    from studio.bootstrap import BootstrapChain
    from studio.config import Config
    from studio.show import Show
    for sub in ("characters", "voices", "scenes"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    cfg = Config(tmp_path)
    show = Show("demo", root=tmp_path)
    show.write_character({"id": "kai", "name": "Kai",
                          "voice": {"mode": "designed",
                                    "voice_description": "soft feminine voice"},
                          "voice_sample_text": "old words " * 30})
    show.set_bootstrap_state({"characters": [{"name": "Kai", "voice": ""}]})

    class FakeLLM:
        def chat_json(self, messages, **kwargs):
            user = messages[-1]["content"]
            if "Revise ONLY the voice description" in user:
                return {"voice_description": "deep masculine baritone"}
            if "first-person monologue" in user:
                return {"voice_sample_text": "new bold words here " * 30}
            raise AssertionError(user[:80])

    chain = BootstrapChain(show, cfg=cfg, llm=FakeLLM())
    text = chain.refresh_voice("Kai", "make it male")
    assert "bold" in text
    reread = show.read_character("kai")
    assert reread["voice"]["voice_description"] == "deep masculine baritone"
    assert "bold" in reread["voice_sample_text"]
    assert show.bootstrap_state()["characters"][0]["voice"] == ""


def test_regenerate_base_falls_back_without_nude(tmp_path):
    # No approved nude and no llm-driven path can derive a base: callers
    # must see False (so they can re-roll the nude instead of failing blind).
    show, cfg = _show(tmp_path)
    char = show.read_character("kai")
    llm = FakeLLM({})
    assert refs_mod.regenerate_base(show, "kai", char, cfg=cfg, llm=llm,
                                    notes="make it male") is False
