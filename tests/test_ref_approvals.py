"""Tests for costume-variant + recurring-object ref approval gating.

The storyboard preview phase (and therefore the final render) must wait for the
human to approve the costume variants and object refs the episode uses. These
tests exercise the pending-ref gate and the approve/unapprove state helpers.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.show import Show
from studio.storyboard import (_approved_object_slugs, mark_costume_approved,
                               mark_object_approved, pending_ref_approvals,
                               regenerate_object_ref, unapprove_object)


def _show(tmp_path) -> Show:
    show = Show("demo", root=tmp_path)
    show.write_character({"id": "blade", "name": "Blade", "appearance_canonical": "x"})
    show.write_character({"id": "lily", "name": "Lily", "appearance_canonical": "y"})
    rd = show.character_refs_dir("blade")
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "refs.json").write_text(json.dumps({
        "status": "real",
        "refs": ["blade_ref_01.png"],
        "variants": {
            "base": "blade_ref_01.png",
            "mech frame": "blade_mech.png",
            "Mercenary Armor": "blade_merc.png",
        },
    }), encoding="utf-8")
    (rd / "blade_ref_01.png").write_bytes(b"x")
    (rd / "blade_mech.png").write_bytes(b"x")
    (rd / "blade_merc.png").write_bytes(b"x")
    # Lily only has her base ref (nothing to approve).
    rd2 = show.character_refs_dir("lily")
    rd2.mkdir(parents=True, exist_ok=True)
    (rd2 / "refs.json").write_text(json.dumps({
        "status": "real", "refs": ["lily_ref_01.png"],
        "variants": {"base": "lily_ref_01.png"},
    }), encoding="utf-8")
    (rd2 / "lily_ref_01.png").write_bytes(b"x")
    return show


def _script():
    return {"scenes": [{"shots": [
        {"action": "Blade powers up the Power Armor",
         "references": {"costumes": {"Blade": "mech frame"}}},
        {"action": "Blade dodges the Power Armor",
         "references": {"costumes": {"Blade": "mech frame"}}},
    ]}]}


def test_pending_includes_costume_and_object(tmp_path):
    show = _show(tmp_path)
    od = show.dir / "runs" / "EP01" / "objects"
    od.mkdir(parents=True, exist_ok=True)
    (od / "power-armor.png").write_bytes(b"x")
    script = _script()

    pending = pending_ref_approvals(show, 1, script)
    # mech frame costume is generated but not approved; object is generated but not approved.
    assert any("costume" in p and "Blade" in p for p in pending)
    assert any("object" in p and "Power Armor" in p for p in pending)


def test_approve_costume_clears_costume_gate(tmp_path):
    show = _show(tmp_path)
    script = _script()
    mark_costume_approved(show, "blade", "mech frame")
    pending = pending_ref_approvals(show, 1, script)
    assert not any("costume" in p and "Blade" in p for p in pending)


def test_approve_object_clears_object_gate(tmp_path):
    show = _show(tmp_path)
    od = show.dir / "runs" / "EP01" / "objects"
    od.mkdir(parents=True, exist_ok=True)
    (od / "power-armor.png").write_bytes(b"x")
    script = _script()
    mark_object_approved(show, 1, "power-armor")
    pending = pending_ref_approvals(show, 1, script)
    assert not any("object" in p and "Power Armor" in p for p in pending)


def test_unapprove_object_reopens_gate(tmp_path):
    show = _show(tmp_path)
    od = show.dir / "runs" / "EP01" / "objects"
    od.mkdir(parents=True, exist_ok=True)
    (od / "power-armor.png").write_bytes(b"x")
    script = _script()
    mark_object_approved(show, 1, "power-armor")
    unapprove_object(show, 1, "power-armor")
    pending = pending_ref_approvals(show, 1, script)
    assert any("object" in p and "Power Armor" in p for p in pending)


def test_no_refs_means_no_costume_gate(tmp_path):
    # Lily wears no costume variants and there are no object refs -> no pending
    # costume/object gate (character base refs are approved via bootstrap).
    show = _show(tmp_path)
    st = show.bootstrap_state()
    st["characters"] = [
        {"name": "Blade", "proposal": "approved", "refs": "approved", "voice": "approved"},
        {"name": "Lily", "proposal": "approved", "refs": "approved", "voice": "approved"},
    ]
    show.set_bootstrap_state(st)
    script = {"scenes": [{"shots": [
        {"action": "Lily talks", "references": {"costumes": {"Lily": "base"}}},
    ]}]}
    pending = pending_ref_approvals(show, 1, script)
    assert not any("costume" in p for p in pending)
    assert not any("object" in p for p in pending)


def test_approved_object_slugs_roundtrip(tmp_path):
    show = _show(tmp_path)
    mark_object_approved(show, 1, "power-armor")
    assert _approved_object_slugs(show, 1) == {"power-armor"}


def test_regen_object_persists_and_revises_prompt(tmp_path, monkeypatch):
    """Object regeneration must work like the costume/char-ref loop: the current
    (LLM-revised) prompt is stored and reused so successive rejects FURTHER
    refine it instead of resetting to the base template, and the feedback is
    folded in as a coherent instruction (never raw-appended)."""
    import studio.remote.ops as ops_mod
    import studio.storyboard as sb

    show = _show(tmp_path)
    od = show.dir / "runs" / "EP01" / "objects"
    od.mkdir(parents=True, exist_ok=True)

    # Stub the hardware + model so we drive the real regenerate_object_ref.
    monkeypatch.setattr(ops_mod.ServiceOps, "generate_image",
                        lambda self, prompt, **kw: {"ok": True, "path": ""})
    monkeypatch.setattr(ops_mod.ServiceOps, "_krea2_client", lambda self: (None, None))
    import studio.comfy_workflows as cw
    from pathlib import Path as _Path
    monkeypatch.setattr(cw, "generate_keyframe",
                        lambda client, wf, prompt, seed, out_path, **kw: _Path(out_path).write_bytes(b"png"))

    seen: list[str] = []
    import studio.storyboard as sb2
    sb2.ServiceOps = None  # ensure the local import path in regenerate_object_ref resolves
    def fake_gen_image(self, prompt, seed=0, aspect_ratio="1:1", out_path=""):
        seen.append(prompt)
        _Path(out_path).write_bytes(b"png")
        return {"ok": True, "path": out_path}
    monkeypatch.setattr(ops_mod.ServiceOps, "generate_image", fake_gen_image)

    class FakeLLM:
        def chat(self, messages, **kwargs):
            content = messages[-1]["content"]
            fb = content.split("DIRECTOR'S FEEDBACK:")[-1].strip()
            return '{"prompt": "the antique circuit board alone flat on a plain background, ' + \
                   fb + ', anime style, no hands no feet no people"}'

    # First regen with feedback -> LLM-revised prompt persisted.
    assert sb.regenerate_object_ref(show, 1, "antique-circuit-board",
                                    "anime style, no hands no feet", llm=FakeLLM())
    stored1 = (od / "prompts.json").read_text(encoding="utf-8")
    assert "no hands no feet" in stored1
    assert "Adjust" not in stored1           # never raw-appended notes
    assert "the antique circuit board alone" in stored1

    # Second regen starts from the stored prompt and refines it further.
    sb.regenerate_object_ref(show, 1, "antique-circuit-board", "smaller", llm=FakeLLM())
    stored2 = sb._object_prompts(show, 1).get("antique-circuit-board", "")
    assert "smaller" in stored2              # cumulative feedback
    assert "no hands no feet" in stored2     # prior feedback retained

    out = od / "antique-circuit-board.png"
    assert out.exists()
