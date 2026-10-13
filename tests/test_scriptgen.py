"""Tests for shared script utilities: runtime review and normalization."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_runtime_review_flags_short_scripts():
    from studio.scriptgen import _runtime_review
    short = {"scenes": [{"shots": [{"duration_s": 10.125}] * 3}]}      # ~30s vs 1320 target
    assert _runtime_review(short, 1320) is not None
    full = {"scenes": [{"shots": [{"duration_s": 10.125,
              "dialogue": [{"char": "Ryou", "line": "x", "on_camera": True}
                           for _ in range(3)]}] * 130}]}    # ~1316s, 390 lines
    assert _runtime_review(full, 1320) is None


def test_runtime_review_ignores_padded_dialogue():
    from studio.scriptgen import _runtime_review
    # Enough duration and plenty of dialogue ENTRIES, but every line is a stage
    # direction with no spoken words — must still fail the line floor.
    padded = {"scenes": [{"shots": [{"duration_s": 10.125,
               "dialogue": [{"char": "Ryou", "line": "(Grunting)", "on_camera": True},
                            {"char": "Ryou", "line": "(Internal/Unvoiced)", "on_camera": False},
                            {"char": "Ryou", "line": "(Sharp exhale)", "on_camera": True}]}
                              ] * 130}]}
    assert _runtime_review(padded, 1320) is not None
    # A delivery note prefix is fine as long as real words follow.
    spoken = {"scenes": [{"shots": [{"duration_s": 10.125,
              "dialogue": [{"char": "Ryou", "line": "(whispering) Stay close.", "on_camera": True},
                           {"char": "Ryou", "line": "Keep up.", "on_camera": True},
                           {"char": "Ryou", "line": "Move.", "on_camera": True}]}
                                ] * 130}]}
    assert _runtime_review(spoken, 1320) is None


def test_normalize_strips_padded_dialogue_lines():
    from studio.scriptgen import _normalize
    script = {"episode": 1, "cast": ["Ryou"],
              "scenes": [{"id": "s01", "shots": [{
                  "id": "s01_sh01", "duration_s": 10,
                  "dialogue": [{"char": "Ryou", "line": "(Grunting)"},
                               {"char": "Ryou", "line": "(whispering) Stay close."}],
                  "references": {"characters": ["Ryou"]}}]}]}
    _normalize(script, ["Ryou"])
    lines = script["scenes"][0]["shots"][0]["dialogue"]
    assert lines == [{"char": "Ryou", "line": "(whispering) Stay close."}]
