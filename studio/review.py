"""Stage 2a - writers'-room script reviewers (DESIGN.md §9 Stage 2a).

Runs the slop / continuity / fan_service reviewers over a script JSON, each
writing its own separate notes file, and returns per-reviewer verdicts. The
revision loop (draft -> notes -> revise -> re-review) is orchestrated by the
stage consumer; this module is the single review pass.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import promptlog
from . import prompts
from .config import DEFAULT_RUNTIME_TARGET_S, get_config
from .show import Show


def reviewer_models(cfg) -> dict[str, str]:
    roles = cfg.get("llm", "roles", {})
    fallback = cfg.get("llm", "model")
    return {
        "slop": roles.get("slop_reviewer") or fallback,
        "continuity": roles.get("continuity_reviewer") or fallback,
        "fan_service": roles.get("fan_service_reviewer") or fallback,
        "structure": roles.get("structure_reviewer") or fallback,
        "voice": roles.get("voice_reviewer") or fallback,
        "exposition": roles.get("exposition_reviewer") or fallback,
        "pacing": roles.get("pacing_reviewer") or fallback,
        "stakes": roles.get("stakes_reviewer") or fallback,
        "visual": roles.get("visual_reviewer") or fallback,
        "agency": roles.get("agency_reviewer") or fallback,
        "serial": roles.get("serial_reviewer") or fallback,
        "repetition": roles.get("repetition_reviewer") or fallback,
    }


def _prior_episode_summaries(show: Show, episode: int) -> list[dict[str, Any]]:
    """One-line plot summaries of earlier episodes, for the serial reviewer."""
    import json as _json
    runs = show.dir / "runs"
    out: list[dict[str, Any]] = []
    if not runs.exists():
        return out
    for d in sorted(runs.iterdir()):
        if not d.is_dir() or not d.name.startswith("EP"):
            continue
        ep = int(d.name[2:]) if d.name[2:].isdigit() else 0
        if ep >= episode:
            continue
        plan_f = d / "plan.json"
        if not plan_f.exists():
            continue
        try:
            pl = _json.loads(plan_f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"episode": ep,
                    "plot": pl.get("plot") or pl.get("summary") or ""})
    return out


def run_plan_reviewers(plan: dict[str, Any], show: Show | None = None,
                       cfg=None, llm=None, episode: int = 0, round_no: int = 1,
                       notes_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Run plan-level reviewers over an episode OUTLINE (not a script).

    Currently a single 'structure' reviewer that catches outline-level incoherence
    (plot vs synopsis vs scenes, cold-open-without-context for episode 1, unresolved
    threat, repeated climax). Returns {reviewer: verdict} and writes a notes file
    per reviewer under notes_dir.
    """
    cfg = cfg or get_config()
    roles = cfg.get("reviewers", "plan_roles", ["structure"])
    thresholds = cfg.get("reviewers", "thresholds", {})
    bible = show.read_bible() if show else {}
    # The reviewer must know the FULL cast — including enemy/visual entities that
    # have character sheets but aren't in bible.cast (which only lists the humans).
    # Without this it flags legitimate threat characters as "not part of the
    # established cast" and the outline can never pass.
    cast = []
    if show:
        cast = [c.get("name") for c in (show.read_character(cid)
                for cid in show.list_characters()) if c.get("name")]
    baseline = (show.style_profile(cfg).get("baseline", "") if show
                else cfg.get("show_profile", "baseline", ""))
    model = cfg.get("llm", "roles", {}).get("structure_reviewer") or cfg.get("llm", "model")

    results: dict[str, dict[str, Any]] = {}
    for reviewer in roles:
        if reviewer != "structure":
            continue
        user = prompts.plan_structure_review_prompt(plan, bible, episode, cast=cast)
        system = prompts.plan_reviewer_system(reviewer, baseline)
        data = promptlog.logged_chat_json(
            llm, show, episode, f"review-plan-structure-r{round_no}",
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            model=model, temperature=0.2, max_tokens=4096,
            role="structure_reviewer")
        notes = data.get("notes", []) or []
        # The structure reviewer is a GATE. Sensitivity is configurable via
        # reviewers.thresholds.structure_block, mirroring the script reviewers:
        #   any   -> fail on ANY note (zero-issue default: no known problem ships)
        #   high  -> fail only on HIGH-severity notes
        #   never -> advisory only, never blocks
        # Never trust the model's own 'pass' verdict.
        block_mode = str(thresholds.get("structure_block", "any") or "any").lower()
        has_high = any(str(n.get("severity", "")).upper() == "HIGH"
                       for n in notes if isinstance(n, dict))
        if block_mode == "never":
            passed = True
        elif block_mode == "high":
            passed = not has_high
        else:  # "any" (default)
            passed = not notes
        results[reviewer] = {
            "score": data.get("score", 0.0),
            "pass": passed,
            "notes": notes,
            "raw": data,
        }
        base = notes_dir or (cfg.root / "runs" / f"EP{episode:02d}" / "reviews")
        base.mkdir(parents=True, exist_ok=True)
        (base / f"{reviewer}.r{round_no}.json").write_text(
            json.dumps({"round": round_no, **results[reviewer]}, indent=2), encoding="utf-8")
    return results


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
    style = show.style_profile(cfg) if show else (cfg.get("show_profile") or {})

    results: dict[str, dict[str, Any]] = {}
    prior = _prior_episode_summaries(show, episode) if show else []
    for reviewer in roles:
        if reviewer == "repetition":
            # Deterministic (no LLM): catches an episode that replays the same
            # scene/beat again and again, which is the root of quip-only dialogue.
            data = repetition_review(script.get("scenes") or [],
                                     similarity=float(thresholds.get(
                                         "repetition_similarity", 0.5)),
                                     min_cluster=int(thresholds.get(
                                         "repetition_min_cluster", 3)))
            results[reviewer] = {"score": data.get("score", 0.0),
                                 "pass": bool(data.get("pass", True)),
                                 "notes": data.get("notes", []) or [],
                                 "raw": data}
            base = notes_dir or (cfg.root / "runs" / f"EP{episode:02d}" / "reviews")
            base.mkdir(parents=True, exist_ok=True)
            (base / f"{reviewer}.r{round_no}.json").write_text(
                json.dumps({"round": round_no, **results[reviewer]}, indent=2),
                encoding="utf-8")
            continue
        if reviewer == "slop":
            user = prompts.slop_review_prompt(script)
        elif reviewer == "continuity":
            user = prompts.continuity_review_prompt(script, continuity, characters)
        elif reviewer == "fan_service":
            user = prompts.fanservice_review_prompt(
                script, mature_spec,
                style.get("fanservice_baseline", ""))
        elif reviewer == "voice":
            user = prompts.voice_review_prompt(script, characters,
                                               continuity.get("nicknames"))
        elif reviewer == "exposition":
            user = prompts.exposition_review_prompt(script)
        elif reviewer == "pacing":
            target = int(bible.get("runtime_target_s", DEFAULT_RUNTIME_TARGET_S) or DEFAULT_RUNTIME_TARGET_S) if bible else DEFAULT_RUNTIME_TARGET_S
            user = prompts.pacing_review_prompt(script, target)
        elif reviewer == "stakes":
            user = prompts.stakes_review_prompt(script, characters)
        elif reviewer == "visual":
            user = prompts.visual_review_prompt(script)
        elif reviewer == "agency":
            user = prompts.agency_review_prompt(script, characters)
        elif reviewer == "serial":
            user = prompts.serial_review_prompt(script, continuity, characters, prior)
        else:
            continue
        system = prompts.reviewer_system(reviewer, style.get("baseline", ""))
        data = promptlog.logged_chat_json(
            llm, show, episode, f"review-{reviewer}-r{round_no}",
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            model=models[reviewer], temperature=0.2, max_tokens=4096,
            role=f"{reviewer}_reviewer")

        passed = bool(data.get("pass", True))
        if reviewer == "slop" and float(data.get("score", 0)) > float(thresholds.get("slop_block", 0.45)):
            passed = False
        # Configurable gate sensitivity for the added reviewers:
        #   block_any  -> fail if any note is returned
        #   block_high -> fail only on HIGH-severity notes
        #   never      -> advisory only, never blocks
        block_mode = str(thresholds.get(f"{reviewer}_block", "any") or "any").lower()
        notes = data.get("notes", []) or []
        if reviewer in ("voice", "exposition", "pacing", "stakes", "visual", "agency", "serial"):
            if block_mode == "any" and notes:
                passed = False
            elif block_mode == "high":
                sev = [str(n.get("severity", "")).upper() for n in notes if isinstance(n, dict)]
                if any(s == "HIGH" for s in sev):
                    passed = False
        results[reviewer] = {
            "score": data.get("score", 0.0),
            "pass": passed,
            "notes": notes,
            "raw": data,
        }

        # separate notes file per reviewer
        base = notes_dir or (cfg.root / "runs" / f"EP{episode:02d}" / "reviews")
        base.mkdir(parents=True, exist_ok=True)
        (base / f"{reviewer}.r{round_no}.json").write_text(
            json.dumps({"round": round_no, **results[reviewer]}, indent=2), encoding="utf-8")

    return results


def _scene_words(s: dict[str, Any]) -> list[str]:
    parts = [str(s.get("location", "")), str(s.get("summary", "")),
             str(s.get("narrative", "") or "")]
    parts += [str(b) for b in (s.get("beats") or [])]
    for sh in (s.get("shots") or []):
        if isinstance(sh, dict):
            parts.append(str(sh.get("action", "")))
            for d in (sh.get("dialogue") or []):
                parts.append(str(d.get("line", "") if isinstance(d, dict) else d))
    return re.findall(r"[a-z]{3,}", " ".join(parts).lower())


def repetition_review(scenes: list[dict[str, Any]], similarity: float = 0.5,
                      min_cluster: int = 3) -> dict[str, Any]:
    """Deterministic 'the same scene again' detector (no LLM).

    Flags (HIGH) when `min_cluster`+ scenes share a location, or when scenes are
    near-identical by content (Jaccard over their words, after dropping tokens
    common to most scenes so shared cast/setting names don't cause false hits).
    This is the cheap gate that stops an episode of 8 identical rescues before the
    dialogue writer has to invent eight sets of filler quips.
    """
    scenes = [s for s in (scenes or []) if isinstance(s, dict)]
    n = len(scenes)
    if n < min_cluster:
        return {"score": 0.0, "notes": [], "pass": True}

    # Raw Jaccard: scenes that merely share names/setting words still score low
    # (the shared words are a small fraction of each scene), so no df filtering is
    # needed, and genuinely identical scenes score ~1.0 as they should.
    toks = [set(_scene_words(s)) for s in scenes]

    notes: list[dict[str, Any]] = []
    flagged: set[int] = set()

    by_loc: dict[str, list[int]] = {}
    for i, s in enumerate(scenes):
        loc = str(s.get("location") or "").strip().lower()
        if loc:
            by_loc.setdefault(loc, []).append(i)
    for loc, idxs in by_loc.items():
        if len(idxs) >= min_cluster:
            flagged.update(idxs)
            notes.append({"severity": "HIGH", "item": "location",
                          "note": f"{len(idxs)} scenes share the location '{loc}' - "
                                  "the same beat plays in the same place; cut or move them"})

    def jac(a: set[str], b: set[str]) -> float:
        return len(a & b) / len(a | b) if (a and b) else 0.0

    content_hits = 0
    for i in range(n):
        for j in range(i + 1, n):
            if jac(toks[i], toks[j]) >= similarity:
                flagged.update((i, j))
                if content_hits < 8:
                    notes.append({"severity": "HIGH",
                                  "item": f"{scenes[i].get('id')}/{scenes[j].get('id')}",
                                  "note": "near-identical scenes - collapse or differentiate"})
                    content_hits += 1

    score = round(len(flagged) / n, 2)
    return {"score": score, "notes": notes, "pass": not flagged}


def all_pass(results: dict[str, dict[str, Any]]) -> bool:
    return all(r.get("pass") for r in results.values())
