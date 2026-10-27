"""Bounded tool-using agent loop over the studio's OpenAI-compatible LLM.

The model is asked for one JSON object per turn:

    {"action": "<tool>", "args": {...}}      -> run a tool, feed back its result
    {"final": {...}}                          -> stop

This protocol works with local models that do not implement native tool calls.
The loop is deliberately small: a step cap, a result-size cap, a trace, and no
ability to move its own card (the daemon applies the final verdict).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import get_config
from . import tools as tools_mod

MAX_RESULT_CHARS = 8000
MAX_MESSAGES = 18


@dataclass
class AgentResult:
    status: str = "blocked"       # verified | retake | blocked (producer)
    verdict: str = ""             # pass | fail (reviewer)
    summary: str = ""
    notes: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("verified", "pass") or self.verdict == "pass"


def parse_action(text: str) -> dict[str, Any]:
    """Extract the first JSON object that carries an action or a final."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty model reply")
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict) and ("action" in obj or "final" in obj):
            return obj
    raise ValueError("reply contains no action/final JSON object")


def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n...[truncated {len(text)-limit} chars]"


def _json_text(value: Any) -> str:
    try:
        return _clip(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return _clip(str(value))


class Agent:
    def __init__(self, *, role, card: dict[str, Any], store, phase: str,
                 run_id: str, llm, cfg=None):
        self.role = role
        self.card = card
        self.store = store
        self.phase = phase
        self.run_id = run_id
        self.llm = llm
        self.cfg = cfg or get_config()
        self.messages: list[dict[str, str]] = []
        self.trace: list[dict[str, Any]] = []

    # ---- prompt ----
    def system_prompt(self) -> str:
        job = self.role.prompt or (
            "Do this card's work with the tools, attach proof, and finish.")
        if self.phase == "review":
            finish = ('{"final":{"verdict":"pass"|"fail","notes":"one or two '
                      'sentences on the decisive issues"}}')
        else:
            finish = ('{"final":{"status":"verified"|"retake"|"blocked",'
                      '"summary":"what you did","notes":"what the next phase must know"}}')
        card = {k: self.card.get(k) for k in
                ("id", "kind", "title", "show_id", "episode", "state", "payload")}
        example = ('{"final":{"verdict":"pass","notes":"identity and action hold"}}'
                   if self.phase == "review"
                   else '{"final":{"status":"verified","summary":"wrote the outline"}}')
        return (
            f"You are the {self.role.name} agent on an anime production board.\n"
            f"You work exactly one card. Phase: {self.phase}. Lane: {self.role.lane}.\n"
            f"{job}\n\n"
            f"CARD:\n{json.dumps(card, ensure_ascii=False)}\n\n"
            "Act only through tools. Reply with ONE JSON object per turn:\n"
            '{"action":"<tool>","args":{...}}\n'
            'Example: {"action":"read_card","args":{}}\n'
            f"Finish with:\n{finish}\n"
            f"Example: {example}\n\n"
            f"TOOLS:\n{tools_mod.manifest(self.role)}\n\n"
            "Work from the card outward: read what you need, do the work, attach "
            "the files it produced, then finish. A tool error is information - "
            "read it and try a different call. The board moves the card; you do "
            "not accept your own work."
        )

    # ---- run ----
    def run(self) -> AgentResult:
        self.messages = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": "Begin. Call your first tool or finish."},
        ]
        parse_errors = 0
        for step in range(1, max(1, int(self.role.max_steps)) + 1):
            try:
                reply = self.llm.chat(
                    self.messages, model=self.role.model,
                    temperature=self.role.temperature,
                    max_tokens=self.role.max_tokens,
                    role=self.role.name,
                )
            except Exception as exc:  # transport/model failure
                return AgentResult(status="blocked", error=f"model call failed: {exc}",
                                   trace=self.trace, steps=step)
            self.messages.append({"role": "assistant", "content": reply})
            try:
                action = parse_action(reply)
            except ValueError as exc:
                parse_errors += 1
                self._trace(step, "parse", {}, False, str(exc))
                self.messages.append({
                    "role": "user",
                    "content": ("Reply with exactly one JSON object: "
                                '{"action":"<tool>","args":{...}} or '
                                '{"final":{...}}.' )})
                if parse_errors >= 3:
                    return AgentResult(status="blocked", error=str(exc),
                                       trace=self.trace, steps=step)
                continue
            parse_errors = 0
            if "final" in action:
                return self._finish(action["final"], step)
            name = str(action.get("action") or "").strip()
            args = action.get("args") or {}
            try:
                ctx = tools_mod.ToolContext(
                    store=self.store, card=self.card, role=self.role,
                    phase=self.phase, run_id=self.run_id, llm=self.llm,
                    cfg=self.cfg)
                result = tools_mod.call(ctx, name, args)
                ok = True
                err = ""
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
                ok = False
                err = f"{type(exc).__name__}: {exc}"
            self._trace(step, name, args, ok, err, result)
            self.messages.append({
                "role": "user",
                "content": f"TOOL RESULT {name}:\n{_json_text(result)}",
            })
            self._trim()
        return AgentResult(status="blocked", error="step budget exhausted",
                           trace=self.trace, steps=self.role.max_steps)

    def _finish(self, final: Any, step: int) -> AgentResult:
        if not isinstance(final, dict):
            return AgentResult(status="blocked", error="final must be an object",
                               trace=self.trace, steps=step)
        notes = str(final.get("notes") or "").strip()
        if self.phase == "review":
            verdict = str(final.get("verdict") or "").strip().lower()
            if verdict not in ("pass", "fail"):
                return AgentResult(status="blocked", error="reviewer verdict must be pass or fail",
                                   trace=self.trace, steps=step)
            self._trace(step, "final", final, True, "")
            return AgentResult(status="pass" if verdict == "pass" else "retake",
                               verdict=verdict, notes=notes,
                               trace=self.trace, steps=step)
        status = str(final.get("status") or "").strip().lower()
        if status not in ("verified", "retake", "blocked"):
            return AgentResult(status="blocked", error="producer status must be verified/retake/blocked",
                               trace=self.trace, steps=step)
        self._trace(step, "final", final, True, "")
        return AgentResult(status=status,
                           summary=str(final.get("summary") or "").strip(),
                           notes=notes, trace=self.trace, steps=step)

    def _trace(self, step: int, action: str, args: dict[str, Any], ok: bool,
               error: str = "", result: Any = None) -> None:
        self.trace.append({
            "step": step, "action": action, "args": args, "ok": ok,
            "error": error, "result": _clip(_json_text(result), 2000) if result is not None else "",
            "ts": time.time(),
        })

    def _trim(self) -> None:
        if len(self.messages) <= MAX_MESSAGES:
            return
        # keep the system + first user, then the most recent turns
        self.messages = self.messages[:2] + self.messages[-(MAX_MESSAGES - 2):]


def write_trace(cfg, run_id: str, prompt: str, result: AgentResult) -> Path:
    """Persist the prompt + tool trace for a run."""
    out = Path(cfg.root) / "runs" / "agents" / run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.md").write_text(prompt, encoding="utf-8")
    (out / "trace.json").write_text(
        json.dumps({"status": result.status, "verdict": result.verdict,
                    "summary": result.summary, "notes": result.notes,
                    "error": result.error, "steps": result.steps,
                    "trace": result.trace}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    return out
