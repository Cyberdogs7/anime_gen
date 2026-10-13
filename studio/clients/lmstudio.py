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

import fnmatch
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

# Stable per-process session id for GO's x-opencode-session routing header
# (required: requests without it are rejected with MissingSessionID). Shared
# across shows in one process; pass session_id= to scope it per conversation
# for better prompt-cache routing.
_ZEN_SESSION = uuid.uuid4().hex

# One-shot flag so a missing-key process warns once instead of per call.
_WARNED_NO_KEY = False


class LMStudioError(RuntimeError):
    pass


class RefusalError(RuntimeError):
    """The provider refused the prompt on content-policy grounds.

    Raised only by the remote (Zen) path. The caller retries the same prompt
    on the local uncensored model, which is exactly the case local exists for.
    """


# Classic refusal openers, checked against the head of the output only. Verb
# lists are deliberately narrow ("I can't believe…" dialogue must NOT match).
_REFUSAL_RES = [
    r"i['’]m sorry,? (but )?i can['’]t (help|comply|assist|do that|write|create|generate|continue)",
    r"i can['’]t (help|comply|assist) with (that|this|these|your)",
    r"i['’]m (unable|not able) to (help|comply|assist|fulfill|complete)",
    r"i (must|have to) decline",
    r"unable to comply",
    r"against (my|our) (content )?policy",
    r"violates? (my|our|the) (content )?(policy|guidelines|terms)",
    r"blocked (by|due to)(.{0,40})?(content|policy|filter|moderation)",
    r"content (was |has been )?blocked",
    r"i don['’]t feel comfortable",
    r"cannot (provide|generate|create|write).{0,60}(explicit|sexual|erotic|pornographic|nsfw)",
]

_REFUSAL_HEAD_CHARS = 800


def is_refusal(text: str) -> bool:
    """True when the model output opens like a content-policy refusal."""
    head = (text or "").strip()[:_REFUSAL_HEAD_CHARS].lower()
    if not head:
        return False
    return any(re.search(p, head) for p in _REFUSAL_RES)


def _has_images(messages: list[dict]) -> bool:
    """True when any message carries image or video parts (a vision call)."""
    for m in messages or []:
        content = (m or {}).get("content")
        if isinstance(content, list) and any(
                isinstance(p, dict) and p.get("type") in ("image_url", "image", "video_url")
                for p in content):
            return True
    return False


def _has_video(messages: list[dict]) -> bool:
    """True when any message carries a vLLM-style video_url part."""
    for m in messages or []:
        content = (m or {}).get("content")
        if isinstance(content, list) and any(
                isinstance(p, dict) and p.get("type") == "video_url"
                for p in content):
            return True
    return False


def _to_responses_input(messages: list[dict]) -> list[dict]:
    """Convert chat messages to OpenAI Responses API ``input`` items.

    text -> input_text, image_url/image -> input_image, vLLM video_url ->
    input_video (passed natively — responses-capable models take video).
    Plain-string content becomes a single input_text part. Unknown roles are
    sent as user (our pipeline only emits system/user).
    """
    items: list[dict] = []
    for m in messages or []:
        role = (m or {}).get("role", "user")
        if role not in ("system", "user", "assistant", "developer"):
            role = "user"
        content = (m or {}).get("content", "")
        if isinstance(content, str):
            parts = [{"type": "input_text", "text": content}]
        else:
            parts = []
            for p in content or []:
                if not isinstance(p, dict):
                    continue
                t = p.get("type")
                if t == "text":
                    parts.append({"type": "input_text", "text": p.get("text", "")})
                elif t in ("image_url", "image"):
                    url = (p.get("image_url") or {}).get("url", "") if t == "image_url" else p.get("url", "")
                    parts.append({"type": "input_image", "image_url": url})
                elif t == "video_url":
                    url = (p.get("video_url") or {}).get("url", "")
                    parts.append({"type": "input_video", "video_url": url})
        items.append({"role": role, "content": parts})
    return items


def _extract_responses_text(data: dict) -> str:
    """Pull the assistant text out of a Responses API result object."""
    texts: list[str] = []
    for item in (data or {}).get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for c in item.get("content", []) or []:
            if isinstance(c, dict) and c.get("type") == "output_text" and c.get("text"):
                texts.append(c["text"])
    text = "".join(texts).strip()
    if not text:
        raise LMStudioError(f"empty text in responses output: {str(data)[:200]!r}")
    return text


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


def _responses_incomplete(data: dict) -> bool:
    """True when a Responses result was cut off by the output-token budget.

    Reasoning models (e.g. Muse Spark) spend tokens on hidden reasoning before
    any message text, so too small a ``max_output_tokens`` returns
    ``status: "incomplete"`` with NO text. That is not a failure - the fix is to
    retry with a bigger budget, not to silently fall back to the local model.
    """
    if not isinstance(data, dict):
        return False
    if str(data.get("status") or "").lower() == "incomplete":
        return True
    det = data.get("incomplete_details")
    return isinstance(det, dict) and "max_output" in str(det.get("reason") or "").lower()


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
                 router: LLMRouter | None = None,
                 api_key: str | None = None, remote_url: str | None = None,
                 remote_model: str | None = None,
                 remote_model_map: dict[str, str] | None = None,
                 remote_first: bool | None = None, remote_timeout: float | None = None,
                 session_id: str | None = None):
        """OpenAI-compatible chat client with optional remote-first routing.

        All existing call sites keep working unchanged: when the ``llm.zen``
        config section is enabled AND an API key is available (read from the
        opencode auth.json — see ``config.resolve_zen_api_key``; the environment
        variable is never consulted), text prompts go to OpenCode Zen first and
        fall back to the local instance on transport errors AND on
        content-policy refusals (the explicit scenes remote models won't
        write). Without a key the client is exactly the old local-only client.

        Explicit constructor args override the ``llm.zen`` config values (used
        by tests and one-off scripts). ``prefer_local=True`` on chat/chat_json
        forces a call onto the local instance.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.router = router if router is not None else _matches(shared_router(), base_url)
        self._zen_override = {
            "api_key": api_key, "url": remote_url, "model": remote_model,
            "model_map": remote_model_map, "enabled": remote_first,
            "timeout": remote_timeout,
        }
        self._session_id = session_id or _ZEN_SESSION
        self._zen: dict[str, Any] | bool | None = None  # None=unresolved
        self.last_provider = "local"

    def _acquire(self) -> str:
        return self.router.acquire() if self.router else self.base_url

    def _release(self, url: str) -> None:
        if self.router:
            self.router.release(url)

    def _fail(self, url: str) -> None:
        if self.router:
            self.router.note_failure(url)

    def _zen_settings(self) -> dict[str, Any] | None:
        """Resolved Zen remote settings, or None when remote is unavailable.

        Result is cached: explicit constructor args win, otherwise the
        ``llm.zen`` config section. Remote stays OFF unless it is enabled AND
        an API key resolves — so a missing key silently keeps today's
        local-only behavior.
        """
        if self._zen is not None:
            return self._zen or None
        try:
            from ..config import get_config, resolve_zen_api_key
            cfg = get_config()
            z = cfg.get("llm", "zen", {}) or {}
        except Exception:
            cfg, z = None, {}

            def resolve_zen_api_key(_cfg=None, override=None):
                return override or ""
        ov = self._zen_override
        enabled = ov["enabled"] if ov["enabled"] is not None else bool(z.get("enabled", False))
        url = (ov["url"] or z.get("base_url") or "").rstrip("/")
        # The key comes from the opencode auth.json (or an explicit override);
        # the environment is never consulted.
        api_key = ov["api_key"] or resolve_zen_api_key(cfg, None)
        if not (enabled and url and api_key):
            global _WARNED_NO_KEY
            if enabled and url and not api_key and not _WARNED_NO_KEY:
                _WARNED_NO_KEY = True
                log.warning(
                    "llm.zen is enabled but no API key was found in the opencode "
                    "auth.json — running LOCAL-ONLY. Sign in with the opencode "
                    "CLI (`opencode auth login`).")
            self._zen = False
            return None
        model_map = dict(z.get("model_map", {}) or {})
        if ov["model_map"]:
            model_map.update(ov["model_map"])
        roles = dict(z.get("roles", {}) or {})
        responses_models = list(z.get("responses_models", []) or [
            "muse-spark-*", "gpt-*", "grok-*"])
        vision = {"enabled": True, "model": "muse-spark-1.3-contributor",
                  "frames_per_video": 6, "frame_dim": 512}
        vision.update(z.get("vision", {}) or {})
        try:
            local_roles = dict(cfg.get("llm", "roles", {}) or {}) if cfg else {}
        except Exception:
            local_roles = {}
        self._zen = {
            "url": url,
            "api_key": api_key,
            "model": ov["model"] or z.get("model", "muse-spark-1.3-contributor"),
            "model_map": model_map,
            "roles": roles,
            "local_roles": local_roles,
            "responses_models": responses_models,
            "vision": vision,
            "timeout": ov["timeout"] or z.get("timeout", 0) or self.timeout,
        }
        return self._zen

    @staticmethod
    def _uses_responses_api(zen: dict[str, Any], remote_model: str) -> bool:
        """True when the remote model lives on the /responses endpoint.

        The docs route models per-endpoint (Spark/GPT/Grok -> responses,
        Kimi/DeepSeek/GLM -> chat/completions, Claude/Qwen -> messages which
        this client does not speak). Patterns come from
        ``zen.responses_models`` so new models don't need code changes.
        """
        return any(fnmatch.fnmatch(remote_model or "", pat)
                   for pat in zen.get("responses_models", []) or [])

    def _remote_model_for(self, zen: dict[str, Any], model: str | None,
                          role: str | None = None) -> str:
        """Pick the remote model for a call.

        Precedence: explicit ``role=`` kwarg -> ``zen.roles[role]``; else
        reverse lookup (the unique configured role whose LOCAL model matches
        the requested one); else ``zen.model_map[local model]``; else the
        ``zen.model`` default. Callers never change: role resolution rides on
        the model names they already pass.
        """
        if role and role in zen["roles"]:
            return zen["roles"][role]
        if model:
            cands = [r for r, lm in zen["local_roles"].items()
                     if lm == model and r in zen["roles"]]
            if len(cands) == 1:
                return zen["roles"][cands[0]]
            if model in zen["model_map"]:
                return zen["model_map"][model]
        return zen["model"]

    def _video_part_to_frame_parts(self, part: dict[str, Any],
                                    vcfg: dict[str, Any]) -> list[dict] | None:
        """Convert a vLLM video_url part into sampled image_url frame parts.

        Returns None when the clip can't be decoded — the caller then runs the
        whole message locally instead of sending a crippled prompt remote.
        """
        import base64 as _b64
        import tempfile as _tf
        url = ((part.get("video_url") or {}).get("url", "")) if isinstance(part, dict) else ""
        if "," in url:
            b64 = url.split(",", 1)[1]
        else:
            return None  # remote http(s) fetch of video is out of scope
        try:
            raw = _b64.b64decode(b64)
        except Exception:
            return None
        tmp = None
        try:
            with _tf.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                f.write(raw)
                tmp = Path(f.name)
            # Lazy: vcheck owns frame sampling; the client only adapts formats.
            from ..vcheck import sample_frames
            frames = sample_frames(
                tmp, n=int(vcfg.get("frames_per_video", 6) or 6),
                out_dim=int(vcfg.get("frame_dim", 512) or 512))
            if not frames:
                return None
            parts = []
            for i, fr in enumerate(frames):
                try:
                    uri = ("data:image/jpeg;base64,"
                           + _b64.b64encode(Path(fr).read_bytes()).decode())
                except OSError:
                    continue
                if i == 0:
                    parts.append({"type": "text",
                                  "text": "Clip frames (chronological sample):"})
                parts.append({"type": "image_url", "image_url": {"url": uri}})
            return parts or None
        except Exception as exc:
            log.warning("remote vision: frame sampling failed (%r); staying local", exc)
            return None
        finally:
            try:
                if tmp is not None:
                    os.unlink(tmp)
            except OSError:
                pass

    def _remote_vision_messages(self, zen: dict[str, Any],
                                messages: list[dict]) -> list[dict] | None:
        """Rewrite messages for a remote OpenAI-style vision model.

        image_url parts pass through untouched; vLLM video_url parts become
        sampled frame images. None means "can't represent remotely" -> local.
        """
        out: list[dict] = []
        for m in messages or []:
            content = (m or {}).get("content")
            if not isinstance(content, list):
                out.append(m)
                continue
            parts: list[dict] = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "video_url":
                    frames = self._video_part_to_frame_parts(p, zen["vision"])
                    if frames is None:
                        return None
                    parts.extend(frames)
                else:
                    parts.append(p)
            out.append({**m, "content": parts})
        return out

    def _post(self, url: str, path: str, json_body: dict,
              headers: dict | None = None, timeout: float | None = None) -> dict:
        with httpx.Client(timeout=self.timeout if timeout is None else timeout) as client:
            resp = client.post(f"{url}{path}", json=json_body, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def _chat_stream(self, url: str, messages: list[dict], model: str, temperature: float,
                     max_tokens: int | None, on_progress,
                     headers: dict | None = None, timeout: float | None = None) -> str:
        """Stream a chat completion; call on_progress(tokens, partial_text) per chunk."""
        body = {"model": model, "messages": messages, "temperature": temperature,
                "stream": True}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        parts: list[str] = []
        n = 0
        with httpx.Client(timeout=self.timeout if timeout is None else timeout) as client:
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

    def _responses_stream(self, url: str, body: dict, on_progress,
                            headers: dict, timeout: float) -> str:
        """Stream a Responses API call, collecting output_text deltas."""
        parts: list[str] = []
        n = 0
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", f"{url}/responses", json={**body, "stream": True},
                               headers=headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload).get("delta", "")
                    except Exception:
                        continue
                    if delta:
                        parts.append(delta)
                        n += 1
                        on_progress(n, "".join(parts))
        return "".join(parts)

    def _chat_zen(self, zen: dict[str, Any], messages: list[dict], model: str | None,
                  temperature: float, max_tokens: int | None, on_progress=None,
                  role: str | None = None, model_override: str | None = None,
                  api: str | None = None) -> str:
        """One remote completion (up to 2 transport retries). Never local.

        ``api`` selects the transport ("chat" or "responses"); None
        auto-selects per model — Spark/GPT/Grok live on /responses, the rest
        on /chat/completions. Raises RefusalError when the remote model
        answers with a content-policy refusal instead of content — the caller
        retries the prompt locally.
        """
        remote_model = model_override or self._remote_model_for(zen, model, role)
        api = api or ("responses" if self._uses_responses_api(zen, remote_model) else "chat")
        # Identify honestly: GO monitors for abuse and asks clients to send
        # their own user agent rather than a generic HTTP-library name.
        headers = {"Authorization": f"Bearer {zen['api_key']}",
                   "User-Agent": "anime-studio/1.0",
                   "x-opencode-session": self._session_id}
        timeout = zen["timeout"]
        last: Exception | None = None
        mt = max_tokens          # local, mutable: grows when a reasoning model truncates
        bumps = 0                # budget-bump retries (NOT counted as transport retries)
        attempts = 0
        while attempts < 2:
            attempts += 1
            try:
                if api == "responses":
                    body = {"model": remote_model,
                            "input": _to_responses_input(messages),
                            "temperature": temperature}
                    if mt is not None:
                        body["max_output_tokens"] = mt
                    data = None
                    if on_progress:
                        content = self._responses_stream(
                            zen["url"], body, on_progress, headers, timeout)
                        if not content.strip():
                            data = self._post(zen["url"], "/responses", body,
                                              headers=headers, timeout=timeout)
                    else:
                        data = self._post(zen["url"], "/responses", body,
                                          headers=headers, timeout=timeout)
                    if data is not None:
                        try:
                            content = _extract_responses_text(data)
                        except LMStudioError:
                            # Reasoning models need headroom: an incomplete result
                            # with no text means the budget was too small, not a
                            # broken remote. Retry with a bigger budget.
                            if (_responses_incomplete(data) and bumps < 2
                                    and (mt is None or mt < 32000)):
                                bumps += 1
                                mt = min((mt or 2048) * 4, 32000)
                                attempts -= 1
                                continue
                            raise
                elif on_progress:
                    content = self._chat_stream(
                        zen["url"], messages, remote_model, temperature,
                        mt, on_progress, headers=headers, timeout=timeout)
                else:
                    req = {"model": remote_model, "messages": messages,
                           "temperature": temperature}
                    if mt is not None:
                        req["max_tokens"] = mt
                    data = self._post(zen["url"], "/chat/completions", req,
                                      headers=headers, timeout=timeout)
                    content = data["choices"][0]["message"]["content"]
                if is_refusal(content):
                    raise RefusalError(
                        f"zen model {remote_model} refused the prompt: {content[:200]!r}")
                return content
            except RefusalError:
                raise
            except (httpx.HTTPError, KeyError, LMStudioError) as exc:
                last = exc
                time.sleep(1.5 * attempts)
        raise LMStudioError(f"zen chat failed after 2 attempts: {last}")

    def chat(self, messages: list[dict], model: str | None = None, temperature: float = 0.7,
             max_tokens: int | None = 4096, json_mode: bool = False, retries: int = 3,
             on_progress=None, prefer_local: bool = False,
             role: str | None = None) -> str:
        """Send a chat completion; returns the message content string.

        Remote-first when Zen is configured: prompts go to OpenCode Zen
        and fall back to the local instance on transport errors AND on
        content-policy refusals (explicit scenes remote models won't write).
        ``role=`` selects the remote model via ``zen.roles`` (e.g.
        role="script"); without it the client reverse-matches the requested
        local model name to a uniquely configured role. Vision calls go to the
        ``zen.vision`` model: responses-style models (Spark) take image + video
        natively, chat-style models get video resampled to frames first.
        Undecodable clips, disabled vision, and ``prefer_local=True`` stay
        local. Each local attempt is routed (acquire/release); a failed
        attempt cools the URL down so the next call falls back to the other
        instance. JSON structure is enforced by prompt instruction + robust
        extraction in chat_json().
        """
        # A caller's small budget must never truncate the model: floor it.
        max_tokens = _effective_output_cap(max_tokens)
        zen = self._zen_settings()
        if zen and not prefer_local:
            if _has_images(messages):
                if zen["vision"].get("enabled"):
                    vmodel = zen["vision"].get("model")
                    if self._uses_responses_api(zen, vmodel):
                        send, api = messages, "responses"  # native image+video
                    else:
                        send = self._remote_vision_messages(zen, messages)
                        api = "chat"
                    if send is not None:
                        try:
                            content = self._chat_zen(
                                zen, send, model, temperature, max_tokens,
                                on_progress, model_override=vmodel, api=api)
                            self.last_provider = "zen"
                            return content
                        except RefusalError as exc:
                            log.info("zen vision refused prompt (%s); retrying locally", exc)
                        except (LMStudioError, httpx.HTTPError) as exc:
                            log.warning("zen vision request failed (%r); retrying locally", exc)
                    # else: unrepresentable remotely (undecodable clip) -> local
                # else: remote vision disabled -> local
            else:
                try:
                    content = self._chat_zen(zen, messages, model, temperature,
                                             max_tokens, on_progress, role=role)
                    self.last_provider = "zen"
                    return content
                except RefusalError as exc:
                    log.info("zen refused prompt (%s); retrying locally", exc)
                except (LMStudioError, httpx.HTTPError) as exc:
                    log.warning("zen request failed (%r); retrying locally", exc)
        content = self._chat_local(messages, model, temperature, max_tokens,
                                   retries, on_progress)
        self.last_provider = "local"
        return content

    def _chat_local(self, messages: list[dict], model: str | None, temperature: float,
                    max_tokens: int | None, retries: int, on_progress=None) -> str:
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
                                                 max_tokens, on_progress)
                    data = self._post(url, "/chat/completions", body)
                    return data["choices"][0]["message"]["content"]
                with lease:
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
                  temperature: float = 0.4, max_tokens: int | None = 4096, retries: int = 3,
                  on_progress=None, prefer_local: bool = False,
                  role: str | None = None) -> dict:
        """Chat completion with a JSON-object response, parsed.

        Retries (fresh generations) when the model truncates or emits unparseable
        JSON - small local models do this on long outputs.
        """
        last: Exception | None = None
        for attempt in range(retries):
            try:
                content = self.chat(messages, model=model, temperature=temperature,
                                    max_tokens=max_tokens, json_mode=True, retries=1,
                                    on_progress=on_progress, prefer_local=prefer_local,
                                    role=role)
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
