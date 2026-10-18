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

#: Prepended to every OUTLINE-stage review. The writers'-room reviewers are
#: script-shaped, but an outline is plot + scene summaries + beats only — so
#: without this they judge material that does not exist yet (voice, wardrobe,
#: shot polish) and fail the outline for stage-inappropriate reasons.
OUTLINE_REVIEW_CONTEXT = (
    "STAGE NOTE: what you are reviewing is an EPISODE OUTLINE — a plot plus scene "
    "summaries and Setup/Change/Consequence beats ONLY. Dialogue, line wording, voice, "
    "shot-level action, camera, clothing/wardrobe and staging do NOT exist yet and must "
    "NOT be judged or reported. Judge only outline-level concerns: plot and "
    "cause-and-effect, act structure and pacing, protagonist agency, stakes, location "
    "variety, serial progression, continuity of ESTABLISHED facts, and whether required "
    "content (e.g. mature/fan-service) is STAGED in the beats.\n\n"
)


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
        grade = str(data.get("grade", "")).strip().lower()
        passed = grade == "pass"
        results[reviewer] = {"grade": "Pass" if passed else "Fail",
                             "pass": passed,
                             "review": str(data.get("review") or "").strip()}
        _write_review(notes_dir or (cfg.root / "runs" / f"EP{episode:02d}" / "reviews"),
                      reviewer, round_no, results[reviewer])
    return results


def plan_pseudo_script(plan: dict[str, Any],
                       target_s: int = DEFAULT_RUNTIME_TARGET_S) -> dict[str, Any]:
    """Shape an episode OUTLINE as a script so the writers'-room reviewers can read it.

    The outline carries scene summaries + Setup/Change/Consequence beats but no
    dialogue or shot detail yet, so each beat becomes one placeholder "shot" and
    the target runtime is spread evenly across scenes (giving the pacing reviewer a
    real length to judge). ``_review_note`` tells the reviewers what does NOT exist
    yet, so they flag plot/structure/pacing/agency/stakes/location/mature-delivery
    issues instead of dialogue wording or shot polish.
    """
    scenes = [s for s in (plan.get("scenes") or [])
              if isinstance(s, dict) and s.get("id")]
    # Spread the target across SCENES, then split each scene's share across its
    # beats, so the pseudo-script totals ~target_s. Giving every beat-shot the full
    # per-scene duration made an N-beat scene count N times (a 5-scene/15-beat
    # outline summed to ~3x target) and every pacing review led with a phantom
    # "cut two-thirds runtime" that shrank the outline for no real reason.
    per_scene = (float(target_s) / len(scenes)) if (scenes and target_s) else 8.0
    out_scenes: list[dict[str, Any]] = []
    for sc in scenes:
        beats = [str(b).strip() for b in (sc.get("beats") or []) if str(b).strip()]
        if not beats:
            beats = [str(sc.get("summary") or "").strip()]
        per_beat = per_scene / len(beats)
        shots = [{"id": f"{sc.get('id')}_sh{i:02d}", "duration_s": round(per_beat, 1),
                  "action": b, "dialogue": [],
                  "references": {"characters": sc.get("characters") or []}}
                 for i, b in enumerate(beats, start=1)]
        out_scenes.append({
            "id": sc.get("id"), "location": sc.get("location", ""),
            "summary": sc.get("summary", ""), "narrative": "\n".join(beats),
            "beats": beats, "characters": sc.get("characters") or [],
            "shots": shots})
    return {
        "episode": plan.get("episode", 0),
        "summary": plan.get("plot") or plan.get("synopsis") or "",
        "cast": plan.get("cast") or plan.get("characters") or [],
        "_review_note": (
            "This is an EPISODE OUTLINE (scene summaries + Setup/Change/Consequence "
            "beats only; no dialogue or shot detail exists yet). Review plot, act "
            "structure, pacing/length, protagonist agency, stakes, location variety, "
            "serial progression, and mature/fan-service delivery. Do NOT flag dialogue "
            "wording, line count, or shot-level polish that does not exist at this stage."),
        "scenes": out_scenes,
    }


def run_plan_room_reviewers(plan: dict[str, Any], show: Show | None = None,
                            cfg=None, llm=None, episode: int = 0, round_no: int = 1,
                            notes_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Run the writers'-room reviewers over the episode OUTLINE.

    The outline decides length, locations, fan-service placement, and the beat
    shape before any downstream writer sees it — so the room that guards those
    (pacing, repetition/location, fan_service, stakes, agency, serial, continuity,
    slop, visual) reviews the OUTLINE, not just the finished script. Reviewers that
    judge spoken lines (voice, exposition) are excluded: there is no dialogue yet,
    and they still run on the prose/script. Configured by ``reviewers.plan_room_roles``.
    """
    cfg = cfg or get_config()
    roles = cfg.get("reviewers", "plan_room_roles", []) or []
    if not roles:
        return {}
    bible = show.read_bible() if show else {}
    target = int(bible.get("runtime_target_s", DEFAULT_RUNTIME_TARGET_S)
                 or DEFAULT_RUNTIME_TARGET_S) if bible else DEFAULT_RUNTIME_TARGET_S
    pseudo = plan_pseudo_script(plan, target)
    return run_reviewers(pseudo, show=show, cfg=cfg, llm=llm, episode=episode,
                         round_no=round_no, notes_dir=notes_dir,
                         step_prefix="plan-review", notes_prefix="plan-",
                         roles_override=roles, outline=True)


def run_reviewers(script: dict[str, Any], show: Show | None = None,
                  cfg=None, llm=None, episode: int = 0, round_no: int = 1,
                  notes_dir: Path | None = None,
                  step_prefix: str = "review", notes_prefix: str = "",
                  roles_override: list[str] | None = None,
                  outline: bool = False) -> dict[str, dict[str, Any]]:
    """Run all configured reviewers over a script. Returns {reviewer: verdict}.

    Each reviewer: {score, notes: [{scene, item/note, ...}], pass}.
    Notes files are written separately per reviewer under notes_dir
    (default: runs/EP<episode>/reviews/<reviewer>.r<round>.json).

    ``step_prefix`` / ``notes_prefix`` let a second pass (the outline room) reuse
    the whole room without colliding with the script-review logs/notes files, and
    ``roles_override`` selects a subset (or superset) of reviewers for that pass.
    """
    cfg = cfg or get_config()
    models = reviewer_models(cfg)
    roles = roles_override or cfg.get("reviewers", "roles",
                                      ["slop", "continuity", "fan_service"])
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
                                         "repetition_min_cluster", 3)),
                                     content=not outline)
            results[reviewer] = data
            _write_review(notes_dir or (cfg.root / "runs" / f"EP{episode:02d}" / "reviews"),
                          f"{notes_prefix}{reviewer}", round_no, data)
            continue
        if reviewer == "slop":
            user = prompts.slop_review_prompt(script)
        elif reviewer == "continuity":
            user = prompts.continuity_review_prompt(script, continuity, characters)
        elif reviewer == "fan_service":
            from . import service as _service
            _led = _service.service_ledger(script.get("scenes") or [])
            _rew = bool(show and _service.is_reward_episode(show, episode, cfg))
            _short = _service.ledger_shortfalls(
                bible if show else {}, _led, reward=_rew)
            user = prompts.fanservice_review_prompt(
                script, mature_spec,
                style.get("fanservice_baseline", ""),
                ledger=_led, shortfalls=_short)
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
        if outline:
            user = OUTLINE_REVIEW_CONTEXT + user
        system = prompts.reviewer_system(reviewer, style.get("baseline", ""))
        data = promptlog.logged_chat_json(
            llm, show, episode, f"{step_prefix}-{reviewer}-r{round_no}",
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            model=models[reviewer], temperature=0.2, max_tokens=4096,
            role=f"{reviewer}_reviewer")

        # The reviewer returns a single verdict: grade + free-form review. The gate
        # is simply grade == "Pass"; there is no score/severity to reconcile.
        grade = str(data.get("grade", "")).strip().lower()
        passed = grade == "pass"
        results[reviewer] = {"grade": "Pass" if passed else "Fail",
                             "pass": passed,
                             "review": str(data.get("review") or "").strip()}

        # separate review file per reviewer
        base = notes_dir or (cfg.root / "runs" / f"EP{episode:02d}" / "reviews")
        _write_review(base, f"{notes_prefix}{reviewer}", round_no, results[reviewer])

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
                      min_cluster: int = 3, content: bool = True) -> dict[str, Any]:
    """Deterministic 'the same scene again' detector (no LLM).

    Flags (HIGH) when `min_cluster`+ scenes share a location, or when scenes are
    near-identical by content (Jaccard over their words, after dropping tokens
    common to most scenes so shared cast/setting names don't cause false hits).
    This is the cheap gate that stops an episode of 8 identical rescues before the
    dialogue writer has to invent eight sets of filler quips.

    ``content=False`` keeps only the location check — used when reviewing a terse
    OUTLINE, where Setup/Change/Consequence beat templates make the word-Jaccard
    check false-positive on text that is not actually repetitive.
    """
    scenes = [s for s in (scenes or []) if isinstance(s, dict)]
    n = len(scenes)
    if n < min_cluster:
        return {"grade": "Pass", "pass": True, "review": ""}

    # Raw Jaccard: scenes that merely share names/setting words still score low
    # (the shared words are a small fraction of each scene), so no df filtering is
    # needed, and genuinely identical scenes score ~1.0 as they should.
    toks = [set(_scene_words(s)) for s in scenes]

    msgs: list[str] = []
    flagged: set[int] = set()

    by_loc: dict[str, list[int]] = {}
    for i, s in enumerate(scenes):
        loc = str(s.get("location") or "").strip().lower()
        if loc:
            by_loc.setdefault(loc, []).append(i)
    for loc, idxs in by_loc.items():
        if len(idxs) >= min_cluster:
            flagged.update(idxs)
            msgs.append(f"{len(idxs)} scenes share the location '{loc}' — cut or move some.")

    def jac(a: set[str], b: set[str]) -> float:
        return len(a & b) / len(a | b) if (a and b) else 0.0

    content_hits = 0
    if content:
        for i in range(n):
            for j in range(i + 1, n):
                if jac(toks[i], toks[j]) >= similarity:
                    flagged.update((i, j))
                    if content_hits < 8:
                        msgs.append(f"scenes {scenes[i].get('id')}/{scenes[j].get('id')} are "
                                    "near-identical — collapse or differentiate them.")
                        content_hits += 1

    return {"grade": "Fail" if flagged else "Pass", "pass": not flagged,
            "review": " ".join(msgs)}


def all_pass(results: dict[str, dict[str, Any]]) -> bool:
    return all(r.get("pass") for r in results.values())


#: The ONLY reviewers that BLOCK an episode outline. The rest are recorded as
#: advisory notes and must not gate: ~10 blocking reviewers with contradictory
#: demands made the outline revise-loop oscillate instead of converge.
PLAN_BLOCKING_REVIEWERS = ("structure", "continuity", "fan_service")


def plan_passes(results: dict[str, dict[str, Any]]) -> bool:
    """True when the blocking outline reviewers pass (advisory ones ignored)."""
    for name in PLAN_BLOCKING_REVIEWERS:
        r = results.get(name) or {}
        if r.get("pass") is False or str(r.get("grade", "")).lower() == "fail":
            return False
    return True


def failing_reviews(results: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Free-form reviews of the reviewers that FAILED (``pass is False``)."""
    return {r: str(v.get("review") or "").strip()
            for r, v in (results or {}).items()
            if isinstance(v, dict) and v.get("pass") is False}


def review_notes_text(results: dict[str, dict[str, Any]]) -> str:
    """Concatenate the failing reviewers' free-form reviews into one notes block."""
    return "\n\n".join(f"[{r}] {txt}" for r, txt in failing_reviews(results).items() if txt)


def _write_review(base: Path, name: str, round_no: int, verdict: dict[str, Any]) -> None:
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{name}.r{round_no}.json").write_text(
        json.dumps({"round": round_no, **verdict}, indent=2), encoding="utf-8")
