"""Tests for the per-shot approval gate (Gate 4): approve / reject-with-notes
on rendered clips, prompt revision persistence, and clip/QC deletion on reject.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.show import Show
from studio.approval import (approve_shot, reject_shot, shot_approvals,
                             shot_state, shot_approval_path)


class _FakeLLM:
    """Stub that mimics LMStudioClient.chat and returns a prompt revision."""

    def __init__(self, text: str = "", raise_: Exception | None = None):
        self._text = text
        self._raise = raise_

    def chat(self, messages, **kwargs):
        if self._raise:
            raise self._raise
        return self._text

    def chat_json(self, messages, **kwargs):
        if self._raise:
            raise self._raise
        return json.loads(self._text)


def _show(tmp_path) -> Show:
    show = Show("demo", root=tmp_path)
    (show.dir / "runs" / "EP01" / "video").mkdir(parents=True, exist_ok=True)
    (show.dir / "runs" / "EP01" / "qc").mkdir(parents=True, exist_ok=True)
    return show


def _clip(show: Show, sid: str) -> Path:
    clip = show.dir / "runs" / "EP01" / "video" / f"{sid}.mp4"
    clip.write_bytes(b"fake mp4")
    return clip


def _script(show: Show) -> None:
    # A minimal script so _current_shot_prompt can resolve the shot.
    script = {"summary": "test", "scenes": [{"shots": [
        {"id": "s01_sh01", "action": "Blade speaks",
         "dialogue": [{"char": "Blade", "line": "Hold it."}]},
    ]}]}
    (show.dir / "runs" / "EP01" / "script.r1.json").write_text(
        json.dumps(script), encoding="utf-8")


def test_approve_shot_records_state(tmp_path):
    show = _show(tmp_path)
    approve_shot(show, 1, "s01_sh01", notes="looks good")
    assert shot_state(show, 1, "s01_sh01")["status"] == "approved"
    assert shot_state(show, 1, "s01_sh01")["notes"] == "looks good"
    assert shot_approval_path(show, 1).exists()
    # untouched shots stay pending
    assert shot_state(show, 1, "s99_sh99")["status"] == "pending"


def test_reject_shot_deletes_clip_and_records(tmp_path):
    show = _show(tmp_path)
    clip = _clip(show, "s01_sh01")
    assert clip.exists()
    reject_shot(show, 1, "s01_sh01", notes="wrong character is speaking",
                llm=_FakeLLM(raise_=RuntimeError("no LLM in test")))
    entry = shot_state(show, 1, "s01_sh01")
    assert entry["status"] == "rejected"
    assert entry["notes"] == "wrong character is speaking"
    assert entry["retake_count"] == 1
    assert not clip.exists()          # reconciler re-renders the missing clip


def test_reject_without_llm_keeps_prompt_and_rerenders(tmp_path):
    """Without a reachable LLM, the revised prompt stays the current one and the
    clip is still deleted so the reconciler re-renders (never a dead end)."""
    show = _show(tmp_path)
    clip = _clip(show, "s01_sh01")
    reject_shot(show, 1, "s01_sh01", notes="garbled audio",
                llm=_FakeLLM(raise_=RuntimeError("LLM down")))
    entry = shot_state(show, 1, "s01_sh01")
    assert entry["status"] == "rejected"
    assert entry["retake_count"] == 1
    assert not clip.exists()


def test_reject_stores_llm_revised_prompt(tmp_path):
    """The revised prompt from the rejection notes is persisted so a re-render
    uses it AND a successive reject refines it (never resets to the base)."""
    show = _show(tmp_path)
    _script(show)
    _clip(show, "s01_sh01")
    revised = "REVISED: <Subject 1> says, <d>[English] Hold it.</d> no garbled audio."
    reject_shot(show, 1, "s01_sh01", notes="garbled audio",
                llm=_FakeLLM(text='{"prompt": "%s"}' % revised))
    entry = shot_state(show, 1, "s01_sh01")
    assert entry["revised_prompt"] == revised
    # Second reject refines the REVISED prompt (feedback loop), not the base.
    revised2 = ("REVISED2: <Subject 1> says, <d>[English] Hold it.</d> "
                "and no wrong character this time.")
    reject_shot(show, 1, "s01_sh01", notes="wrong character",
                llm=_FakeLLM(text='{"prompt": "%s"}' % revised2))
    entry2 = shot_state(show, 1, "s01_sh01")
    assert entry2["revised_prompt"] == revised2
    assert entry2["retake_count"] == 2


def test_successive_rejects_increment_retake(tmp_path):
    show = _show(tmp_path)
    for _ in range(3):
        _clip(show, "s01_sh01")
        reject_shot(show, 1, "s01_sh01", notes="still wrong",
                    llm=_FakeLLM(raise_=RuntimeError("LLM down")))
    entry = shot_state(show, 1, "s01_sh01")
    assert entry["retake_count"] == 3
    # persisted revised_prompt is present even without LLM (kept the current)
    assert "revised_prompt" in entry


def test_approve_keeps_clip(tmp_path):
    show = _show(tmp_path)
    clip = _clip(show, "s01_sh01")
    approve_shot(show, 1, "s01_sh01")
    assert clip.exists()  # approval must never delete the render
    assert shot_state(show, 1, "s01_sh01")["status"] == "approved"


def test_shot_approvals_file_shape(tmp_path):
    show = _show(tmp_path)
    _script(show)
    reject_shot(show, 1, "s01_sh01", notes="audio garbage",
                llm=_FakeLLM(raise_=RuntimeError("no LLM")))
    data = shot_approvals(show, 1)
    entry = data["s01_sh01"]
    assert entry["status"] == "rejected"
    assert entry["retake_count"] >= 1
    assert "decided_at" in entry
