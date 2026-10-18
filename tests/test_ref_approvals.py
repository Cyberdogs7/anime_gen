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

import studio.refs as refs
from studio.show import Show
from studio.storyboard import (_approved_costume_labels, _approved_object_slugs,
                               is_base_ref_approved, mark_base_ref_approved,
                               mark_costume_approved, mark_object_approved,
                               pending_ref_approvals, regenerate_object_ref,
                               unapprove_base_ref, unapprove_object)


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


def test_base_ref_approved_from_bootstrap(tmp_path):
    """A character approved at the Gate-0 character/refs gate must read as an
    approved BASE ref everywhere — including the episode cast card, which reads
    the refs.json approval list."""
    show = _show(tmp_path)
    show_bootstrap = {"characters": [
        {"name": "Blade", "proposal": "approved", "refs": "approved",
         "voice": "approved"}]}
    show.set_bootstrap_state(show_bootstrap)
    assert is_base_ref_approved(show, "blade") is True
    # Lily was never approved.
    assert is_base_ref_approved(show, "lily") is False


def test_base_ref_approval_roundtrips_in_refs_json(tmp_path):
    """Episode-supporting characters (no bootstrap entry) approve their base ref
    in refs.json; a regen/reject drops it again."""
    show = _show(tmp_path)
    assert is_base_ref_approved(show, "lily") is False
    assert mark_base_ref_approved(show, "lily") is True
    assert is_base_ref_approved(show, "lily") is True
    # Approving a costume label keeps the base approval intact.
    mark_costume_approved(show, "lily", "snow gear")
    assert "base" in _approved_costume_labels(show, "lily")
    assert is_base_ref_approved(show, "lily") is True
    unapprove_base_ref(show, "lily")
    assert is_base_ref_approved(show, "lily") is False


def test_approve_step_refs_records_base_for_supporting_char(tmp_path, monkeypatch):
    """Approving a base ref for a character with NO bootstrap roster entry must
    succeed and persist, instead of raising "no character ... in bootstrap state"."""
    import studio.approval as approval
    import studio.config as _cfg

    class FakeChain:
        def __init__(self, *a, **k):
            pass

        def _emit(self, event):
            pass

        def advance(self, *a, **k):
            return []

    monkeypatch.setattr(approval, "BootstrapChain", FakeChain)
    # approve_step builds Show(show_id) from the process config, so root the
    # config at tmp_path to keep the write local to the test.
    monkeypatch.setattr(_cfg, "_default", _cfg.Config(tmp_path))
    show = Show("demo")
    show.write_character({"id": "lily", "name": "Lily", "appearance_canonical": "y"})
    rd = show.character_refs_dir("lily")
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "refs.json").write_text(json.dumps({
        "status": "real", "refs": ["lily_ref_01.png"],
        "variants": {"base": "lily_ref_01.png"}}), encoding="utf-8")
    (rd / "lily_ref_01.png").write_bytes(b"x")
    assert show.bootstrap_state().get("characters", []) == []
    approval.approve_step("demo", "refs", char="Lily")
    assert is_base_ref_approved(show, "lily") is True


def test_episode_cast_marks_approved_base(tmp_path, monkeypatch):
    """The episode cast payload (the 'Episode cast (character refs)' card) shows
    a bootstrap-approved base ref as approved, not permanently unapproved."""
    import studio.config as _cfg
    from studio.dashboard import episode_payload
    monkeypatch.setattr(_cfg, "_default", _cfg.Config(tmp_path))
    show = Show("demo")
    show.write_character({"id": "blade", "name": "Blade", "appearance_canonical": "x"})
    rd = show.character_refs_dir("blade")
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "refs.json").write_text(json.dumps({
        "status": "real", "refs": ["blade_ref_01.png"],
        "variants": {"base": "blade_ref_01.png"}}), encoding="utf-8")
    (rd / "blade_ref_01.png").write_bytes(b"x")
    show.set_bootstrap_state({"characters": [
        {"name": "Blade", "proposal": "approved", "refs": "approved",
         "voice": "approved"}]})
    d = show.dir / "runs" / "EP01"
    d.mkdir(parents=True, exist_ok=True)
    (d / "script.r1.json").write_text(json.dumps({"cast": ["Blade"], "scenes": []}),
                                      encoding="utf-8")
    cast = episode_payload("demo", "EP01")["cast"]
    base = next(v for c in cast if c["name"] == "Blade"
                for v in c["variants"] if v["label"] == "base")
    assert base["approved"] is True


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
    def fake_gen_image(self, prompt, seed=0, aspect_ratio="1:1", out_path="", **kwargs):
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


def test_shot_character_ref_anchors_on_approved_costume(tmp_path):
    """A shot's declared outfit is the identity anchor WHEN approved; an
    unapproved or unknown label falls back to the base ref (never leaking an
    unapproved outfit into a render)."""
    from studio.render import _shot_character_ref
    show = _show(tmp_path)
    base = _shot_character_ref(show, "Blade")
    assert base and "blade_ref_01.png" in base
    # Declared-but-unapproved costume -> base fallback.
    assert "blade_ref_01.png" in (_shot_character_ref(show, "Blade", "mech frame") or "")
    # Unknown label -> base fallback.
    assert "blade_ref_01.png" in (_shot_character_ref(show, "Blade", "no such outfit") or "")
    # Approved costume -> that variant's image is the anchor.
    mark_costume_approved(show, "blade", "mech frame")
    picked = _shot_character_ref(show, "Blade", "mech frame")
    assert picked and "blade_mech.png" in picked
    # Case-insensitive match against the approved set.
    mark_costume_approved(show, "blade", "Mercenary Armor")
    picked2 = _shot_character_ref(show, "Blade", "mercenary armor")
    assert picked2 and "blade_merc.png" in picked2
    # "base"/empty always resolves to the base ref.
    assert "blade_ref_01.png" in (_shot_character_ref(show, "Blade", "base") or "")


def test_nude_ref_is_a_selectable_variant_never_base(tmp_path, monkeypatch):
    """The nude identity root is a first-class appearance variant (selectable,
    needs approval) but never the base ref — so shots that declare nothing still
    anchor the clothed canonical."""
    import studio.storyboard as sb
    show = _show(tmp_path)

    def fake_krea2(show_, name, prompt, label, out_path, cfg, seed=0):
        Path(out_path).write_bytes(b"png")
        return True
    monkeypatch.setattr(refs, "_render_costume_via_krea2", fake_krea2)

    p = sb.ensure_nude_ref(show, "blade", "Blade")
    assert p and p.endswith("blade_nude.png") and Path(p).exists()
    data = json.loads((show.character_refs_dir("blade") / "refs.json").read_text())
    assert data["variants"]["nude"] == "blade_nude.png"      # selectable variant
    assert data["variants"]["base"] == "blade_ref_01.png"    # base slot unchanged
    assert "nude" not in (data.get("approved") or [])        # must be approved
    # Cached: a second call reuses the file without re-rendering.
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return fake_krea2(*a, **k)
    monkeypatch.setattr(refs, "_render_costume_via_krea2", counting)
    assert sb.ensure_nude_ref(show, "blade", "Blade")
    assert calls["n"] == 0
    # force (regenerate) re-renders.
    assert sb.ensure_nude_ref(show, "blade", "Blade", force=True)
    assert calls["n"] == 1


def test_shot_character_ref_anchors_on_nude_when_approved(tmp_path, monkeypatch):
    """A nude shot anchors the 'nude' ref once approved; unapproved falls back to
    the clothed base (never leaking an unapproved nude into a render)."""
    from studio.render import _shot_character_ref
    import studio.storyboard as sb
    show = _show(tmp_path)

    def fake_krea2(show_, name, prompt, label, out_path, cfg, seed=0):
        Path(out_path).write_bytes(b"png")
        return True
    monkeypatch.setattr(refs, "_render_costume_via_krea2", fake_krea2)

    sb.ensure_nude_ref(show, "blade", "Blade")
    assert "blade_ref_01.png" in (_shot_character_ref(show, "Blade", "nude") or "")
    mark_costume_approved(show, "blade", "nude")
    picked = _shot_character_ref(show, "Blade", "nude")
    assert picked and "blade_nude.png" in picked


def test_nude_ref_states_gender(tmp_path, monkeypatch):
    """The nude-root prompt must carry the subject's gender (from the body-only
    identity description) — otherwise male characters render female anatomy."""
    import studio.storyboard as sb
    seen: dict[str, str] = {}

    def fake_krea2(show_, name, prompt, label, out_path, cfg, seed=0):
        seen["prompt"] = prompt
        Path(out_path).write_bytes(b"png")
        return True
    monkeypatch.setattr(refs, "_render_costume_via_krea2", fake_krea2)

    def _char(cid, **kw):
        show = Show("demo", root=tmp_path / cid)
        show.write_character({"id": cid, "name": kw.pop("name", cid), **kw})
        rd = show.character_refs_dir(cid)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "refs.json").write_text(json.dumps({
            "status": "real", "refs": [f"{cid}_ref_01.png"],
            "variants": {"base": f"{cid}_ref_01.png"}}), encoding="utf-8")
        (rd / f"{cid}_ref_01.png").write_bytes(b"x")
        return show

    show = _char("ren", name="Ren",
                 identity_canonical="21-year-old lean man, dark hair, scar")
    assert sb.ensure_nude_ref(show, "ren", "Ren")
    assert "man" in seen["prompt"]

    # No identity_canonical: falls back to the body clause of appearance_canonical.
    show = _char("sam", name="Sam",
                 appearance_canonical="tall, buzzed hair, a man wearing a gray suit")
    assert sb.ensure_nude_ref(show, "sam", "Sam")
    assert "man" in seen["prompt"]

