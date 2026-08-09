"""Tests for the H3 video render module (prompt compile + ref resolution)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.render import _subjects_for, compile_shot_prompt
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


def test_subjects_mapping():
    tokens, defs = _subjects_for(["Blade", "Lily"])
    assert tokens == ["<Subject 1>", "<Subject 2>"]
    assert "<Subject 1> is the character shown in <Picture 1>." in defs


def test_compile_shot_prompt_includes_action_subjects_dialogue():
    script = {"summary": "The crew defends the city."}
    scene = {"summary": "They fight the bio-mech.", "location": "slums"}
    shot = {"id": "s01_sh01", "action": "Blade draws his sword", "camera": "medium shot",
            "duration_s": 10.125,
            "dialogue": [{"char": "Blade", "line": "Hold it.", "on_camera": True}],
            "soundscape": "rain", "music": "drums"}
    prompt = compile_shot_prompt(script, scene, shot, ["Blade"])
    assert "subject_definitions:" in prompt
    assert "retention_analysis:" in prompt
    assert "Blade draws his sword" in prompt
    assert "Hold it." in prompt
    assert "overall_soundscape: rain" in prompt
    assert "non_diegetic_music: drums" in prompt
