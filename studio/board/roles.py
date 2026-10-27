"""Role, lane and per-role model resolution.

Every agent role names its own lane, model and tool allowlist in
``config/agents.yaml``. Model resolution order:

1. ``agents.roles.<role>.model``
2. ``llm.roles.<role>`` (the existing per-role map in config/llm.yaml)
3. ``llm.model``

So a role can run on a different model without touching code, and a missing
override falls back to the studio default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .cards import LANES

DEFAULT_LANES = {
    "llm": {"capacity": 2},
    "image": {"capacity": 1},
    "video": {"capacity": 1},
    "audio": {"capacity": 1},
    "io": {"capacity": 1},
    "vision": {"capacity": 1},
}


@dataclass(frozen=True)
class Role:
    name: str
    kind: str                 # producer | reviewer
    lane: str
    model: str
    temperature: float = 0.4
    max_steps: int = 10
    max_tokens: int = 4096
    timeout_s: float = 1800.0
    # Model for the work the role performs (the board tool's LLM calls). When
    # empty the worker model is used. Set this when the worker has to be a
    # tool-capable model but the actual generation should run on a different one.
    work_model: str = ""
    # Run this role's tool directly in the daemon (no opencode worker LLM). Use
    # for staged tools that are themselves one deterministic LLM call: the card
    # names its tool, so asking a model to choose and finish only added minutes.
    run_as_exec: bool = False
    # Allow the opencode agent to work in a self-check/retry loop instead of
    # being told to call one tool once and finish.
    iterate: bool = False
    # Keep this agent's model resident while its tool models load/unload.
    pin_model: bool = False
    # opencode agent name (studio / studio-writer / studio-reviewer) and the
    # skill file (under .agents/skills/<name>/SKILL.md) the agent must read.
    agent: str = ""
    skill: str = ""
    tools: tuple[str, ...] = field(default_factory=tuple)
    max_attempts: int = 2
    prompt: str = ""

    @property
    def is_reviewer(self) -> bool:
        return self.kind == "reviewer"

    @property
    def is_exec(self) -> bool:
        return self.kind == "exec"


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def lane_config(cfg) -> dict[str, dict[str, int]]:
    """Lane -> capacity, merged over the defaults."""
    raw = cfg.get("agents", "lanes", {}) or {}
    out = {name: dict(spec) for name, spec in DEFAULT_LANES.items()}
    for name, spec in raw.items():
        if name not in LANES or not isinstance(spec, dict):
            continue
        merged = out.setdefault(name, {"capacity": 1})
        merged.update(spec)
    for spec in out.values():
        spec["capacity"] = max(1, _as_int(spec.get("capacity"), 1))
    return out


def load_roles(cfg) -> dict[str, Role]:
    """All configured roles, with model resolved."""
    raw = cfg.get("agents", "roles", {}) or {}
    llm_roles = cfg.get("llm", "roles", {}) or {}
    default_model = str(cfg.get("llm", "model", "") or "")
    out: dict[str, Role] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        lane = str(spec.get("lane") or "llm")
        if lane not in LANES:
            raise ValueError(f"role {name!r} uses unknown lane {lane!r}")
        kind = str(spec.get("kind") or "producer")
        if kind not in ("producer", "reviewer", "exec"):
            raise ValueError(f"role {name!r} has unknown kind {kind!r}")
        model = str(spec.get("model") or llm_roles.get(name) or default_model or "")
        tools = spec.get("tools") or []
        if isinstance(tools, str):
            tools = [tools]
        out[name] = Role(
            name=name,
            kind=kind,
            lane=lane,
            model=model,
            temperature=_as_float(spec.get("temperature"), 0.4),
            max_steps=_as_int(spec.get("max_steps"), 10),
            max_tokens=_as_int(spec.get("max_tokens"), 4096),
            timeout_s=_as_float(spec.get("timeout_s"), 1800.0),
            work_model=str(spec.get("work_model") or ""),
            run_as_exec=bool(spec.get("exec", False)),
            iterate=bool(spec.get("iterate", False)),
            pin_model=bool(spec.get("pin_model", False)),
            agent=str(spec.get("agent") or ""),
            skill=str(spec.get("skill") or ""),
            tools=tuple(str(t) for t in tools),
            max_attempts=_as_int(spec.get("max_attempts"), 2),
            prompt=str(spec.get("prompt") or ""),
        )
    return out


def get_role(cfg, name: str) -> Role:
    roles = load_roles(cfg)
    if name not in roles:
        raise KeyError(f"unknown agent role {name!r} (configured: {', '.join(sorted(roles)) or 'none'})")
    return roles[name]


def role_for_phase(card: dict[str, Any], phase: str) -> str:
    """Which role owns this phase of a card."""
    if phase == "review":
        reviewer = str(card.get("reviewer") or "")
        if not reviewer:
            raise KeyError(f"card {card.get('id')!r} has no reviewer")
        return reviewer
    return str(card.get("role") or "")
