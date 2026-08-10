"""Tests for the H3 video render module (prompt compile + ref resolution)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.render import compile_shot_prompt
from studio.show import Show


def _show(tmp_path) -> Show:
    for sub in ("characters", "voices", "scenes"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=tmp_path)
    show.write_character({"id": "blade", "name": "Blade", "appearance_canonical": "x"})
    show.write_character({"id": "lily", "name": "Lily", "appearance_canonical": "y"})
    rd = show.character_refs_dir("blade")
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "refs.json").write_text(
        '{"status": "real", "refs": ["blade_ref_01.png"]}', encoding="utf-8")
    (rd / "blade_ref_01.png").write_bytes(b"x")
    return show


def test_picture_tags_align_with_ref_order():
    """The guide-exact prompt defines <Subject N> from <Picture N> in ref order."""
    from studio.render import compile_shot_prompt
    shot = {"id": "s01", "action": "they clash",
            "dialogue": [{"char": "Blade", "line": "Hold it."}]}
    prompt = compile_shot_prompt(None, {}, {}, shot, ["Blade", "Lily"])
    assert "<Subject 1> is Blade, shown in <Picture 1>" in prompt
    assert "<Subject 2> is Lily, shown in <Picture 2>" in prompt
    assert "detailed_description:" in prompt
    assert "<d>[English] Hold it.</d>" in prompt


def test_compile_shot_prompt_includes_action_subjects_dialogue():
    script = {"summary": "The crew defends the city."}
    scene = {"summary": "They fight the bio-mech.", "location": "slums"}
    shot = {"id": "s01_sh01", "action": "Blade draws his sword", "camera": "medium shot",
            "duration_s": 10.125,
            "dialogue": [{"char": "Blade", "line": "Hold it.", "on_camera": True}],
            "soundscape": "rain", "music": "drums"}
    prompt = compile_shot_prompt(None, script, scene, shot, ["Blade"])
    # guide-exact six-section structure
    for sec in ("subject_definitions:", "summary:", "retention_analysis:",
                "detailed_description:", "overall_soundscape:", "non_diegetic_music:"):
        assert sec in prompt, sec
    assert "<Subject 1> is Blade, shown in <Picture 1>" in prompt
    assert "Blade draws his sword" in prompt
    assert "<Subject 1> (S1) says" in prompt
    assert "<d>[English] Hold it.</d>" in prompt


def test_dialogue_is_d_token_speech():
    """Dialogue is written as <d>[English] …</d> tokens bound to <Subject N> (Sx),
    per the guide's speaker rules."""
    from studio.render import compile_shot_prompt
    script = {"summary": "They fight."}
    scene = {"summary": "Blade vs Lily."}
    shot = {"id": "s01_sh01", "action": "Blade raises his hand",
            "dialogue": [{"char": "Blade", "line": "Hold it.", "on_camera": True},
                         {"char": "Lily", "line": "Never.", "on_camera": True}]}
    prompt = compile_shot_prompt(None, script, scene, shot, ["Blade", "Lily"])
    assert "<Subject 1> (S1) says, <d>[English] Hold it.</d>" in prompt
    assert "<Subject 2> (S2) says, <d>[English] Never.</d>" in prompt
    assert '"Hold it."' not in prompt  # dialogue must be <d>, not quotes


def test_shot_places_character_appearance_in_frame():
    """Appearance is woven into the <Subject N> definition and detailed_description."""
    import tempfile
    from pathlib import Path
    from studio.render import compile_shot_prompt
    show = _show(Path(tempfile.mkdtemp()))
    c = show.read_character("blade")
    c["appearance_canonical"] = "a tall man with slicked-back dark hair and amber eyes"
    show.write_character(c)
    shot = {"id": "s01_sh01", "action": "he draws his sword",
            "dialogue": [{"char": "Blade", "line": "Hold it."}]}
    prompt = compile_shot_prompt(show, {}, {}, shot, ["Blade"])
    assert "amber eyes" in prompt
    assert "<Subject 1> is Blade, shown in <Picture 1>, a tall man" in prompt
    assert "<d>[English] Hold it.</d>" in prompt


def test_audio_ref_binding_in_shot_line():
    from studio.render import compile_shot_prompt
    script = {"summary": "They fight."}
    scene = {"summary": "Blade vs Lily."}
    shot = {"id": "s01_sh01", "action": "Blade speaks",
            "dialogue": [{"char": "Blade", "line": "Hold it."}]}
    prompt = compile_shot_prompt(
        None, script, scene, shot, ["Blade"],
        audio_refs=[{"char": "Blade", "token": "<Audio 1>"}])
    # guide-exact audio binding + retention + in-shot reference
    assert "<Audio 1> is the voice-timbre reference for <Subject 1> (S1)." in prompt
    assert "<Audio 1>: reference - its vocal timbre guides the dialogue" in prompt
    assert "using the voice timbre referenced from <Audio 1>, <d>[English] Hold it.</d>" in prompt


def test_h3_ref2va_workflow_socket_refs():
    """build_h3_ref2va_workflow must use the core MiniMaxH3ReferenceToVideo node
    and wire refs as SOCKETS (LoadImage->ref_image_N, LoadAudio->ref_audio_N),
    referencing <Picture N> / <Audio N> in connection order."""
    from studio.h3 import build_h3_ref2va_workflow
    wf = build_h3_ref2va_workflow(
        "<Picture 1> is Blade. <Audio 1> is Blade's voice.",
        10.125, 1,
        ref_image_filenames=["blade_ref_01.png", "keyframe.png"],
        ref_audio_filenames=["blade_voice.wav"],
    )
    cond = wf["5"]
    assert cond["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert cond["inputs"]["ref_images.ref_image_0"] == ["img0", 0]
    assert cond["inputs"]["ref_images.ref_image_1"] == ["img1", 0]
    assert cond["inputs"]["ref_audios.ref_audio_0"] == ["aud0", 0]
    assert wf["img0"]["class_type"] == "LoadImage"
    assert wf["aud0"]["class_type"] == "LoadAudio"
    assert cond["inputs"]["width"] == 1344
    assert cond["inputs"]["height"] == 768
    # no refs -> no socket keys, conditioning node still present
    wf2 = build_h3_ref2va_workflow("t2v prompt", 10.125, 1)
    cond2 = wf2["5"]["inputs"]
    assert "ref_images.ref_image_0" not in cond2
    assert "ref_audios.ref_audio_0" not in cond2


def test_shot_ref_paths_and_voice_resolution(tmp_path):
    from studio.render import _shot_ref_paths, _shot_character_ref, _voice_sample_path
    show = _show(tmp_path)
    (show.dir / "assets").mkdir(parents=True, exist_ok=True)
    (show.dir / "assets" / "voice").mkdir(parents=True, exist_ok=True)
    vp = show.dir / "assets" / "voice" / "blade_voice.wav"
    vp.write_bytes(b"x")
    assert _voice_sample_path(show, "Blade") == vp
    assert _voice_sample_path(show, "Nope") is None
    # character ref resolves to the approved portrait (for the timeline slot)…
    crefs = _shot_character_ref(show, "Blade")
    assert crefs and "blade_ref_01.png" in crefs
    # …but _shot_ref_paths carries ONLY non-character anchors (keyframe).
    shot = {"id": "s01_sh01", "action": "Blade fights",
            "references": {"characters": ["Blade"]}}
    kf = show.dir / "runs" / "EP01" / "storyboard"
    kf.mkdir(parents=True, exist_ok=True)
    (kf / "s01_sh01.png").write_bytes(b"x")
    paths = _shot_ref_paths(show, 1, {}, shot)
    assert any("s01_sh01.png" in p for p in paths)
    assert not any("blade_ref_01.png" in p for p in paths)
