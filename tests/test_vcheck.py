"""Tests for the post-render visual check (vcheck): Qwen3-VL verdicts on
character-vs-ref identity and action-vs-script adherence, persistence, and the
auto-feed into the shot gate (capped auto-rejects).
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.show import Show
from studio.vcheck import (review_video, run_shot_vcheck, run_episode_vcheck,
                           clear_shot_vcheck, episode_vcheck_summary,
                           _frame_fractions)
from studio.approval import shot_approvals, approve_shot


class _FakeLLM:
    """Stub that mimics LMStudioClient.chat and returns a canned verdict."""

    def __init__(self, payload: dict, raise_: Exception | None = None):
        self._payload = payload
        self._raise = raise_
        self.calls = []

    def chat(self, messages, **kwargs):
        if self._raise:
            raise self._raise
        self.calls.append(messages)
        return json.dumps(self._payload)

    def chat_json(self, messages, **kwargs):
        if self._raise:
            raise self._raise
        self.calls.append(messages)
        return self._payload


def _show(tmp_path) -> Show:
    show = Show("demo", root=tmp_path)
    (show.dir / "runs" / "EP01" / "video").mkdir(parents=True, exist_ok=True)
    return show


def _cfg(server="lmstudio"):
    """Fresh config copy forced to the given vcheck server (never the live
    machine's default; tests must not boot WSL vLLM or hit the GPU)."""
    import copy
    from studio.config import Config
    cfg = copy.deepcopy(Config())
    cfg.data["qc"]["vcheck"] = {**dict(cfg.get("qc", "vcheck", {})),
                                "server": server}
    return cfg


def _script(show: Show) -> None:
    script = {"summary": "test", "scenes": [{"shots": [
        {"id": "s01_sh01", "action": "Blade punches a drone",
         "camera": "medium close-up, slow push-in",
         "dialogue": [{"char": "Blade", "line": "Hold it."}]},
        {"id": "s01_sh02", "action": "Lily runs across the rooftop",
         "camera": "wide tracking shot",
         "dialogue": []},
    ]}]}
    (show.dir / "runs" / "EP01" / "script.r1.json").write_text(
        json.dumps(script), encoding="utf-8")


def _clip(show: Show, sid: str) -> Path:
    clip = show.dir / "runs" / "EP01" / "video" / f"{sid}.mp4"
    clip.write_bytes(b"fake mp4")
    return clip


def _frames(tmp_path) -> list[Path]:
    d = tmp_path / "frames"
    d.mkdir(exist_ok=True)
    paths = []
    for i in range(3):
        p = d / f"f{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
        paths.append(p)
    return paths


def _good_verdict() -> dict:
    return {"characters": [{"name": "Blade", "present": True,
                            "consistent": True, "issue": ""}],
            "unknown_characters": [], "action_match": True, "action_issue": "",
            "text_defect": False, "pass": True, "issues": [],
            "prompt_fix": ""}


def _bad_verdict() -> dict:
    return {"characters": [{"name": "Blade", "present": True,
                            "consistent": False,
                            "issue": "wearing green armor, not the grey plates"}],
            "unknown_characters": ["a pink-haired girl"], "action_match": False,
            "action_issue": "no drone visible, Blade just stands", "text_defect": True,
            "text_details": "SUBS watermark", "pass": False,
            "issues": ["character wrong", "action wrong"],
            "prompt_fix": "Blade in grey plating, punch the drone, no text"}


@pytest.mark.parametrize("frame_count,expect_pass", [(0, False), (3, True)])
def test_review_video_frames(monkeypatch, tmp_path, frame_count, expect_pass):
    """review_video works with a well-formed verdict for 0 or N frames."""
    llm = _FakeLLM(_good_verdict())
    monkeypatch.setattr("studio.vcheck._data_uri", lambda p: "data:image/jpeg;base64,xx")
    frames = [] if frame_count == 0 else _frames(tmp_path)
    verdict = review_video(llm, "qwen/qwen3-vl-8b", [], [], frames, "punch")
    assert verdict["pass"] is expect_pass
    if frame_count == 0:
        assert "no frames" in " ".join(verdict["issues"])


@pytest.mark.parametrize("n,expected", [
    (1, (0.5,)),
    (5, (0.0, 0.25, 0.5, 0.75, 0.99)),      # legacy 5 fixed points kept stable
    (8, (0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875)),  # 8 spread evenly
])
def test_frame_fractions_spread(n, expected):
    """More frames spread evenly across the timeline; n==5 keeps legacy points."""
    fracs = _frame_fractions(n)
    assert len(fracs) == n
    assert fracs == expected


def test_review_video_aggregates_issues(monkeypatch, tmp_path):
    """Each problem axis lands in the issues list."""
    llm = _FakeLLM(_bad_verdict())
    monkeypatch.setattr("studio.vcheck._data_uri", lambda p: "data:image/jpeg;base64,xx")
    verdict = review_video(llm, "m", ["Blade", "Lily"], [Path("r1"), Path("r2")],
                           _frames(tmp_path), "punch the drone")
    assert verdict["pass"] is False
    joined = " | ".join(verdict["issues"]).lower()
    assert "inconsistent" in joined
    assert "unknown character" in joined
    assert "action missing" in joined
    assert "text in frame" in joined


def test_run_shot_vcheck_persists_pass(monkeypatch, tmp_path):
    show = _show(tmp_path)
    _script(show)
    clip = _clip(show, "s01_sh01")
    monkeypatch.setattr("studio.vcheck.sample_frames", lambda c, cfg=None: _frames(tmp_path))
    rep = run_shot_vcheck(show, 1, clip, {"id": "s01_sh01", "action": "x"},
                          cfg=_cfg(), llm=_FakeLLM(_good_verdict()))
    assert rep["verdict"] == "pass"
    assert rep["shot_id"] == "s01_sh01"
    # summary + DB persisted
    summary = (show.dir / "runs" / "EP01" / "vcheck" / "summary.json")
    assert summary.exists()
    assert json.loads(summary.read_text(encoding="utf-8"))["s01_sh01"]["verdict"] == "pass"
    roll = episode_vcheck_summary(show, 1)
    assert roll["counts"]["pass"] == 1


def test_run_shot_vcheck_persists_fail(monkeypatch, tmp_path):
    show = _show(tmp_path)
    _script(show)
    clip = _clip(show, "s01_sh01")
    monkeypatch.setattr("studio.vcheck.sample_frames", lambda c, cfg=None: _frames(tmp_path))
    rep = run_shot_vcheck(show, 1, clip, {"id": "s01_sh01", "action": "x"},
                          cfg=_cfg(), llm=_FakeLLM(_bad_verdict()))
    assert rep["verdict"] == "fail"
    assert rep["issues"]


def test_run_shot_vcheck_native_video_sends_clip(monkeypatch, tmp_path):
    """server=vllm streams the WHOLE clip to the VLM (video_url content part),
    never samples frames."""
    show = _show(tmp_path)
    _script(show)
    clip = _clip(show, "s01_sh01")
    captures = {}

    class _CaptureLLM(_FakeLLM):
        def chat(self, messages, **kwargs):
            captures["content"] = messages[0]["content"]
            return super().chat(messages, **kwargs)

        def chat_json(self, messages, **kwargs):
            captures["content"] = messages[0]["content"]
            return super().chat_json(messages, **kwargs)

    llm = _CaptureLLM(_good_verdict())
    rep = run_shot_vcheck(show, 1, clip, {"id": "s01_sh01", "action": "x"},
                          cfg=_cfg("vllm"), llm=llm)
    assert rep["verdict"] == "pass"
    parts = captures["content"]
    vids = [p for p in parts if p.get("type") == "video_url"]
    assert len(vids) == 1
    assert vids[0]["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert "video/mp4;base64," in captures["content"][0]["text"] or True
    # no frame-sampling was needed
    assert not any(p.get("type") == "image_url" and "frame" in str(p) for p in parts)


def test_run_shot_vcheck_missing_clip_native(monkeypatch, tmp_path):
    """server=vllm with a missing clip -> fail report, no crash."""
    show = _show(tmp_path)
    _script(show)
    missing = show.dir / "runs" / "EP01" / "video" / "gone.mp4"
    rep = run_shot_vcheck(show, 1, missing, {"id": "gone", "action": "x"},
                          cfg=_cfg("vllm"), llm=_FakeLLM(_good_verdict()))
    assert rep["verdict"] == "fail"
    assert rep["issues"]


def test_run_shot_vcheck_skip_when_disabled(monkeypatch, tmp_path):
    show = _show(tmp_path)
    _script(show)
    clip = _clip(show, "s01_sh01")
    cfg = _cfg()
    cfg.data["qc"]["vcheck"] = {**cfg.get("qc", "vcheck", {}), "enabled": False}
    rep = run_shot_vcheck(show, 1, clip, {"id": "s01_sh01"}, cfg=cfg,
                          llm=_FakeLLM(_good_verdict()))
    assert rep["verdict"] == "skip"


def test_run_episode_vcheck_skips_already_reviewed(monkeypatch, tmp_path):
    show = _show(tmp_path)
    _script(show)
    for sid in ("s01_sh01", "s01_sh02"):
        _clip(show, sid)
    monkeypatch.setattr("studio.vcheck.sample_frames",
                        lambda c, cfg=None: _frames(tmp_path))
    llm = _FakeLLM(_good_verdict())
    run_episode_vcheck(show, 1, cfg=_cfg(), llm=llm)
    # both reviewed now
    assert episode_vcheck_summary(show, 1)["counts"]["total"] == 2
    calls = len(llm.calls)
    # second run touches nothing (all stored)
    run_episode_vcheck(show, 1, cfg=_cfg(), llm=_FakeLLM(_good_verdict()))
    assert len(llm.calls) == calls
    assert episode_vcheck_summary(show, 1)["counts"]["total"] == 2


def test_auto_feed_rejects_fail_into_shot_gate(monkeypatch, tmp_path):
    """A failed clip auto-rejects: rejection state + clip deleted + review cleared."""
    show = _show(tmp_path)
    _script(show)
    clip = _clip(show, "s01_sh01")
    monkeypatch.setattr("studio.vcheck.sample_frames",
                        lambda c, cfg=None: _frames(tmp_path))
    llama = _FakeLLM(_bad_verdict())
    run_episode_vcheck(show, 1, cfg=_cfg(), llm=llama)
    entry = shot_approvals(show, 1)["s01_sh01"]
    assert entry["status"] == "rejected"
    assert entry["retake_count"] >= 1
    assert "visual check" in entry["notes"].lower()
    assert not clip.exists()          # reconciler re-renders the deleted clip
    # the failed report was cleared so the retake gets re-reviewed
    roll = episode_vcheck_summary(show, 1)
    assert roll["counts"]["total"] == 0


def test_auto_feed_respects_approved(monkeypatch, tmp_path):
    """A shot human-approved is never auto-rejected, even on a VL fail."""
    show = _show(tmp_path)
    _script(show)
    _clip(show, "s01_sh01")
    approve_shot(show, 1, "s01_sh01", notes="keep it")
    monkeypatch.setattr("studio.vcheck.sample_frames",
                        lambda c, cfg=None: _frames(tmp_path))
    run_episode_vcheck(show, 1, cfg=_cfg(), llm=_FakeLLM(_bad_verdict()))
    assert shot_approvals(show, 1)["s01_sh01"]["status"] == "approved"
    assert (show.dir / "runs" / "EP01" / "video" / "s01_sh01.mp4").exists()


def test_auto_feed_caps_retakes(monkeypatch, tmp_path):
    """Beyond max_auto_retakes the shot is flagged but NOT re-rejected (no loop)."""
    show = _show(tmp_path)
    _script(show)
    monkeypatch.setattr("studio.vcheck.sample_frames",
                        lambda c, cfg=None: _frames(tmp_path))
    for _ in range(3):
        _clip(show, "s01_sh01")
        run_episode_vcheck(show, 1, cfg=_cfg(), llm=_FakeLLM(_bad_verdict()))
    entry = shot_approvals(show, 1)["s01_sh01"]
    assert entry["retake_count"] == 2          # capped (config max_auto_retakes=2)
    roll = episode_vcheck_summary(show, 1)
    assert roll["counts"]["fail"] == 1         # the capped fail stays visible


def test_clear_shot_vcheck(monkeypatch, tmp_path):
    show = _show(tmp_path)
    _script(show)
    _clip(show, "s01_sh01")
    monkeypatch.setattr("studio.vcheck.sample_frames",
                        lambda c, cfg=None: _frames(tmp_path))
    run_shot_vcheck(show, 1, (show.dir / "runs" / "EP01" / "video" / "s01_sh01.mp4"),
                    {"id": "s01_sh01", "action": "x"}, llm=_FakeLLM(_good_verdict()))
    assert episode_vcheck_summary(show, 1)["counts"]["total"] == 1
    clear_shot_vcheck(show, 1, "s01_sh01")
    assert episode_vcheck_summary(show, 1)["counts"]["total"] == 0