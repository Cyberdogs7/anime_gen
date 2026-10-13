"""Deterministic repetition gate: N near-identical scenes must be caught before
they reach dialogue (the root cause of quip-only, filler dialogue)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.review import repetition_review


def _scene(i, loc, text):
    return {"id": f"s{i:02d}", "location": loc, "summary": text,
            "narrative": text, "beats": [text], "shots": []}


def test_flags_same_location_cluster():
    scenes = [_scene(i, "Mira's Locked Cockpit",
                     f"Ren jams the live cable into the locked cockpit and Mira wakes, take {i}")
              for i in range(1, 9)]
    v = repetition_review(scenes)
    assert v["pass"] is False
    assert any("share the location" in n["note"] for n in v["notes"])
    assert all(n["severity"] == "HIGH" for n in v["notes"])


def test_passes_varied_scenes():
    scenes = [
        _scene(1, "Hangar Pit", "Ren haggles with the sponsor over his contract price"),
        _scene(2, "Duel Arena", "Mira duels Harrow and nearly blows her last nerve"),
        _scene(3, "Bathhouse", "Sable and Juno fight over the towel schedule"),
        _scene(4, "Bunk Room", "Tessa runs a diagnostic and finds the implant scar"),
    ]
    assert repetition_review(scenes)["pass"] is True


def test_flags_near_identical_content_across_locations():
    body = ("Ren sprints to the dead frame and jams the live calibration cable into "
            "the locked cockpit while Mira screams and the sync climbs past ninety")
    scenes = [_scene(1, "A", body), _scene(2, "B", body), _scene(3, "C", body)]
    v = repetition_review(scenes, similarity=0.6, min_cluster=3)
    assert v["pass"] is False


def test_ignores_shared_cast_names_only():
    # Scenes share the cast but are about different things -> must NOT trip.
    scenes = [
        _scene(1, "Hangar", "Ren haggles with the sponsor over his contract price"),
        _scene(2, "Bathhouse", "Mira soaks and plots the ladder climb with Sable"),
        _scene(3, "Arena", "Juno spots a rival honey trap on the scout radio"),
    ]
    assert repetition_review(scenes, similarity=0.6, min_cluster=3)["pass"] is True
