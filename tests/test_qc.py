"""Tests for the STT dialogue QC (garbled-audio detection)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.qc import (normalize, _tokens, _best_window_overlap,
                       verify_dialogue, run_shot_qc)
from studio.show import Show


def test_normalize_strips_parentheticals_and_punctuation():
    assert normalize("(Whispering) Stay close, kid!") == "stay close kid"
    assert normalize("What's up?") == "what's up"
    assert normalize("(Grunting)") == ""
    assert normalize("  ") == ""


def test_best_window_overlap_perfect():
    assert _best_window_overlap("hold the line", "hold the line now") == 1.0


def test_best_window_overlap_reordered_words_scores_lower():
    # Same words but scrambled: overlap degrades (ordered-window match).
    score = _best_window_overlap("hold the line", "line the hold")
    assert 0.0 < score < 1.0


def test_best_window_overlap_no_match():
    assert _best_window_overlap("the package is in the vault", "pizza tastes good") == 0.0


def test_verify_dialogue_pass():
    shot = {"dialogue": [
        {"char": "Blade", "line": "The package. Where is it?"},
        {"char": "Lily", "line": "Never."},
    ]}
    rep = verify_dialogue(shot, "The package. Where is it? Never.")
    assert rep["verdict"] == "pass"
    assert rep["score"] >= 0.5
    assert rep["issues"] == []


def test_verify_dialogue_silent_clip_when_speech_expected():
    shot = {"dialogue": [{"char": "Blade", "line": "Where is the package?"}]}
    rep = verify_dialogue(shot, "")
    assert rep["verdict"] == "fail"
    assert any("silent" in i for i in rep["issues"])


def test_verify_dialogue_garbled_transcript():
    shot = {"dialogue": [{"char": "Blade", "line": "The package is in the vault."}]}
    rep = verify_dialogue(shot, "bzzz the kthunk glarb the snorple womple")
    assert rep["verdict"] == "fail"
    assert rep["score"] < 0.5


def test_verify_dialogue_invented_preamble_fails():
    """A clip that says the right line but pads it with invented garbled speech
    must FAIL — the prompt guarantees no ambience chatter, so unexplained words
    are an H3 artifact, not background noise."""
    shot = {"dialogue": [{"char": "Blade", "line": "Clumsy? Hardly."}]}
    rep = verify_dialogue(
        shot, "The second edge woods are stridings of special Nregra staff. "
              "Clumsy? Hardly. You just needed a more efficient anchor point.")
    assert rep["verdict"] == "fail"
    assert any("unexplained" in i for i in rep["issues"])
    assert rep["explained"] < 0.45
    # The calibration floor (config qc.stt.min_explained = 0.70) must also fail it.
    rep2 = verify_dialogue(shot,
                           "The second edge woods are stridings of special Nregra staff. "
                           "Clumsy? Hardly. You just needed a more efficient anchor point.",
                           min_explained=0.70)
    assert rep2["verdict"] == "fail"


def test_verify_dialogue_clean_transcript_fully_explained():
    shot = {"dialogue": [{"char": "Lily", "line": "Target acquired."},
                         {"char": "Blade", "line": "The spike is anomalous."}]}
    rep = verify_dialogue(shot, "Target acquired. The spike is anomalous.")
    assert rep["verdict"] == "pass"
    assert rep["explained"] >= 0.9


def test_verify_dialogue_silent_shot_passes_as_skip():
    shot = {"dialogue": [{"char": "Blade", "line": "(Grunting)"}]}
    rep = verify_dialogue(shot, "")
    assert rep["verdict"] == "skip"


def test_verify_dialogue_silent_shot_with_invented_speech_fails():
    shot = {"dialogue": []}
    rep = verify_dialogue(shot, "someone is talking gibberish now")
    assert rep["verdict"] == "fail"
    assert any("unexpected speech" in i for i in rep["issues"])


def test_run_shot_qc_persists_summary(tmp_path):
    show = Show("demo", root=tmp_path)
    shot = {"id": "s01_sh01",
            "dialogue": [{"char": "Blade", "line": "Hold it."}]}
    # No STT model / no real clip: the report must still be recorded, marked
    # fail (extraction impossible), and never raise.
    clip = tmp_path / "video" / "s01_sh01.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"not a real video")
    rep = run_shot_qc(show, 1, clip, shot, cfg=None)
    assert rep["verdict"] in ("pass", "fail", "skip")
    summary = show.dir / "runs" / "EP01" / "qc" / "summary.json"
    assert summary.exists()
    data = __import__("json").loads(summary.read_text(encoding="utf-8"))
    assert data["s01_sh01"]["shot_id"] == "s01_sh01"
