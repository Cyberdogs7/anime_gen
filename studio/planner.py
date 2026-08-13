"""Chunked episode generation: plan -> per-scene shots -> assemble.

Keeps each LLM call's context LOCALIZED so a full-length episode is achievable:
  1. PLAN  — one call: episode summary + scene-by-scene blueprint (approval gate).
  2. SHOTS — one call PER SCENE (bible + plan scene + cast only) -> that scene's shots.
  3. ASSEMBLE — combine scene shots into the episode script, runtime-check, write.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import prompts
from .bootstrap import ACTIVITY
from .config import get_config
from .review import all_pass, run_plan_reviewers, run_reviewers
from .scriptgen import _normalize, _runtime_review
from .show import Show

PLAN_FILE = "plan.json"

# Transient costume descriptors: a label containing one of these is NOT a
# distinct costume — it is the character's outfit in a situational state
# (wet/bloody/scorched/posed/camera-framed). Such labels are dropped to base
# deterministically, so a failed reconcile LLM call can never let a transient
# label through to the expensive ref generator.
_TRANSIENT_TOKENS = (
    "wet", "damp", "soaked", "blood", "bloodstain", "scorch", "burn", "smoke",
    "steam", "vent", "rain", "weathered", "scarred",
    "close-up", "closeup", "focus on", "focus", "out of frame", "out of focus",
    "slightly visible", "in the background", "in background", "implied presence",
    "behind her", "behind him", "detail", "detailed", "gloves", "gauntlet detail",
    "watching", "observing", "observational",
    "leaning", "turning", "stance", "posture", "pose", "ready", "calm",
    "relaxed", "confident", "intense", "gaze", "expression", "smiling",
    "clenching", "nodding", "aftermath", "post-strike", "post-impact",
    "settling", "triumphant", "subtle shift", "mid-freeze", "mid-charge",
    "charging", "discharging", "active charge", "peak power", "full power",
    "power output", "power dissipation", "stabilized", "dampeners engaged",
    "field active", "field deployment", "defensive plating engaged",
    "phase lock indicator", "plasma gauntlet active", "kinetic charge",
    "kinetic surge", "kinetic strike", "maintaining charge", "mid-flight",
)


def is_transient_costume_label(label: str) -> bool:
    """True when a costume label describes a situational state, not an outfit.

    The base outfit worn during rain, after a hit, in a close-up, or mid-power-
    surge is the SAME costume — it must not spawn its own reference image.
    """
    low = (label or "").strip().lower()
    return any(tok in low for tok in _TRANSIENT_TOKENS)


def is_base_outfit_label(label: str) -> bool:
    """True when a label refers to the character's DEFAULT/base outfit.

    The studio already has ONE reference image for a character's base form
    (``variants.base``). Labels like "Base Armor Suit", "Base Mercenary
    Armor/Jacket" or "Default gear" are that same base outfit spelled out by the
    shot writer — they must resolve to the base ref, NOT spawn a new Qwen edit
    of an identical costume.
    """
    low = (label or "").strip().lower()
    if low in ("base", "default", "base outfit", "base form", "default gear"):
        return True
    return low.startswith("base ") or low.startswith("default ")


def _plan_path(show: Show, episode: int) -> Path:
    p = show.dir / "runs" / f"EP{episode:02d}" / PLAN_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _target(show: Show) -> int:
    return int((show.read_bible() or {}).get("runtime_target_s", 1320) or 1320)


def _director_notes_path(show: Show) -> Path:
    return show.dir / "director_notes.json"


def _story_engine_path(show: Show) -> Path:
    return show.dir / "story_engine.txt"


def read_story_engine(show: Show) -> str:
    p = _story_engine_path(show)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def ensure_story_engine(show: Show, llm=None, cfg=None) -> str:
    """Generate (once) and store the show-specific episode-outline prompt template."""
    # Storyboard jobs can be restarted by the dashboard self-healer. Keep this
    # expensive LLM pass idempotent across those restarts.
    if script.get("_costume_reconciled"):
        return script
    from .clients.lmstudio import LMStudioClient
    from . import prompts as _p
    existing = read_story_engine(show)
    if existing:
        return existing
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    bible = show.read_bible()
    ACTIVITY[show.show_id] = {"detail": "Designing this show's story engine prompt…",
                              "ts": time.time()}
    text = llm.chat(
        [{"role": "system", "content": prompts.showrunner_system(
            cfg["show_profile"], bible.get("content_policy", "mature"),
            cfg["show_profile"].get("baseline", "ranma-1-2"))},
         {"role": "user", "content": _p.story_engine_architect_prompt(bible)}],
        model=model, temperature=0.6, max_tokens=16384,
        on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
            "detail": f"Designing story engine ({n} tokens…)",
            "output": t[-500:], "ts": time.time()}}))
    text = (text or "").strip()
    if text:
        _story_engine_path(show).write_text(text, encoding="utf-8")
    return text


def fill_outline_template(template: str, state: dict[str, Any], bible: dict[str, Any]) -> str:
    """Fill a show-specific outline template with the live per-episode data."""
    reps = {
        "{{SERIES_BIBLE_JSON}}": json.dumps(bible, ensure_ascii=False),
        "{{EPISODE_NUMBER}}": str(state.get("episode", "")),
        "{{ACTIVE_PLOTLINE_DATA}}": state.get("active", "{}"),
        "{{DORMANT_PLOTLINE_DATA}}": state.get("dormant", "{}"),
        "{{DORMANT_PLOTLINE_TO_TEASE}}": state.get("dormant", "{}"),
        "{{COOLING_PLOTLINES_LIST}}": state.get("cooling", "[]"),
        "{{RECENT_EPISODES_SUMMARY_LOG}}": state.get("history", "No prior episodes."),
    }
    out = template
    for k, v in reps.items():
        out = out.replace(k, v)
    return out


def state_tracker_data(show: Show, episode: int) -> dict[str, Any]:
    """Build the <state_tracker> + <episode_history> payload for the outline prompt."""
    from .development import _plotline_state
    bible = show.read_bible()
    continuity = show.read_continuity()
    plotlines = _plotline_state(show, bible)
    active = [p for p in plotlines if p.get("status") == "active"]
    dormant = [p for p in plotlines if p.get("status") == "dormant"]
    cooling = [p for p in plotlines
               if (p.get("last_seen_episode") or 0) >= episode - 2
               and p.get("status") != "dormant"]
    tease = dormant[0] if dormant else {}
    history_lines = []
    runs = show.dir / "runs"
    if runs.exists():
        for d in sorted(runs.iterdir()):
            if not d.name.startswith("EP") or d.name == f"EP{episode:02d}":
                continue
            plan_f = d / "plan.json"
            if plan_f.exists():
                try:
                    pl = json.loads(plan_f.read_text(encoding="utf-8"))
                    plot = pl.get("plot") or pl.get("summary") or ""
                    history_lines.append(f"EP{d.name[-2:]}: {plot[:200]}")
                except Exception:
                    pass
    return {
        "episode": episode,
        "active": json.dumps(active, ensure_ascii=False) if active else "{}",
        "dormant": json.dumps(tease, ensure_ascii=False) if tease else "{}",
        "cooling": json.dumps([p.get("name") or p.get("id") for p in cooling],
                              ensure_ascii=False) if cooling else "[]",
        "history": "\n".join(history_lines[-3:]) if history_lines else "No prior episodes.",
    }


def read_director_notes(show: Show) -> list[str]:
    p = _director_notes_path(show)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def add_director_note(show: Show, note: str) -> list[str]:
    """Persist a rejection note as a durable director constraint (deduped)."""
    note = note.strip()
    notes = read_director_notes(show)
    if note and not any(note.lower() in n.lower() or n.lower() in note.lower() for n in notes):
        notes.append(note)
        _director_notes_path(show).write_text(json.dumps(notes, indent=2, ensure_ascii=False),
                                              encoding="utf-8")
    return notes



def generate_episode_plan(show: Show, episode: int, llm=None, cfg=None,
                          notes: str = "") -> dict[str, Any]:
    """Chunk 1: produce the episode plan (summary + scene blueprint). Writes plan.json."""
    from .clients.lmstudio import LMStudioClient
    from .development import develop_episode
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    bible = show.read_bible()
    continuity = show.read_continuity()
    names = [c.get("name") for c in (show.read_character(cid) for cid in show.list_characters())
             if c.get("name")]
    ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: writing plan…", "ts": time.time()}
    dev = develop_episode(show, episode, llm=llm, cfg=cfg, notes=notes)
    synopsis = dev.get("synopsis", "") if isinstance(dev, dict) else str(dev or "")
    # What this episode features is decided at development time but must only be
    # COMMITTED to continuity/bible when the plan is approved — otherwise every
    # rejected/regenerated episode re-grows the same plotlines. Carry it on the
    # plan so approve_plan can commit it.
    plan_dev = {"featured": (dev or {}).get("featured", []),
                "new_plotline": (dev or {}).get("new_plotline")}
    state = state_tracker_data(show, episode)
    current_plan = None
    if notes.strip():
        prior = read_episode_plan(show, episode)
        current_plan = {k: v for k, v in prior.items()
                        if k not in ("status", "rejected_notes", "synopsis",
                                     "_plan_dev")} or None
    if current_plan and notes.strip():
        user_prompt = prompts.episode_plan_prompt(
            bible, synopsis, continuity, names, _target(show), current_plan, notes, None, state)
    else:
        template = read_story_engine(show)
        if not template:
            template = ensure_story_engine(show, llm=llm, cfg=cfg)
        user_prompt = fill_outline_template(template or "", state, bible)
    # Durable director constraints shape the outline directly: append them to the
    # prompt so they are part of the creative mandate, not just stored metadata.
    dir_notes = read_director_notes(show)
    if dir_notes:
        user_prompt += (
            "\n\nDIRECTOR'S STANDING CONSTRAINTS (ABSOLUTE, override the bible/plotlines/"
            "outline wherever they conflict; apply every one):\n- "
            + "\n- ".join(dir_notes))
    plan = llm.chat_json(
        [{"role": "system", "content": prompts.showrunner_system(
            cfg["show_profile"], bible.get("content_policy", "mature"),
            cfg["show_profile"].get("baseline", "ranma-1-2"))},
         {"role": "user", "content": user_prompt}],
        model=model, temperature=0.8, max_tokens=16384,
        on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
            "detail": f"Episode {episode}: writing plan ({n} tokens…)",
            "output": t[-500:], "ts": time.time()}}))
    # Director constraints are also part of the outline's recorded mandate.
    if dir_notes:
        plan.setdefault("director_constraints", dir_notes)
    if isinstance(plan.get("plot"), dict):
        plan["plot"] = (plan["plot"].get("summary") or "") if isinstance(plan["plot"], dict) else plan["plot"]
    if not notes.strip():
        plan.setdefault("episode", episode)
    if notes.strip():
        plan["rejected_notes"] = notes
    # The model may omit the cast; the episode always features the approved
    # roster (the script assembly keys off "cast", the dashboard off "characters").
    chars = [c for c in (plan.get("characters") or []) if isinstance(c, str) and c]
    if not chars:
        chars = list(names)
    plan["characters"] = chars
    plan.setdefault("cast", chars)
    # The scene-by-scene outline must be a list of dicts with at least an id; drop
    # anything malformed so the dashboard/breakdown never trips on junk.
    plan_scenes = [s for s in (plan.get("scenes") or [])
                   if isinstance(s, dict) and s.get("id")]
    if plan_scenes:
        plan["scenes"] = plan_scenes
    # Plan-level structural review: review the outline itself (plot vs synopsis vs
    # scenes, cold-open-without-context for episode 1, resolved threat, distinct
    # climax). If it fails, revise the outline with the reviewer notes as ABSOLUTE
    # feedback and re-review, up to plan_max_revisions. The outcome is recorded on
    # the plan as plan_review so approve_plan can refuse an outline that never
    # cleared review.
    system_prompt = prompts.showrunner_system(
        cfg["show_profile"], bible.get("content_policy", "mature"),
        cfg["show_profile"].get("baseline", "ranma-1-2"))
    plan = _plan_review_loop(show, episode, plan, llm, cfg, model, system_prompt,
                             bible, synopsis, continuity, names, state, dir_notes)
    plan["status"] = "pending"
    plan["synopsis"] = synopsis
    # Deferred plotline commit: carry what this episode featured so approve_plan
    # can stamp continuity/bible ONLY on approval (never on a rejected/regenerated
    # outline). Stripped from the stored plan by approve_plan.
    plan["_plan_dev"] = plan_dev
    _plan_path(show, episode).write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
    return plan


def read_episode_plan(show: Show, episode: int) -> dict[str, Any]:
    p = _plan_path(show, episode)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _plan_review_feedback(reviews: dict[str, dict[str, Any]]) -> str:
    """Flatten every reviewer's notes into an ABSOLUTE feedback block for revision."""
    lines = []
    for reviewer, r in reviews.items():
        lines.append(f"--- {reviewer} reviewer (pass={r.get('pass')}) ---")
        for note in r.get("notes", []):
            if isinstance(note, dict):
                text = note.get("note") or note.get("item") or str(note)
            else:
                text = str(note)
            lines.append(f"  - {text}")
    return "\n".join(lines)


def _plan_review_loop(show: Show, episode: int, plan: dict[str, Any], llm, cfg,
                      model: str, system: str, bible: dict[str, Any],
                      synopsis: str, continuity: dict[str, Any],
                      names: list[str], state: dict[str, Any],
                      dir_notes: list[str]) -> dict[str, Any]:
    """Review the episode outline and revise it up to plan_max_revisions.

    Mirrors ``_writer_review`` but on the OUTLINE: the structure reviewer checks
    plot-vs-synopsis-vs-scenes coherence and episode-1 context, and a failing
    outline is revised with the reviewer notes as ABSOLUTE feedback. The final
    verdict is stored on the plan as ``plan_review`` so approval gates can refuse
    an outline that never cleared review.
    """
    max_revisions = int(cfg.get("reviewers", "plan_max_revisions", 2) or 2)
    notes_dir = show.dir / "runs" / f"EP{episode:02d}" / "reviews"

    def _review(plan: dict[str, Any], round_no: int) -> dict[str, dict[str, Any]]:
        return run_plan_reviewers(plan, show=show, cfg=cfg, llm=llm, episode=episode,
                                  round_no=round_no, notes_dir=notes_dir)

    def _finalize(p: dict[str, Any]) -> dict[str, Any]:
        """Re-apply the deterministic plan cleanup on a revised outline."""
        chars = [c for c in (p.get("characters") or []) if isinstance(c, str) and c]
        if not chars:
            chars = list(names)
        p["characters"] = chars
        p.setdefault("cast", chars)
        plan_scenes = [s for s in (p.get("scenes") or [])
                       if isinstance(s, dict) and s.get("id")]
        if plan_scenes:
            p["scenes"] = plan_scenes
        if dir_notes:
            p.setdefault("director_constraints", dir_notes)
        # The LLM may drop these on a revision; carry them over from the prior draft.
        if "rejected_notes" in plan:
            p["rejected_notes"] = plan["rejected_notes"]
        if "episode" in plan:
            p.setdefault("episode", plan["episode"])
        return p

    round_no = 1
    reviews = _review(plan, round_no)
    passed = all_pass(reviews)
    while not passed and round_no < max_revisions:
        round_no += 1
        feedback = _plan_review_feedback(reviews)
        ACTIVITY[show.show_id] = {
            "detail": f"Episode {episode}: revising outline from plan review (round {round_no})…",
            "ts": time.time()}
        revised = llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompts.episode_plan_prompt(
                 bible, synopsis, continuity, names, _target(show), plan, feedback,
                 dir_notes, state)}],
            model=model, temperature=0.7, max_tokens=16384,
            on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                "detail": f"Episode {episode}: revising outline ({n} tokens…)",
                "output": t[-500:], "ts": time.time()}}))
        if not isinstance(revised, dict) or not revised.get("scenes"):
            break
        plan = _finalize(revised)
        reviews = _review(plan, round_no)
        passed = all_pass(reviews)
    plan["plan_review"] = {
        "rounds": round_no,
        "passed": passed,
        "notes": [n for r in reviews.values()
                  for n in (r.get("notes") or []) if isinstance(n, dict)],
    }
    return plan


def approve_plan(show: Show, episode: int, llm=None, cfg=None) -> dict[str, Any]:
    plan = read_episode_plan(show, episode)
    # Never rubber-stamp an outline that failed structural review — the fix must
    # land before approval (either it passed review or a human regenerated it).
    review = (plan or {}).get("plan_review") or {}
    if review.get("passed") is False:
        notes = "; ".join(str(n.get("note") or n.get("item") or n)
                          for n in (review.get("notes") or [])[:3]) or "failed structural review"
        raise ValueError(
            f"plan EP{episode:02d} failed structural review ({review.get('rounds', 1)} "
            f"rounds): {notes} — reject it with notes to regenerate, or clear the "
            "review before approving.")
    plan["status"] = "approved"
    # Commit the plotline bookkeeping NOW (this is the real approval gate): stamp
    # last_seen on featured plotlines and persist a reviewed new plotline to the
    # bible + continuity. Development only carried it on the plan, so a rejected
    # or deleted episode never mutated continuity.
    dev = (plan or {}).get("_plan_dev") or {}
    try:
        from .growth import commit_plotline_on_approval
        commit_plotline_on_approval(show, dev.get("featured", []),
                                    dev.get("new_plotline"), episode, llm=llm, cfg=cfg)
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "plotline commit failed for %s EP%02d (plan still approved): %s",
            show.show_id, episode, exc)
    # The plan payload is downstream data; don't persist the internal dev marker.
    plan.pop("_plan_dev", None)
    _plan_path(show, episode).write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
    return plan


def reject_plan(show: Show, episode: int, notes: str = "") -> dict[str, Any]:
    plan = read_episode_plan(show, episode)
    plan["status"] = "rejected"
    plan["rejected_notes"] = notes
    _plan_path(show, episode).write_text(json.dumps(plan, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
    return plan


# --- Step 2: detailed scene section ---------------------------------------

def _scenes_dir(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / "scenes"


def _details_path(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / "scene_details.json"


def generate_scene_details(show: Show, episode: int, llm=None, cfg=None,
                           notes: str = "") -> list[dict[str, Any]]:
    """Stage 2: break the approved outline into scenes, then detail each scene.

    Two sub-steps: (2a) scene breakdown from the one-paragraph outline, (2b) a
    detailed treatment per scene. With notes, REWRITES the rejected details in place.
    """
    from .clients.lmstudio import LMStudioClient
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    bible = show.read_bible()
    cast = [show.read_character(cid) for cid in show.list_characters()]
    outline = read_episode_plan(show, episode)

    # 2a: scene breakdown (unless we're rewriting details from a prior breakdown).
    scenes: list[dict[str, Any]] = []
    if notes.strip() and read_scene_details(show, episode).get("scenes"):
        scenes = read_scene_details(show, episode)["scenes"]
        scenes = [{"id": s.get("id"), "location": s.get("location"),
                   "time_of_day": s.get("time_of_day"), "summary": s.get("summary", ""),
                   "characters": s.get("characters", [])} for s in scenes]
    else:
        # The approved outline carries the scene-by-scene blueprint when the story
        # engine produced one (full outline); reuse it directly. Fall back to a
        # dedicated breakdown LLM call only for legacy plans that lack "scenes".
        plan_scenes = outline.get("scenes") or []
        plan_scenes = [s for s in plan_scenes if isinstance(s, dict) and s.get("id")]
        if plan_scenes:
            scenes = [{"id": s.get("id"), "location": s.get("location", ""),
                       "time_of_day": s.get("time_of_day", ""),
                       "summary": s.get("summary", ""),
                       "characters": s.get("characters", [])} for s in plan_scenes]
        else:
            ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: breaking outline into scenes…",
                                      "ts": time.time()}
            breakdown = llm.chat_json(
                [{"role": "system", "content": prompts.showrunner_system(
                    cfg["show_profile"], bible.get("content_policy", "mature"),
                    cfg["show_profile"].get("baseline", "ranma-1-2"))},
                 {"role": "user", "content": prompts.scene_breakdown_prompt(
                     bible, outline, cast, _target(show))}],
                model=model, temperature=0.8, max_tokens=8192,
                on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                    "detail": f"Episode {episode}: scene breakdown ({n} tokens…)",
                    "output": t[-500:], "ts": time.time()}}))
            scenes = breakdown.get("scenes", []) or []

    # 2b: detailed treatment per scene. Resumable: a scene whose detail file
    # already exists is reused, so an interrupted run only regenerates the
    # missing scenes (the reconciler calls this until all plan scenes exist).
    prior = read_scene_details(show, episode).get("scenes", []) if notes.strip() else []
    prior_by_id = {s.get("id"): s for s in prior}
    sc_dir = _scenes_dir(show, episode)
    sc_dir.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict[str, Any]] = {}
    if not notes.strip():
        # Key by FILENAME sid (authoritative): the model sometimes echoes a wrong
        # id in the content, so a checkpoint file's name is what identifies it.
        for f in sc_dir.glob("*.json"):
            if f.name.endswith("_shots.json"):
                continue   # shot checkpoints from assemble; not a scene detail
            sid = f.stem
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(d, dict) and d.get("narrative"):
                    d["id"] = sid
                    existing[sid] = d
            except Exception:
                continue
    details: list[dict[str, Any]] = []
    for i, ps in enumerate(scenes, start=1):
        sid = ps.get("id", f"s{i:02d}")
        if not notes.strip() and sid in existing:
            details.append(existing[sid])
            continue
        current = prior_by_id.get(sid)
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: scene detail {sid}…",
                                  "ts": time.time()}
        detail = llm.chat_json(
            [{"role": "system", "content": prompts.showrunner_system(
                cfg["show_profile"], bible.get("content_policy", "mature"),
                cfg["show_profile"].get("baseline", "ranma-1-2"))},
             {"role": "user", "content": prompts.scene_detail_prompt(
                 bible, outline, cast, current, notes)}],
            model=model, temperature=0.8, max_tokens=8192,
            on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                "detail": f"Episode {episode}: scene detail {sid} ({n} tokens…)",
                "output": t[-500:], "ts": time.time()}}))
        detail.setdefault("id", sid)
        detail["id"] = sid   # force: the model often echoes a wrong id in content
        detail.setdefault("location", ps.get("location", ""))
        detail.setdefault("time_of_day", ps.get("time_of_day", ""))
        detail.setdefault("summary", ps.get("summary", ""))
        detail.setdefault("characters", ps.get("characters", []))
        (sc_dir / f"{sid}.json").write_text(json.dumps(detail, indent=2, ensure_ascii=False),
                                            encoding="utf-8")
        details.append(detail)
    payload = {"status": "pending", "scenes": details,
               **({"rejected_notes": notes} if notes.strip() else {})}
    _details_path(show, episode).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                            encoding="utf-8")
    return details


def read_scene_details(show: Show, episode: int) -> dict[str, Any]:
    p = _details_path(show, episode)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def approve_scene_details(show: Show, episode: int) -> dict[str, Any]:
    data = read_scene_details(show, episode)
    data["status"] = "approved"
    _details_path(show, episode).write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                            encoding="utf-8")
    return data


def _clear_scenes_downstream(show: Show, episode: int) -> None:
    """Delete artifacts derived from scene details.

    Rejecting scenes regenerates the treatments in place (same scene ids), but the
    per-scene shot checkpoints, assembled script, storyboard, object refs and
    rendered videos are all built from the OLD treatments — if they survive, the
    next approve silently reuses the stale shots. This removes exactly what lives
    below scene details, keeping plan.json and the (about-to-be-rewritten)
    scene treatments themselves.
    """
    import shutil
    runs = show.dir / "runs" / f"EP{episode:02d}"
    sc_dir = _scenes_dir(show, episode)
    if sc_dir.exists():
        for f in list(sc_dir.glob("*_shots.json")) + list(sc_dir.glob("*.p*.json")):
            try:
                f.unlink()
            except Exception:
                pass
    for r in list(runs.glob("script.r*.json")):
        try:
            r.unlink()
        except Exception:
            pass
    for sub in ("reviews", "storyboard", "objects", "video"):
        p = runs / sub
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)


def clear_scene_shots(show: Show, episode: int) -> None:
    """Drop everything built from the approved scene treatments so the shots
    regenerate in place: per-scene shot checkpoints, assembled script, storyboard,
    object refs and rendered video. Keeps plan.json and scene_details.json (whose
    status stays approved) — the next assemble rewrites shots from the SAME
    treatments, optionally with director notes.
    """
    _clear_scenes_downstream(show, episode)


def reject_scene_details(show: Show, episode: int, notes: str = "") -> dict[str, Any]:
    data = read_scene_details(show, episode)
    data["status"] = "rejected"
    data["rejected_notes"] = notes
    _details_path(show, episode).write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                            encoding="utf-8")
    _clear_scenes_downstream(show, episode)
    return data


def _scene_runtime_target(plan_scene: dict[str, Any], total_target: int, n_scenes: int) -> int:
    if not n_scenes:
        return 90
    return max(30, total_target // n_scenes)


# Chunk 2 is a CHAIN of single-responsibility passes over one scene, not one giant
# call. Writing blocking + camera + action + references + costumes + soundscape +
# dialogue in a single prompt made the model pad the dialogue array with stage
# directions ("(Grunting)", "(Internal/Unvoiced)") to hit the line-count gate.
# Each pass is one focused LLM call with a proper 'job description'; later passes
# receive the accumulated draft and merge in ONLY their own field(s), so they can
# make the edits they need without re-inventing the structure.
_SCENE_PASSES: list[dict[str, Any]] = [
    {
        "name": "blocking",
        "job": "SCENE BLOCKER (director of staging)",
        "schema": {"shots": [{
            "id": "s01_sh01, s01_sh02, ...",
            "type": "'ref2va' if characters are on screen, else 'fl2va'",
            "importance": "'hero' for the scene's 1-2 money shots, else 'standard'",
            "duration_s": "float 4-15 (shots with dialogue ~10.125, action inserts ~5.167)",
            "beat": "one sentence: what happens in this shot and which characters are present",
        }]},
        "instructions": (
            "Break the scene into enough shots to fill ~{target_s}s of runtime "
            "(~{n_shots} shots). Fully realize every beat of the scene treatment; "
            "vary what happens across the shots instead of replaying one movement. "
            "Mark the scene's 1-2 hero money shots."),
        "cast_key": "appearance_canonical",
    },
    {
        "name": "camera",
        "job": "DIRECTOR OF PHOTOGRAPHY",
        "schema": {"shots": [{
            "id": "shot id (verbatim)",
            "camera": "framing, angle, lens feel, and movement",
        }]},
        "instructions": (
            "Describe each shot's camera: framing (wide / medium / close-up / insert), "
            "angle, lens feel, and movement (static, pan, tilt, dolly, crane, tracking, "
            "handheld). Vary coverage across the scene — never repeat the same framing "
            "on consecutive shots. Match the shot's beat."),
        "fields": ["camera"],
    },
    {
        "name": "action",
        "job": "ACTION DIRECTOR",
        "schema": {"shots": [{
            "id": "shot id (verbatim)",
            "action": "what physically happens in this shot",
        }]},
        "instructions": (
            "Describe each shot's physical action: what every character on screen does, "
            "from pose to pose, and how the objects/environment react. Keep it concrete "
            "and shot-sized — one continuous movement per shot."),
        "fields": ["action"],
    },
    {
        "name": "references",
        "job": "CHARACTER & SCENE CONTINUITY MANAGER",
        "schema": {"shots": [{
            "id": "shot id (verbatim)",
            "references": {"characters": ["EXACT cast names on screen"],
                           "scene": "location name"},
        }]},
        "instructions": (
            "For every shot, list the EXACT cast names visible on screen in "
            "references.characters (empty array if no characters are visible) and the "
            "scene's location in references.scene. Names must match the cast exactly."),
        "nested": [("references", ["characters", "scene"])],
    },
    {
        "name": "costumes",
        "job": "WARDROBE SUPERVISOR",
        "schema": {"shots": [{
            "id": "shot id (verbatim)",
            "references": {"costumes": {"CharacterName": "costume variant they wear, or omit for base"}},
        }]},
        "instructions": (
            "For every character on screen, assign the costume variant they wear in this "
            "shot (references.costumes). Omit a character to mean their base outfit. Use "
            "the same label for the same outfit across the scene; if a character changes "
            "outfits mid-scene, flag the exact shot where it happens."),
        "nested": [("references", ["costumes"])],
    },
    {
        "name": "soundscape",
        "job": "SOUND DESIGNER",
        "schema": {"shots": [{
            "id": "shot id (verbatim)",
            "soundscape": "background / environment sound",
            "music": "music cue, or 'none'",
        }]},
        "instructions": (
            "Give each shot its background soundscape (environment, weather, machinery, "
            "crowd, Foley matching the action) and its music cue — genre, tempo, "
            "emotional color, and where it swells or drops. Use 'none' for silent beats."),
        "fields": ["soundscape", "music"],
    },
    {
        "name": "dialogue",
        "job": "DIALOGUE WRITER",
        "schema": {"shots": [{
            "id": "shot id (verbatim)",
            "dialogue": [{"char": "exact cast name", "line": "spoken line",
                          "on_camera": "bool"}],
        }]},
        "instructions": (
            "Write the scene's dialogue from its dialogue_beats / narrative. Most shots "
            "that have characters on screen should carry spoken lines; roughly 70% of the "
            "scene's runtime should be spoken. Aim for ~{n_lines} total spoken lines. "
            "Every line must contain real spoken words — a leading delivery note like "
            "'(whispering)' is allowed, but the line must have actual speech after it. "
            "NEVER write a line that is only a stage direction, a grunt, or an internal "
            "note. Use only exact cast names."),
        "fields": ["dialogue"],
        "cast_key": "personality",
    },
]


def _apply_pass(draft: list[dict[str, Any]], out: dict[str, Any],
                pass_cfg: dict[str, Any]) -> None:
    """Merge ONE pass's output into the draft, keyed by shot id.

    A pass may return a subset of shots; only shot ids that already exist in the
    draft are touched, so a wayward pass can never add or drop shots. For nested
    maps (``references``) only the keys that pass owns are merged, leaving earlier
    passes' keys intact.
    """
    by_id = {s.get("id"): s for s in draft}
    for item in (out.get("shots") or []):
        if not isinstance(item, dict):
            continue
        shot = by_id.get(item.get("id"))
        if shot is None:
            continue
        for f in pass_cfg.get("fields", []):
            if f in item and item[f] is not None:
                shot[f] = item[f]
        for top, keys in pass_cfg.get("nested", []):
            nsrc = item.get(top)
            if not isinstance(nsrc, dict):
                continue
            ndst = shot.setdefault(top, {})
            for k in keys:
                if k in nsrc and nsrc[k] is not None:
                    ndst[k] = nsrc[k]


def _partial_draft(sc_dir: Path, sid: str) -> tuple[int, list[dict[str, Any]] | None]:
    """Resume point for a scene whose pass chain was interrupted.

    Returns ``(next_pass_index, draft)`` from the highest ``<sid>.p<N>.json``
    checkpoint (draft AFTER pass N). ``(0, None)`` means start from blocking.
    """
    best_i, best_f = -1, None
    for f in sc_dir.glob(f"{sid}.p*.json"):
        m = re.fullmatch(re.escape(sid) + r"\.p(\d+)\.json", f.name)
        if not m:
            continue
        i = int(m.group(1))
        if i > best_i:
            best_i, best_f = i, f
    if best_f is None:
        return 0, None
    try:
        data = json.loads(best_f.read_text(encoding="utf-8"))
        shots = data.get("shots")
        if isinstance(shots, list) and shots:
            return best_i + 1, shots
    except Exception:
        pass
    return 0, None


def generate_scene_shots(show: Show, episode: int, plan_scene: dict[str, Any],
                         llm=None, cfg=None, episode_summary: str = "",
                         notes: str = "") -> dict[str, Any]:
    """Chunk 2: write ONE scene's shots as a chain of focused passes.

    blocking -> camera -> action -> references -> costumes -> soundscape -> dialogue.
    Each pass is its own LLM call with a single job description; later passes merge
    only their own field(s) into the draft. The draft is checkpointed after every
    pass (``scenes/<sid>.p<N>.json``) so an interrupted run resumes mid-scene.
    ``notes`` (director feedback) is appended to every pass prompt.
    """
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    bible = show.read_bible()
    cast = [show.read_character(cid) for cid in show.list_characters()]
    sid = plan_scene.get("id", "s01")
    target_s = _scene_runtime_target(plan_scene, _target(show),
                                     len(read_episode_plan(show, episode).get("scenes", [])))
    system = prompts.showrunner_system(
        cfg["show_profile"], bible.get("content_policy", "mature"),
        cfg["show_profile"].get("baseline", "ranma-1-2"))
    sc_dir = _scenes_dir(show, episode)
    sc_dir.mkdir(parents=True, exist_ok=True)
    start, draft = _partial_draft(sc_dir, sid)

    for i, pass_cfg in enumerate(_SCENE_PASSES):
        if i < start:
            continue
        name = pass_cfg["name"]
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: scene {sid} — {name} pass…",
                                  "ts": time.time()}
        out = llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompts.scene_pass_prompt(
                 pass_cfg, bible, episode_summary, plan_scene, cast, draft, target_s, notes)}],
            model=model, temperature=0.8, max_tokens=32768,
            on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                "detail": f"Episode {episode}: scene {sid} — {name} ({n} tokens…)",
                "output": t[-500:], "ts": time.time()}}))
        if i == 0:
            draft = [s for s in (out.get("shots") or []) if isinstance(s, dict) and s.get("id")]
            if not draft:
                raise RuntimeError(f"scene {sid} blocking pass returned no shots")
        else:
            _apply_pass(draft, out, pass_cfg)
        (sc_dir / f"{sid}.p{i}.json").write_text(
            json.dumps({"shots": draft}, indent=2, ensure_ascii=False), encoding="utf-8")

    scene = {
        "id": sid,
        "location": plan_scene.get("location", ""),
        "time_of_day": plan_scene.get("time_of_day", ""),
        "summary": plan_scene.get("summary", ""),
        "shots": draft,
    }
    return scene


def _writer_review(show: Show, episode: int, script: dict[str, Any],
                   llm=None, cfg=None, max_revisions: int | None = None) -> dict[str, Any]:
    """Writers'-room review + revise loop for a chunked script.

    Mirrors ``WritersRoom.run`` but on an already-assembled script: reviews it
    (slop/continuity/fan_service + duration), and if it fails, asks the
    showrunner to revise it addressing every note, re-reviews, up to
    ``max_revisions``. Writes script.r<round>.json + per-reviewer notes files.
    Returns the final script.
    """
    from .clients.lmstudio import LMStudioClient
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    max_revisions = max_revisions or int(cfg.get("reviewers", "max_revisions", 2))
    bible = show.read_bible()
    cast_names = [c.get("name") for c in (show.read_character(cid) for cid in show.list_characters())
                  if c.get("name")]
    system = prompts.showrunner_system(
        cfg["show_profile"], bible.get("content_policy", "mature"),
        cfg["show_profile"].get("baseline", "ranma-1-2"))
    runs = show.dir / "runs" / f"EP{episode:02d}"
    notes_dir = runs / "reviews"
    target = _target(show)

    def _review(script: dict[str, Any], round_no: int) -> dict[str, Any]:
        reviews = run_reviewers(script, show=show, cfg=cfg, llm=llm, episode=episode,
                                round_no=round_no, notes_dir=notes_dir)
        dur = _runtime_review(script, target)
        if dur:
            reviews["duration"] = dur
        return reviews

    round_no = 1
    reviews = _review(script, round_no)
    passed = all_pass(reviews)
    while not passed and round_no < max_revisions:
        round_no += 1
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: writers' room revision (round {round_no})…",
                                  "ts": time.time()}
        revised = llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompts.revision_prompt(script, reviews)}],
            model=model, temperature=0.6, max_tokens=65536,
            on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                "detail": f"Episode {episode}: writers' room revision ({n} tokens…)",
                "output": t[-500:], "ts": time.time()}}))
        if not isinstance(revised, dict) or not revised.get("scenes"):
            break
        script = _normalize(revised, cast_names)
        (runs / f"script.r{round_no}.json").write_text(
            json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
        reviews = _review(script, round_no)
        passed = all_pass(reviews)
    return script


def assemble_episode_script(show: Show, episode: int, llm=None, cfg=None,
                            max_revisions: int | None = None,
                            notes: str = "") -> dict[str, Any]:
    """Chunk 3: run every plan scene's shot generation, assemble, normalize, write.

    Resumable: each scene's shots are checkpointed to scenes/<sid>_shots.json, so an
    interrupted run resumes from the checkpointed scenes instead of re-writing shots.
    ``notes`` (director feedback) is threaded into every shot pass.
    """
    from .clients.lmstudio import LMStudioClient
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    plan = read_episode_plan(show, episode)
    details = read_scene_details(show, episode)
    scenes = details.get("scenes", []) if details.get("status") == "approved" \
        else plan.get("scenes", [])
    ep_summary = plan.get("plot") or plan.get("summary", "")
    episodes: list[dict[str, Any]] = []
    cast_names = [c.get("name") for c in (show.read_character(cid) for cid in show.list_characters())
                  if c.get("name")]
    runs = show.dir / "runs" / f"EP{episode:02d}"
    runs.mkdir(parents=True, exist_ok=True)
    sc_dir = _scenes_dir(show, episode)
    sc_dir.mkdir(parents=True, exist_ok=True)
    for i, ps in enumerate(scenes, start=1):
        sid = ps.get("id", f"s{i:02d}")
        ck = sc_dir / f"{sid}_shots.json"
        if ck.exists():
            try:
                scene = json.loads(ck.read_text(encoding="utf-8"))
                if scene.get("id") and scene.get("shots"):
                    # The scene is complete; drop any stale per-pass checkpoints.
                    for pf in sc_dir.glob(f"{sid}.p*.json"):
                        pf.unlink(missing_ok=True)
                    episodes.append(scene)
                    continue
            except Exception:
                pass
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: writing shots for {sid}…",
                                  "ts": time.time()}
        scene = generate_scene_shots(show, episode, ps, llm=llm, cfg=cfg,
                                     episode_summary=ep_summary, notes=notes)
        for pf in sc_dir.glob(f"{sid}.p*.json"):
            pf.unlink(missing_ok=True)
        ck.write_text(json.dumps(scene, indent=2, ensure_ascii=False), encoding="utf-8")
        episodes.append(scene)
    script = {"episode": episode, "summary": ep_summary,
              "cast": plan.get("characters") or plan.get("cast") or cast_names,
              "scenes": episodes}
    script = _normalize(script, cast_names)
    (runs / "script.r1.json").write_text(json.dumps(script, indent=2, ensure_ascii=False),
                                         encoding="utf-8")
    script = _writer_review(show, episode, script, llm=llm, cfg=cfg,
                            max_revisions=max_revisions)
    return script


def runtime_report(script: dict[str, Any], show: Show) -> dict[str, Any] | None:
    return _runtime_review(script, _target(show))


def reconcile_costume_labels(show: Show, episode: int, script: dict[str, Any],
                             llm=None, cfg=None) -> dict[str, Any]:
    """After-pass: collapse per-shot costume labels onto a canonical wardrobe.

    The shot writer invents a label per shot, so one real costume becomes dozens
    of near-identical labels (e.g. "Base Armorer Garb (wet sheen)" vs
    "Base Armorer Garb (hand detail)"). This pass looks at the SHOT CONTEXT each
    label appears in and maps every label to either an existing canonical costume
    label (reuse verbatim), the base outfit (drop transient variants like
    wet/bloody/posed), or a NEW canonical label created ONLY when the scene
    genuinely requires a distinct outfit.

    Known canonical labels come from the character's refs.json ``variants``.
    Rewrites ``script.scenes[].shots[].references.costumes`` IN PLACE and
    persists the reconciled script as a new revision. Returns the script.
    """
    from .clients.lmstudio import LMStudioClient
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")

    # Gather distinct labels per character WITH shot context (scene summary/action).
    cand: dict[str, dict[str, dict[str, Any]]] = {}   # name -> label -> {count, example_shots}
    for sc in script.get("scenes", []):
        loc = sc.get("location", "")
        summary = sc.get("summary", "")
        for shot in sc.get("shots", []):
            refs = (shot.get("references") or {})
            costumes = (refs.get("costumes") or {}) or {}
            for name, label in costumes.items():
                if not label or str(label).strip().lower() == "base":
                    continue
                label = str(label).strip()
                ctx = " ".join(x for x in (loc, summary, shot.get("action", "")) if x)
                entry = cand.setdefault(name, {}).setdefault(
                    label, {"count": 0, "example_shots": []})
                entry["count"] += 1
                if len(entry["example_shots"]) < 2 and ctx:
                    entry["example_shots"].append(ctx[:220])

    # Known canonical wardrobe per character = existing refs.json variants.
    known: dict[str, list[str]] = {}
    for cid in show.list_characters():
        c = show.read_character(cid)
        name = c.get("name")
        if not name or name not in cand:
            continue
        rj = show.character_refs_dir(cid) / "refs.json"
        variants = []
        if rj.exists():
            try:
                variants = [k for k in (json.loads(rj.read_text(encoding="utf-8"))
                            .get("variants") or {}).keys() if k != "base"]
            except Exception:
                variants = []
        known[name] = variants

    mapping: dict[tuple[str, str], tuple[str, str | None]] = {}
    failed = False
    for name, labels in cand.items():
        char = next((show.read_character(cid) for cid in show.list_characters()
                     if (show.read_character(cid).get("name") or "") == name), {})
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: reconciling {name}'s costumes…",
                                  "ts": time.time()}
        # Deterministic guards: transient labels (wet/bloody/posed/framing) and
        # base-outfit labels ("Base Armor Suit") are dropped to base no matter
        # what the LLM says — transient states are not costumes, and the base
        # outfit already has its reference image. Exclude them from the
        # candidates so the LLM is never asked to classify them.
        candidates = []
        for lb, d in list(labels.items()):
            if is_transient_costume_label(lb) or is_base_outfit_label(lb):
                mapping[(name, lb)] = ("drop", None)
                labels.pop(lb)
            else:
                candidates.append({"label": lb, **d})
        if not candidates:
            continue
        try:
            out = llm.chat_json(
                [{"role": "system", "content": prompts.showrunner_system(
                    cfg["show_profile"], (show.read_bible() or {}).get("content_policy", "mature"),
                    cfg["show_profile"].get("baseline", "ranma-1-2"))},
                 {"role": "user", "content": prompts.costume_reconcile_prompt(
                     char, known.get(name, []), candidates)}],
                model=model, temperature=0.2, max_tokens=8192,
                on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                    "detail": f"Episode {episode}: reconciling {name}'s costumes ({n} tokens…)",
                    "output": t[-500:], "ts": time.time()}}))
        except Exception as exc:
            # A failed call must not silently let every label through to the
            # ref generator. Log it; the labels stay unmapped (kept) but the
            # transient guard above already dropped the wasteful ones.
            log.warning("costume reconcile LLM failed for %s/%s EP%02d: %s",
                        show.show_id, name, episode, exc)
            failed = True
            continue
        for d in (out.get("labels") or []):
            src = str(d.get("label", "")).strip()
            if not src:
                continue
            action = str(d.get("action", "")).strip().lower()
            tgt = (str(d.get("target", "")).strip() or None) if d.get("target") else None
            if action == "create" and tgt:
                mapping[(name, src)] = ("create", tgt)
            elif action == "reuse" and tgt:
                mapping[(name, src)] = ("reuse", tgt)
            else:
                mapping[(name, src)] = ("drop", None)

    # Apply the mapping in place.
    changed = 0
    for sc in script.get("scenes", []):
        for shot in sc.get("shots", []):
            refs = (shot.get("references") or {})
            costumes = (refs.get("costumes") or {}) or {}
            new_costs: dict[str, str] = {}
            for name, label in costumes.items():
                if not label or str(label).strip().lower() == "base":
                    continue
                label = str(label).strip()
                action, tgt = mapping.get((name, label), ("keep", None))
                if action == "drop":
                    changed += 1
                    continue                      # character stays in base outfit
                if action in ("create", "reuse") and tgt:
                    if tgt != label:
                        changed += 1
                    new_costs[name] = tgt
                else:
                    new_costs[name] = label
            if refs.get("costumes"):
                refs["costumes"] = new_costs
    if not failed:
        script["_costume_reconciled"] = True
        runs = show.dir / "runs" / f"EP{episode:02d}"
        rounds = []
        for p in runs.glob("script.r*.json"):
            tail = p.stem.rsplit(".r", 1)[-1]
            if tail.isdigit():
                rounds.append(int(tail))
        nxt = (max(rounds) if rounds else 0) + 1
        (runs / f"script.r{nxt}.json").write_text(
            json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
    return script
