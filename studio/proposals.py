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
    # No wall-clock limit on generation (the client bounds only the connection,
    # not the response): a 27B can legitimately take minutes, and a read timeout
    # used to fire after LM Studio had already answered.
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"))
    profile = cfg.get("show_profile") or {}
    maturity = profile.get("maturity") or ""
    baseline = profile.get("baseline", "")
    roles = cfg.get("llm", "roles", {})
    model = roles.get("concept") or cfg.get("llm", "model")
    system = prompts.showrunner_system(profile, maturity, baseline)
    user = prompts.proposals_prompt(guidance, profile, n)
    # No client-side token cap: the server/model decides (llm.max_output_tokens=0
    # omits the field). A cap only truncates a thinking model's JSON.
    data = llm.chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model, temperature=0.9, max_tokens=None, retries=1, role="concept",
    )
    proposals = data.get("proposals") or []
    if maturity:
        for p in proposals:
            p["maturity"] = maturity  # enforce the approved level exactly
    return proposals
