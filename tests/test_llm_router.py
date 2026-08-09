"""Tests for the LM Studio LLM router (primary Beast5 -> fallback Beast3, concurrency limit)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.clients.lmstudio import LLMRouter, LMStudioClient, _matches, shared_router


def _router(limit=4, cooldown_s=30.0) -> LLMRouter:
    return LLMRouter("http://127.0.0.1:1234/v1",
                     "http://192.168.50.173:1235/v1",
                     limit=limit, cooldown_s=cooldown_s)


def test_prefers_primary_while_idle():
    r = _router()
    assert r.acquire() == r.primary
    assert r.snapshot()["inflight"][r.primary] == 1


def test_falls_back_when_primary_at_limit():
    r = _router(limit=2)
    r.acquire()
    r.acquire()
    assert r.acquire() == r.fallback, "primary full -> fallback"
    snap = r.snapshot()
    assert snap["inflight"][r.primary] == 2
    assert snap["inflight"][r.fallback] == 1


def test_returns_to_primary_after_release():
    r = _router(limit=1)
    u = r.acquire()
    assert u == r.primary
    r.release(u)
    assert r.acquire() == r.primary


def test_cooldown_routes_around_failed_primary():
    r = _router(limit=4, cooldown_s=60.0)
    r.note_failure(r.primary)
    assert r.acquire() == r.fallback


def test_cooldown_expires():
    import time
    r = _router(limit=4, cooldown_s=0.0)
    r.note_failure(r.primary)
    assert r.acquire() == r.primary


def test_matches_attaches_only_configured_urls():
    r = _router()
    assert _matches(r, "http://127.0.0.1:1234/v1") is r
    assert _matches(r, "http://192.168.50.173:1235/v1") is r
    assert _matches(r, "http://example.com:9999/v1") is None
    assert _matches(None, "http://127.0.0.1:1234/v1") is None


def test_client_auto_attaches_router():
    r = _router()
    c_primary = LMStudioClient("http://127.0.0.1:1234/v1", router=r)
    assert c_primary.router is r
    c_fb = LMStudioClient("http://192.168.50.173:1235/v1", router=r)
    assert c_fb.router is r
    # no explicit router + unmatched URL -> auto-attach finds nothing
    c_other = LMStudioClient("http://example.com:9999/v1")
    assert c_other.router is None


def test_shared_router_matches_config(monkeypatch):
    import studio.clients.lmstudio as mod

    class FakeConfig:
        def get(self, section, key, default=None):
            if section == "llm":
                return {"base_url": "http://127.0.0.1:1234/v1",
                        "fallback_url": "http://192.168.50.173:1235/v1",
                        "concurrency_limit": 4}.get(key, default)
            return default

    monkeypatch.setattr("studio.config.get_config", lambda: FakeConfig())
    mod._shared_router = None
    r = shared_router()
    assert r.primary == "http://127.0.0.1:1234/v1"
    assert r.fallback == "http://192.168.50.173:1235/v1"
    assert r.limit == 4


def test_router_is_thread_safe_at_limit():
    import threading
    import time

    r = _router(limit=4)
    counts = {"primary": 0, "fallback": 0}
    lock = threading.Lock()

    def worker():
        u = r.acquire()
        with lock:
            counts["primary" if u == r.primary else "fallback"] += 1
        time.sleep(0.005)   # hold the slot so the primary saturates
        r.release(u)

    threads = [threading.Thread(target=worker) for _ in range(64)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(counts.values()) == 64
    assert counts["primary"] >= 1 and counts["fallback"] >= 1
    assert r.snapshot()["inflight"][r.primary] == 0
    assert r.snapshot()["inflight"][r.fallback] == 0


# --- GPU guard: prefer fallback while the primary host is rendering ---------

def _router_with_guards(busy: bool, limit=4) -> LLMRouter:
    r = LLMRouter("http://127.0.0.1:1234/v1",
                  "http://192.168.50.173:1235/v1",
                  limit=limit,
                  gpu_guard_urls=["http://127.0.0.1:8188", "http://127.0.0.1:8189"],
                  gpu_check_ttl=60.0)
    r._gpu_busy_flag = busy
    r._gpu_checked = 0.0
    r._comfy_busy = lambda url: busy
    return r


def test_gpu_busy_routes_llm_to_fallback(monkeypatch):
    r = _router_with_guards(busy=True)
    assert r.acquire() == r.fallback, "primary GPU rendering -> use Beast3 for LLM"
    assert r.acquire() == r.fallback


def test_gpu_idle_keeps_llm_on_primary():
    r = _router_with_guards(busy=False)
    assert r.acquire() == r.primary


def test_gpu_busy_still_uses_primary_when_fallback_full(monkeypatch):
    r = _router_with_guards(busy=True, limit=1)
    assert r.acquire() == r.fallback     # first goes to fallback
    assert r.acquire() == r.primary      # fallback at limit -> primary anyway


def test_gpu_guard_urls_from_config(monkeypatch):
    import studio.clients.lmstudio as mod

    class FakeConfig:
        def get(self, section, key, default=None):
            if section == "llm":
                return {"base_url": "http://127.0.0.1:1234/v1",
                        "fallback_url": "http://192.168.50.173:1235/v1",
                        "concurrency_limit": 4,
                        "gpu_guard_urls": ["http://127.0.0.1:8188"]}.get(key, default)
            return default

    monkeypatch.setattr("studio.config.get_config", lambda: FakeConfig())
    mod._shared_router = None
    r = shared_router()
    assert r.gpu_guard_urls == ["http://127.0.0.1:8188"]

