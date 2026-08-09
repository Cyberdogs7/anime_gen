"""Tests for storyboard object-ref injection and per-ref IPAdapter weights."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.show import Show
from studio.storyboard import (_recurring_objects, _shot_object_refs,
                               _shot_ref_weights)


def _show(tmp_path) -> Show:
    show = Show("demo", root=tmp_path)
    od = tmp_path / "runs" / "EP01" / "objects"
    od.mkdir(parents=True, exist_ok=True)
    (od / "power-armor.png").write_bytes(b"x")
    (od / "hardsuit.png").write_bytes(b"x")
    return show


def test_recurring_objects_needs_two_plus_shots():
    script = {"scenes": [{"shots": [
        {"action": "uses the Power Armor", "camera": ""},
        {"action": "dodges the Power Armor", "camera": ""},
        {"action": "walks away", "camera": ""},
    ]}]}
    assert "Power Armor" in _recurring_objects(script)
    assert "Walk" not in _recurring_objects(script)  # single mention, lowercase anyway


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
