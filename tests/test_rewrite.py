"""Tests for the one-off rewrite-with-a-chosen-model path (studio/rewrite.py)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import rewrite
from studio.config import Config
from studio.show import Show


def _script() -> dict:
    return {
        "episode": 1, "summary": "x", "cast": ["Ren"],
        "scenes": [{
            "id": "s01", "location": "dojo", "summary": "x", "characters": ["Ren"],
            "shots": [{"id": "s01_sh01", "duration_s": 10, "action": "Ren fights",
                       "dialogue": [{"char": "Ren", "line": "Hello there, I have been waiting for you to arrive at last."},
                                    {"char": "Ren", "line": "Start now and do not stop until this entire thing is finished tonight."}],
                       "references": {"characters": ["Ren"]}}],
        }],
    }


def _make_show(tmp_path) -> Show:
    d = tmp_path / "shows" / "demo"
    (d / "characters").mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=d)
    show.write_bible({"title": "Demo", "content_policy": "mature", "runtime_target_s": 10})
    show.write_character({"id": "ren", "name": "Ren", "appearance_canonical": "x"})
    show.write_continuity({"episode": 0})
    runs = d / "runs" / "EP01"
    runs.mkdir(parents=True, exist_ok=True)
    s = _script()
    s["_review"] = {"passed": False, "rounds": 1,
                    "reviews": {"slop": {"grade": "Fail", "pass": False,
                                         "review": "cliche"}}}
    (runs / "script.r1.json").write_text(json.dumps(s), encoding="utf-8")
    return show


class _FakeLLM:
    """Serves a revised script for the rewrite call and a pass for each reviewer."""

    def __init__(self):
        self.rewrite_calls = 0

    def chat_json(self, messages, **kw):
        user = messages[-1]["content"]
        if "Revise the script below" in user:
            self.rewrite_calls += 1
            return {"script": _script()}
        return {"grade": "Pass", "review": ""}

    def chat(self, messages, **kw):
        return "{}"


class _FakeCfg:
    def __init__(self, data):
        self._d = data

    def get(self, *keys):
        keys = list(keys)
        default = None
        if keys and not isinstance(keys[-1], str):
            default = keys.pop()
        cur = self._d
        for k in keys:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k)
            if cur is None:
                return default
        return cur


def test_notes_text_from_review_verdict():
    review = {"passed": False, "rounds": 1,
              "reviews": {"slop": {"grade": "Fail", "pass": False, "review": "fix a"},
                          "pacing": {"grade": "Pass", "pass": True, "review": ""}}}
    assert rewrite._notes_text(review) == "[slop] fix a"
    assert rewrite._notes_text({"reviews": {}}) == ""


def test_pickable_models_falls_back_to_config():
    cfg = _FakeCfg({"llm": {"roles": {"showrunner": "dolphin"}, "model": "dolphin"}})
    out = rewrite.pickable_models(cfg, ttl=0)
    assert out["local"] == ["dolphin"]
    assert "remote" not in out


def test_pickable_models_reads_live_list(monkeypatch):
    cfg = _FakeCfg({"llm": {
        "base_url": "http://local/v1",
        "roles": {"showrunner": "dolphin"}, "model": "dolphin"}})

    def fake_ids(url, headers=None, timeout=5.0):
        return ["localA", "localB"]

    monkeypatch.setattr(rewrite, "_http_model_ids", fake_ids)
    out = rewrite.pickable_models(cfg, ttl=0)
    assert out["local"] == ["localA", "localB"]


def test_pickable_models_falls_back_when_provider_down(monkeypatch):
    cfg = _FakeCfg({"llm": {
        "base_url": "http://local/v1",
        "roles": {"showrunner": "dolphin"}, "model": "dolphin"}})

    def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(rewrite, "_http_model_ids", boom)
    out = rewrite.pickable_models(cfg, ttl=0)
    assert out["local"] == ["dolphin"]
    assert "remote" not in out


def test_rewrite_runs_once_and_re_reviews(tmp_path):
    show = _make_show(tmp_path)
    fake = _FakeLLM()
    script = rewrite.rewrite_script_once(
        show, 1, "dolphin-2.9.3-mistral-nemo-12b",
        cfg=Config(tmp_path), llm=fake)
    assert fake.rewrite_calls == 1
    new = show.dir / "runs" / "EP01" / "script.r2.json"
    assert new.exists()
    assert script["_review"]["passed"] is True
    assert script["_review"]["rounds"] == 2


def test_rewrite_requires_a_script(tmp_path):
    d = tmp_path / "shows" / "empty"
    (d / "characters").mkdir(parents=True, exist_ok=True)
    show = Show("empty", root=d)
    show.write_bible({"title": "e", "runtime_target_s": 10})
    with pytest.raises(FileNotFoundError):
        rewrite.rewrite_script_once(show, 1, "dolphin", cfg=Config(tmp_path), llm=_FakeLLM())


# ---- outline regeneration --------------------------------------------------

class _CaptureClient:
    def __init__(self):
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append(kw)
        return "x"

    def chat_json(self, messages, **kw):
        self.calls.append(kw)
        return {}


def test_model_override_forces_writer_calls_not_reviewers():
    from studio.planner import _ModelOverride
    cap = _CaptureClient()
    ov = _ModelOverride(cap, model="big-local")
    ov.chat([{"role": "user", "content": "x"}], model="dolphin", role="showrunner")
    ov.chat([{"role": "user", "content": "x"}], model="dolphin", role="slop_reviewer")
    assert cap.calls[0]["model"] == "big-local"
    # reviewer call untouched -> keeps its own role model
    assert cap.calls[1]["model"] == "dolphin"


def test_regenerate_outline_wraps_the_llm(tmp_path, monkeypatch):
    show = _make_show(tmp_path)
    captured = {}
    import studio.planner as P

    def fake_gep(show, episode, llm=None, cfg=None, notes=""):
        captured["llm"] = llm
        return {"plan_review": {"passed": True, "rounds": 2, "notes": []}}

    monkeypatch.setattr(P, "generate_episode_plan", fake_gep)

    class _Base:
        pass

    out = rewrite.regenerate_outline_once(show, 1, "dolphin-2.9.3-mistral-nemo-12b",
                                          cfg=Config(tmp_path), llm=_Base())
    assert out["plan_review"]["passed"] is True
    wrapped = captured["llm"]
    assert isinstance(wrapped, P._ModelOverride)
    assert wrapped._model == "dolphin-2.9.3-mistral-nemo-12b"
