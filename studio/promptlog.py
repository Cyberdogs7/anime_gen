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


def logged_chat(llm, show, episode: int, step: str,
                messages: list[dict[str, Any]], **kwargs) -> str:
    """llm.chat + prompt capture. kwargs (model, temperature, role, …) pass through."""
    log_prompt(show, episode, step, messages,
               model=kwargs.get("model"), temperature=kwargs.get("temperature"))
    return llm.chat(messages, **kwargs)


def logged_chat_json(llm, show, episode: int, step: str,
                     messages: list[dict[str, Any]], **kwargs) -> dict:
    """llm.chat_json + prompt capture. kwargs pass through unchanged."""
    log_prompt(show, episode, step, messages,
               model=kwargs.get("model"), temperature=kwargs.get("temperature"))
    return llm.chat_json(messages, **kwargs)
