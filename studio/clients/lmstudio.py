"""LM Studio client (OpenAI-compatible). See DESIGN.md §11.

Routing: BEAST5's own LM Studio (config `llm.base_url`) is the PRIMARY and every
call goes there while it has capacity. When the primary is busy at its
concurrency limit (config `llm.concurrency_limit`, 4 on both instances) or is
cooling down after a failure, calls fall back to `llm.fallback_url` (Beast3).
In-flight counts are tracked process-wide so parallel pipeline threads share
one budget; the router is attached automatically to any client whose base_url
matches the configured primary or fallback.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

class LMStudioError(RuntimeError):
    pass


def _effective_output_cap(caller: int | None) -> int | None:
    """Decide the outgoing output-token cap. ``None`` = DON'T SEND IT.

    ``max_output_tokens`` / ``max_tokens`` are OPTIONAL on both APIs: omit them
    and the model/server decides (its own max, or until EOS). Every model already
    caps its own output and unused budget is free (billing is on ACTUAL tokens),
    so a client-side cap buys nothing and only truncates a reasoning model ->
    empty responses, unparseable JSON, spurious local fallback.

    ``llm.max_output_tokens``:
      0 (default) -> no client cap: omit the field entirely.
      N > 0       -> optional ceiling: send max(caller hint, N).
    """
    try:
        from ..config import get_config
        floor = int(get_config().get("llm", "max_output_tokens", 0) or 0)
    except Exception:
        floor = 0
    if floor <= 0:
        return None
    try:
        return max(int(caller or 0), floor)
    except (TypeError, ValueError):
        return floor


class LLMRouter:
    """Thread-safe load balancer over a primary + fallback LM Studio endpoint.

    ``acquire()`` reserves a slot on the best URL for one in-flight request
    (preferring the primary while it is under the concurrency limit and not
    cooling down), ``release()`` frees it, and ``note_failure()`` puts a URL
    into cooldown so a down instance stops receiving new calls.

    ``gpu_guard_urls`` are ComfyUI instances on the primary host whose running
    queue marks that machine's GPU as busy (e.g. BEAST5's krea2 image gen and
    H3 video renderer). While any guard URL is rendering, LLM calls prefer the
    fallback node instead of competing with the render for VRAM.
    """

    def __init__(self, primary: str, fallback: str | None = None,
                 limit: int = 4, cooldown_s: float = 30.0,
                 gpu_guard_urls: list[str] | None = None,
                 gpu_check_ttl: float = 5.0):
        self.primary = primary.rstrip("/")
        self.fallback = fallback.rstrip("/") if fallback else None
        self.limit = max(1, int(limit))
        self.cooldown_s = float(cooldown_s)
        self.gpu_guard_urls = [u.rstrip("/") for u in (gpu_guard_urls or [])]
        self.gpu_check_ttl = float(gpu_check_ttl)
        self._lock = threading.Lock()
        self._gpu_lock = threading.Lock()
        self._inflight: dict[str, int] = {self.primary: 0}
        if self.fallback:
            self._inflight[self.fallback] = 0
        self._cooldown_until: dict[str, float] = {}
        self._gpu_busy_flag = False
        self._gpu_checked = 0.0

    def _candidates(self) -> list[str]:
        return [self.primary] + ([self.fallback] if self.fallback else [])

    def _comfy_busy(self, url: str) -> bool:
        """True when a ComfyUI instance has running/pending jobs (GPU in use).

        Unreachable = not busy: a down ComfyUI can't be rendering, so it must
        not block LLM routing.
        """
        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{url}/queue")
                resp.raise_for_status()
                q = resp.json()
                return bool(q.get("queue_running") or q.get("queue_pending"))
        except Exception:
            return False

    def _gpu_busy(self) -> bool:
        """Cached check: is the primary host's GPU busy rendering (image/video)?"""
        if not self.gpu_guard_urls:
            return False
        with self._gpu_lock:
            now = time.time()
            if now - self._gpu_checked < self.gpu_check_ttl:
                return self._gpu_busy_flag
            urls = list(self.gpu_guard_urls)
        busy = any(self._comfy_busy(u) for u in urls)
        with self._gpu_lock:
            self._gpu_busy_flag = busy
            self._gpu_checked = time.time()
        return busy

    def acquire(self) -> str:
        """Pick the best URL for a new request and reserve a slot on it."""
        gpu_busy = self._gpu_busy()
        with self._lock:
            now = time.time()
            # While the primary's GPU is rendering, prefer the fallback node.
            order = ([self.fallback, self.primary] if (gpu_busy and self.fallback)
                     else self._candidates())
            for url in order:
                if (self._inflight.get(url, 0) < self.limit
                        and now >= self._cooldown_until.get(url, 0.0)):
                    self._inflight[url] = self._inflight.get(url, 0) + 1
                    return url
            # Everything saturated or cooling down: least-loaded URL.
            url = min(order, key=lambda u: self._inflight.get(u, 0))
            self._inflight[url] = self._inflight.get(url, 0) + 1
            return url

    def release(self, url: str) -> None:
        with self._lock:
            if url in self._inflight and self._inflight[url] > 0:
                self._inflight[url] -= 1

    def note_failure(self, url: str) -> None:
        with self._lock:
            self._cooldown_until[url] = time.time() + self.cooldown_s

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            return {
                "primary": self.primary,
                "fallback": self.fallback,
                "limit": self.limit,
                "inflight": dict(self._inflight),
                "cooling": {u: max(0.0, until - now) for u, until in self._cooldown_until.items()},
            }


_router_lock = threading.Lock()
_shared_router: LLMRouter | None = None


def shared_router(cfg=None) -> LLMRouter | None:
    """Process-wide router built once from config; None when no fallback set."""
    global _shared_router
    if _shared_router is not None:
        return _shared_router
    try:
        from ..config import get_config
        cfg = cfg or get_config()
        primary = cfg.get("llm", "base_url") or "http://127.0.0.1:1234/v1"
        fallback = cfg.get("llm", "fallback_url") or ""
        limit = int(cfg.get("llm", "concurrency_limit", 4) or 4)
        with _router_lock:
            if _shared_router is None:
                guards = cfg.get("llm", "gpu_guard_urls", []) or []
                _shared_router = LLMRouter(primary, fallback or None, limit=limit,
                                           gpu_guard_urls=guards)
    except Exception:
        _shared_router = None
    return _shared_router


def _matches(router: LLMRouter | None, base_url: str) -> LLMRouter | None:
    """Attach the router to a client pointed at the configured primary/fallback."""
    if router is None or not base_url:
        return None
    u = base_url.rstrip("/")
    return router if u in (router.primary, router.fallback or "") else None


class LMStudioClient:
    def __init__(self, base_url: str = "http://127.0.0.1:1234/v1", timeout: float = 120.0,
                 router: LLMRouter | None = None):
        """OpenAI-compatible chat client for a local LM Studio instance.

        Every call goes to the local instance (primary, with the configured
        fallback node when the primary is busy). There is no remote provider.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.router = router if router is not None else _matches(shared_router(), base_url)
        self.last_provider = "local"
        self.last_resolved_model: str | None = None

    def _acquire(self) -> str:
        return self.router.acquire() if self.router else self.base_url

    def _release(self, url: str) -> None:
        if self.router:
            self.router.release(url)

    def _fail(self, url: str) -> None:
        if self.router:
            self.router.note_failure(url)

    def _http_timeout(self, override: float | None = None,
                      read: float | None = None) -> httpx.Timeout:
        """Connect is bounded; ``read`` is the caller's stall/safety bound.

        Streaming calls pass a short read timeout (no chunk for N seconds = a
        stalled server, not a slow one). Non-streaming calls pass the configured
        request budget so a dead request can never block a board job forever.
        """
        connect = float(override if override is not None else self.timeout)
        return httpx.Timeout(connect=min(connect, 30.0), read=read,
                             write=None, pool=None)

    def _read_timeout(self) -> float:
        try:
            from ..config import get_config
            return float(get_config().get("llm", "request_timeout_s", 3600) or 3600)
        except Exception:
            return 3600.0

    def _post(self, url: str, path: str, json_body: dict,
              headers: dict | None = None, timeout: float | None = None) -> dict:
        with httpx.Client(timeout=self._http_timeout(timeout,
                                                     read=self._read_timeout())) as client:
            resp = client.post(f"{url}{path}", json=json_body, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def _chat_stream(self, url: str, messages: list[dict], model: str, temperature: float,
                     max_tokens: int | None, on_progress,
                     headers: dict | None = None, timeout: float | None = None,
                     json_mode: bool = False) -> str:
        """Stream a chat completion; call on_progress(tokens, partial_text) per chunk."""
        body = {"model": model, "messages": messages, "temperature": temperature,
                "stream": True}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        parts: list[str] = []
        n = 0
        with httpx.Client(timeout=self._http_timeout(timeout, read=300.0)) as client:
            with client.stream("POST", f"{url}/chat/completions", json=body,
                               headers=headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = ((json.loads(payload).get("choices") or [{}])[0]
                                 .get("delta") or {}).get("content", "")
                    except Exception:
                        continue
                    if delta:
                        parts.append(delta)
                        n += 1
                        on_progress(n, "".join(parts))
        return "".join(parts)

    def chat(self, messages: list[dict], model: str | None = None, temperature: float = 0.7,
             max_tokens: int | None = 4096, json_mode: bool = False, retries: int = 3,
             on_progress=None, role: str | None = None) -> str:
        """Send a chat completion to the local LM Studio; returns the content.

        Each attempt is routed (acquire/release); a failed attempt cools the URL
        down so the next call falls back to the other local instance. JSON
        structure is enforced by prompt instruction + robust extraction in
        chat_json(). ``role`` is accepted for logging/model semantics only.
        """
        max_tokens = _effective_output_cap(max_tokens)
        content = self._chat_local(messages, model, temperature, max_tokens,
                                   retries, on_progress, json_mode=json_mode)
        self.last_provider = "local"
        self.last_resolved_model = model
        return content

    def provider_info(self) -> dict[str, Any]:
        """Which model actually answered the LAST call.

        Lets a prompt log show what really ran instead of the requested role name.
        """
        return {"provider": self.last_provider,
                "model": self.last_resolved_model or self.base_url}

    def _chat_local(self, messages: list[dict], model: str | None, temperature: float,
                    max_tokens: int | None, retries: int, on_progress=None,
                    json_mode: bool = False) -> str:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        last: Exception | None = None
        for attempt in range(retries):
            url = self._acquire()
            try:
                # A local LM Studio request is itself a GPU owner. Route-away
                # requests use the fallback node and must not lock this GPU.
                # The lease also evicts ComfyUI before loading exactly one LLM.
                lease = None
                if url == (self.router.primary if self.router else self.base_url):
                    from ..gpu_manager import ServiceType, get_gpu_manager
                    from ..config import get_config
                    cfg = get_config()
                    if cfg.get("llm", "gpu_offload", True):
                        lease = get_gpu_manager(cfg).acquire(ServiceType.LLM, model=model)
                elif self.router and self.router.fallback:
                    # Routed to fallback because the primary's GPU is busy
                    # rendering. A previous call may have left the primary's LLM
                    # model loaded — evict it so ComfyUI gets the full VRAM.
                    from ..config import get_config
                    cfg = get_config()
                    if cfg.get("llm", "gpu_offload", True):
                        from ..gpu_manager import ServiceType, get_gpu_manager
                        try:
                            gpu = get_gpu_manager(cfg)
                            gpu._unload_llm()
                        except Exception:
                            pass
                if lease is None:
                    if on_progress:
                        return self._chat_stream(url, messages, model, temperature,
                                                 max_tokens, on_progress,
                                                 json_mode=json_mode)
                    data = self._post(url, "/chat/completions", body)
                    return data["choices"][0]["message"]["content"]
                with lease:
                    if on_progress:
                        return self._chat_stream(url, messages, model, temperature,
                                                 max_tokens, on_progress,
                                                 json_mode=json_mode)
                    data = self._post(url, "/chat/completions", body)
                    return data["choices"][0]["message"]["content"]
            except (httpx.HTTPError, KeyError) as exc:
                last = exc
                self._fail(url)
                time.sleep(1.5 * (attempt + 1))
            finally:
                self._release(url)
        raise LMStudioError(f"LM Studio chat failed after {retries} attempts: {last}")

    def chat_json(self, messages: list[dict], model: str | None = None,
                  temperature: float = 0.4, max_tokens: int | None = 4096, retries: int = 3,
                  on_progress=None, role: str | None = None) -> dict:
        """Chat completion whose reply is JSON; return the parsed object.

        The request asks for JSON in the prompt. ``response_format`` is NOT sent:
        not every local LM Studio model/server accepts it and a 400 would fail
        the whole stage. Parsing happens once, here, through ``_extract_json``.
        Retries (fresh generations) when the model truncates or emits unparseable
        JSON - small local models do this on long outputs.
        """
        last: Exception | None = None
        for attempt in range(retries):
            try:
                content = self.chat(messages, model=model, temperature=temperature,
                                    max_tokens=max_tokens, json_mode=True, retries=1,
                                    on_progress=on_progress, role=role)
                return _extract_json(content)
            except (LMStudioError, Exception) as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(1.0 * (attempt + 1))
        raise LMStudioError(f"chat_json failed after {retries} attempts: {last}")

    def health(self) -> bool:
        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{self.base_url}/models")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False


def _extract_json(text: str) -> dict:
    """Best-effort JSON extraction (direct parse, code fences, first balanced object)."""
    import re

    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise LMStudioError(f"No valid JSON in model output: {text[:200]!r}")
