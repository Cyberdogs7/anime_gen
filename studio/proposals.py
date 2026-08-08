"""Concept proposals from the showrunner (dashboard 'New show' flow).

The owner supplies prompt guidance only; the showrunner pitches N distinct
series concepts, and the owner picks one to turn into a show.
"""
from __future__ import annotations

from typing import Any

from . import prompts
from .clients import LMStudioClient
from .config import get_config


def propose_concepts(guidance: str = "", n: int = 3, cfg=None, llm=None) -> list[dict[str, Any]]:
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"))
    profile = cfg.get("show_profile") or {}
    maturity = profile.get("maturity", "mature")
    baseline = profile.get("baseline", "ranma-1-2")
    roles = cfg.get("llm", "roles", {})
    model = roles.get("concept") or cfg.get("llm", "model")
    system = prompts.showrunner_system(profile, maturity, baseline)
    user = prompts.proposals_prompt(guidance, profile, n)
    data = llm.chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model, temperature=0.9, max_tokens=8192,
    )
    proposals = data.get("proposals") or []
    for p in proposals:
        p["maturity"] = maturity  # enforce the approved level exactly
    return proposals
