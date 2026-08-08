"""Stage 2a - writers'-room script reviewers (DESIGN.md §9 Stage 2a).

Runs the slop / continuity / fan_service reviewers over a script JSON, each
writing its own separate notes file, and returns per-reviewer verdicts. The
revision loop (draft -> notes -> revise -> re-review) is orchestrated by the
stage consumer; this module is the single review pass.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import prompts
from .config import get_config
from .show import Show


def reviewer_models(cfg) -> dict[str, str]:
    roles = cfg.get("llm", "roles", {})
    return {
        "slop": roles.get("slop_reviewer") or cfg.get("llm", "model"),
        "continuity": roles.get("continuity_reviewer") or cfg.get("llm", "model"),
        "fan_service": roles.get("fan_service_reviewer") or cfg.get("llm", "model"),
    }


def run_reviewers(script: dict[str, Any], show: Show | None = None,
                  cfg=None, llm=None, episode: int = 0, round_no: int = 1,
                  notes_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Run all configured reviewers over a script. Returns {reviewer: verdict}.

    Each reviewer: {score, notes: [{scene, item/note, ...}], pass}.
    Notes files are written separately per reviewer under notes_dir
    (default: runs/EP<episode>/reviews/<reviewer>.r<round>.json).
    """
    cfg = cfg or get_config()
    models = reviewer_models(cfg)
    roles = cfg.get("reviewers", "roles", ["slop", "continuity", "fan_service"])
    thresholds = cfg.get("reviewers", "thresholds", {})

    continuity = show.read_continuity() if show else {"episode": 0}
    characters = [show.read_character(c) for c in show.list_characters()] if show else []
    bible = show.read_bible() if show else {}
    mature_spec = bible.get("mature_spec", {}) if show else {}

    results: dict[str, dict[str, Any]] = {}
    for reviewer in roles:
        if reviewer == "slop":
            user = prompts.slop_review_prompt(script)
        elif reviewer == "continuity":
            user = prompts.continuity_review_prompt(script, continuity, characters)
        elif reviewer == "fan_service":
            user = prompts.fanservice_review_prompt(script, mature_spec)
        else:
            continue
        system = prompts.reviewer_system(reviewer, cfg.get("show_profile", "baseline", "ranma-1-2"))
        data = llm.chat_json([{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                             model=models[reviewer], temperature=0.2, max_tokens=4096)

        passed = bool(data.get("pass", True))
        if reviewer == "slop" and float(data.get("score", 0)) > float(thresholds.get("slop_block", 0.45)):
            passed = False
        results[reviewer] = {
            "score": data.get("score", 0.0),
            "pass": passed,
            "notes": data.get("notes", []),
            "raw": data,
        }

        # separate notes file per reviewer
        base = notes_dir or (cfg.root / "runs" / f"EP{episode:02d}" / "reviews")
        base.mkdir(parents=True, exist_ok=True)
        (base / f"{reviewer}.r{round_no}.json").write_text(
            json.dumps({"round": round_no, **results[reviewer]}, indent=2), encoding="utf-8")

    return results


def all_pass(results: dict[str, dict[str, Any]]) -> bool:
    return all(r.get("pass") for r in results.values())
