"""Tests for the episode pipeline's self-healing recovery.

The reconciler must never let a stuck background job stall an episode forever:
a storyboard/render job whose worker thread died (zombie) or that has made no
progress for ``pipeline.stale_job_s`` (wedged ComfyUI) is interrupted and
restarted, while a healthy running job is left alone.
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.show import Show
import studio.episode_repair as er
import studio.storyboard as sb_mod
import studio.render as render_mod


def _bootstrap_complete(show: Show) -> None:
    show.set_bootstrap_state({
        "concept": {"status": "approved"},
        "bible": {"status": "approved"},
        "characters": [
            {"name": "Blade", "proposal": "approved", "refs": "approved", "voice": "approved"},
        ],
        "scenes": {"status": "approved"},
        "complete": True,
    })


def _episode_on_disk(show: Show, episode: int = 1) -> None:
    d = show.dir / "runs" / f"EP{episode:02d}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text(
        json.dumps({"status": "approved", "scenes": [{"id": "s01"}]}), encoding="utf-8")
    (d / "scene_details.json").write_text(
        json.dumps({"status": "approved", "scenes": [{"id": "s01"}]}), encoding="utf-8")
    (d / "script.r1.json").write_text(
        json.dumps({"scenes": [{"shots": [{"id": "s01_sh01"}]}]}), encoding="utf-8")


def _show(tmp_path) -> Show:
    show = Show("demo", root=tmp_path)
    _bootstrap_complete(show)
    _episode_on_disk(show)
    return show


# ---------------------------------------------------------------------------
# ComfyClient.wait hard wall-clock cap
# ---------------------------------------------------------------------------

def test_wait_hard_timeout_interrupts_wedged_job(monkeypatch):
    """A prompt stuck in queue_running forever is interrupted and raises instead
    of blocking the caller indefinitely (the queue-aware grace period alone can
    never expire while the prompt stays in the queue)."""
    import httpx
    from studio.clients.comfy import ComfyClient

    client = ComfyClient("http://127.0.0.1:9999", timeout=5)
    interrupted: list[bool] = []
    monkeypatch.setattr(client, "history", lambda pid: {})
    monkeypatch.setattr(client, "interrupt", lambda: interrupted.append(True))

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"queue_running": [[0, "pid-1"]], "queue_pending": []}

    class FakeCtx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            return FakeResp()

    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: FakeCtx())

    with pytest.raises(TimeoutError):
        client.wait("pid-1", timeout_s=60, poll_interval=0.01, hard_timeout_s=0.2)
    assert interrupted == [True]


# ---------------------------------------------------------------------------
# Stale-job helpers
# ---------------------------------------------------------------------------

def test_job_stale_uses_config_cap(tmp_path, monkeypatch):
    from studio.config import Config
    cfg = Config(tmp_path)
    cfg.data["pipeline"]["stale_job_s"] = 60
    monkeypatch.setattr(er, "get_config", lambda: cfg)
    assert er._job_stale({"state": "running", "ts": time.time() - 61}, cfg=cfg)
    assert not er._job_stale({"state": "running", "ts": time.time() - 10}, cfg=cfg)
    # A job without a ts stamp (legacy shape) is never declared stale.
    assert not er._job_stale({"state": "running"}, cfg=cfg)


# ---------------------------------------------------------------------------
# Reconciler watchdog: stale / zombie storyboard + render jobs
# ---------------------------------------------------------------------------

def _fake_running(stale: bool = False, alive: bool = True) -> dict:
    return {"state": "running", "done": 0, "total": 0,
            "ts": time.time() - 3600 if stale else time.time(),
            "alive": alive, "detail": ""}


def test_reconcile_restarts_stale_storyboard(tmp_path, monkeypatch):
    """A running storyboard job with no progress for stale_job_s is recovered
    (renderers interrupted + wedged instances restarted) and re-kicked instead
    of left hanging forever."""
    show = _show(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(sb_mod, "storyboard_status", lambda show_id: _fake_running(stale=True))
    monkeypatch.setattr(sb_mod, "stop_storyboard", lambda show_id: calls.append("stop"))
    monkeypatch.setattr(sb_mod, "build_storyboard", lambda s, e, cfg=None: calls.append("build"))
    monkeypatch.setattr(er, "_recover_renderers", lambda cfg=None: calls.append("recover"))

    log_ = er._advance_episode(show, 1)
    assert "recover" in calls and "stop" in calls and "build" in calls
    assert any("storyboard" in line for line in log_)


def test_reconcile_restarts_stale_render(tmp_path, monkeypatch):
    """A running render job with no progress for stale_job_s is recovered and
    re-kicked instead of left hanging forever."""
    show = _show(tmp_path)
    (show.dir / "runs" / "EP01" / "storyboard").mkdir(parents=True, exist_ok=True)
    (show.dir / "runs" / "EP01" / "storyboard" / "s01_sh01.mp4").write_bytes(b"x")
    calls: list[str] = []
    monkeypatch.setattr(render_mod, "render_status", lambda show_id: _fake_running(stale=True))
    monkeypatch.setattr(render_mod, "build_render", lambda s, e, cfg=None: calls.append("build"))
    monkeypatch.setattr(er, "_recover_renderers", lambda cfg=None: calls.append("recover"))

    er._advance_episode(show, 1)
    assert "recover" in calls and "build" in calls
    assert show.show_id not in er.RENDER_JOBS or er.RENDER_JOBS.get(show.show_id, {}).get("state") != "running"


def test_reconcile_does_not_restart_healthy_storyboard(tmp_path, monkeypatch):
    """A running job that is still making progress is never re-kicked."""
    show = _show(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(sb_mod, "storyboard_status", lambda show_id: _fake_running(stale=False))
    monkeypatch.setattr(sb_mod, "stop_storyboard", lambda show_id: calls.append("stop"))
    monkeypatch.setattr(sb_mod, "build_storyboard", lambda s, e, cfg=None: calls.append("build"))
    monkeypatch.setattr(er, "_recover_renderers", lambda cfg=None: calls.append("recover"))

    log_ = er._advance_episode(show, 1)
    assert calls == []
    assert log_ == []


def test_reconcile_restarts_zombie_render(tmp_path, monkeypatch):
    """A render job whose worker thread died (running but alive=False) restarts."""
    show = _show(tmp_path)
    (show.dir / "runs" / "EP01" / "storyboard").mkdir(parents=True, exist_ok=True)
    (show.dir / "runs" / "EP01" / "storyboard" / "s01_sh01.mp4").write_bytes(b"x")
    calls: list[str] = []
    monkeypatch.setattr(render_mod, "render_status", lambda show_id: _fake_running(stale=False, alive=False))
    monkeypatch.setattr(render_mod, "build_render", lambda s, e, cfg=None: calls.append("build"))

    er._advance_episode(show, 1)
    assert calls == ["build"]


# ---------------------------------------------------------------------------
# Hard-wedged instance detection + restart escalation
# ---------------------------------------------------------------------------

def test_comfy_wedged_detects_dead_system_stats(monkeypatch):
    """An instance whose /system_stats fails while the port is occupied is wedged."""
    from studio.clients.comfy import ComfyClient
    class DeadClient(ComfyClient):
        def health(self):
            return False
    import studio.clients.comfy as comfy_mod
    monkeypatch.setattr(comfy_mod, "ComfyClient", DeadClient)
    assert er._comfy_wedged("http://127.0.0.1:9999", 900)


def test_comfy_wedged_detects_stuck_queue(monkeypatch):
    """A healthy instance with a queue_running job older than the cap is wedged."""
    import httpx

    class HealthyClient:
        def __init__(self, url, *a, **kw):
            self.base_url = url

        def health(self):
            return True

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            old_ms = (time.time() - 3600) * 1000.0
            return {"queue_running": [[0, "pid-old", {}, old_ms, ["9"]]],
                    "queue_pending": []}

    class FakeCtx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            return FakeResp()

    import studio.clients.comfy as comfy_mod
    monkeypatch.setattr(comfy_mod, "ComfyClient", HealthyClient)
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: FakeCtx())
    assert er._comfy_wedged("http://127.0.0.1:9999", 900)


def test_comfy_wedged_fresh_queue_not_wedged(monkeypatch):
    """A healthy instance with a recent running job is a legitimate render."""
    import httpx

    class HealthyClient:
        def __init__(self, url, *a, **kw):
            self.base_url = url

        def health(self):
            return True

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"queue_running": [[0, "pid-fresh", {}, time.time() * 1000.0, ["9"]]],
                    "queue_pending": []}

    class FakeCtx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            return FakeResp()

    import studio.clients.comfy as comfy_mod
    monkeypatch.setattr(comfy_mod, "ComfyClient", HealthyClient)
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: FakeCtx())
    assert not er._comfy_wedged("http://127.0.0.1:9999", 900)


def test_restart_renderers_stops_and_starts_wedged(monkeypatch):
    """A wedged configured instance is stopped + relaunched; a healthy one isn't."""
    from studio.config import Config
    cfg = Config(".")
    cfg.data.setdefault("env", {}).setdefault("comfyui", {})["krea2"] = {
        "url": "http://127.0.0.1:8190"}
    cfg.data.setdefault("env", {})["comfyui"]["h3"] = {"url": "http://127.0.0.1:8188"}
    calls: list[str] = []
    monkeypatch.setattr(er, "_comfy_wedged", lambda url, age: url.endswith("8190"))
    monkeypatch.setattr(er, "_stale_job_s", lambda c=None: 900)

    class FakeOps:
        def comfy_stop(self, which):
            calls.append(f"stop:{which}")

        def comfy_start(self, which):
            calls.append(f"start:{which}")

    import studio.remote.ops as ops_mod
    monkeypatch.setattr(ops_mod, "ServiceOps", lambda *a, **kw: FakeOps())
    er._restart_renderers(cfg)
    assert calls == ["stop:krea2", "start:krea2"]


def test_recover_renderers_interrupts_then_restarts(monkeypatch):
    """Two-stage recovery: interrupt everything, then restart the hard-wedged."""
    calls: list[str] = []
    monkeypatch.setattr(er, "_interrupt_renderers", lambda cfg=None: calls.append("interrupt"))
    monkeypatch.setattr(er, "_restart_renderers", lambda cfg=None: calls.append("restart"))
    er._recover_renderers()
    assert calls == ["interrupt", "restart"]

