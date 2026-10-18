"""Input/output capture for every LLM step: the director sees everything.

Every creative call in the episode path logs its filled prompt (system + user
messages, model, temperature) to ``runs/EP##/prompts/<step>.json`` BEFORE
calling the model, so a debugging user can review the exact INPUT that
produced any output. Outputs already persist next to them (prose txt,
dialogue txt, scene checkpoints, scripts, review notes files).

Usage at a call site (mechanical, no behavior change — kwargs pass through)::

    out = promptlog.logged_chat_json(
        llm, show, episode, "blocking-s01",
        [{"role": "system", ...}, {"role": "user", ...}],
        model=model, temperature=0.8, ...)

Logging never raises: a failure here must never break generation.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


def _prompts_dir(show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / "prompts"


def _safe_step(step: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (step or "step").lower()).strip("-")
    return (slug or "step")[:80]


def log_prompt(show, episode: int, step: str,
               messages: list[dict[str, Any]],
               model: str | None = None,
               temperature: float | None = None,
               extra: dict[str, Any] | None = None) -> Path | None:
    """Persist one LLM call's filled prompt. Returns the file path (or None)."""
    if show is None:
        return None
    try:
        d = _prompts_dir(show, episode)
        d.mkdir(parents=True, exist_ok=True)
        system_parts: list[str] = []
        user_parts: list[str] = []
        for m in messages or []:
            role = (m.get("role") or "").lower()
            content = m.get("content", "")
            if not isinstance(content, str):
                try:
                    content = json.dumps(content, ensure_ascii=False)
                except Exception:
                    content = str(content)
            if role == "system":
                system_parts.append(content)
            else:
                user_parts.append(f"[{role or 'user'}]\n{content}" if role != "user" else content)
        payload = {"step": step, "episode": episode, "model": model,
                   "temperature": temperature, "ts": time.time(),
                   "system": "\n\n".join(system_parts),
                   "user": "\n\n".join(user_parts)}
        if extra:
            payload["extra"] = extra
        path = d / f"{_safe_step(step)}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).warning("prompt log failed for step %s", step,
                                             exc_info=True)
        return None


def _annotate_provider(path: Path | None, llm) -> None:
    """Patch the just-written log with the provider/model that ACTUALLY answered.

    ``log_prompt`` runs BEFORE the call and only knows the requested local role
    model, which is misleading when the call went remote (or fell back local).
    Afterwards the client exposes ``provider_info()``; merge it in so the prompt
    file records the truth. Never raises.
    """
    if path is None:
        return
    try:
        info = getattr(llm, "provider_info", None)
        detail = info() if callable(info) else None
        if not isinstance(detail, dict):
            return
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["provider"] = detail.get("provider")
        data["resolved_model"] = detail.get("model")
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    except Exception:
        import logging as _logging
        _logging.getLogger(__name__).warning("prompt provider annotate failed",
                                             exc_info=True)


def logged_chat(llm, show, episode: int, step: str,
                messages: list[dict[str, Any]], **kwargs) -> str:
    """llm.chat + prompt capture. kwargs (model, temperature, role, …) pass through."""
    path = log_prompt(show, episode, step, messages,
                      model=kwargs.get("model"), temperature=kwargs.get("temperature"))
    try:
        return llm.chat(messages, **kwargs)
    finally:
        _annotate_provider(path, llm)


def logged_chat_json(llm, show, episode: int, step: str,
                     messages: list[dict[str, Any]], **kwargs) -> dict:
    """llm.chat_json + prompt capture. kwargs pass through unchanged."""
    path = log_prompt(show, episode, step, messages,
                      model=kwargs.get("model"), temperature=kwargs.get("temperature"))
    try:
        return llm.chat_json(messages, **kwargs)
    finally:
        _annotate_provider(path, llm)
