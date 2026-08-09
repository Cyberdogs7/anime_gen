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
import threading
import time
from typing import Any

import httpx


class LMStudioError(RuntimeError):
    pass


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
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.router = router if router is not None else _matches(shared_router(), base_url)

    def _acquire(self) -> str:
        return self.router.acquire() if self.router else self.base_url

    def _release(self, url: str) -> None:
        if self.router:
            self.router.release(url)

    def _fail(self, url: str) -> None:
        if self.router:
            self.router.note_failure(url)

    def _post(self, url: str, path: str, json_body: dict) -> dict:
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{url}{path}", json=json_body)
            resp.raise_for_status()
            return resp.json()

    def _chat_stream(self, url: str, messages: list[dict], model: str, temperature: float,
                     max_tokens: int, on_progress) -> str:
        """Stream a chat completion; call on_progress(tokens, partial_text) per chunk."""
        body = {
            "model": model, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "stream": True,
        }
        parts: list[str] = []
        n = 0
        with httpx.Client(timeout=self.timeout) as client:
            with client.stream("POST", f"{url}/chat/completions", json=body) as resp:
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
             max_tokens: int = 4096, json_mode: bool = False, retries: int = 3,
             on_progress=None) -> str:
        """Send a chat completion; returns the message content string.

        Each attempt is routed (acquire/release); a failed attempt cools the URL
        down so the next call falls back to the other instance. JSON structure is
        enforced by prompt instruction + robust extraction in chat_json().
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        last: Exception | None = None
        for attempt in range(retries):
            url = self._acquire()
            try:
                if on_progress:
                    return self._chat_stream(url, messages, model, temperature,
                                             max_tokens, on_progress)
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
                  temperature: float = 0.4, max_tokens: int = 4096, retries: int = 3,
                  on_progress=None) -> dict:
        """Chat completion with a JSON-object response, parsed.

        Retries (fresh generations) when the model truncates or emits unparseable
        JSON - small local models do this on long outputs.
        """
        last: Exception | None = None
        for attempt in range(retries):
            try:
                content = self.chat(messages, model=model, temperature=temperature,
                                    max_tokens=max_tokens, json_mode=True, retries=1,
                                    on_progress=on_progress)
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
