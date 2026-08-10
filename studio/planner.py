"""Chunked episode generation: plan -> per-scene shots -> assemble.

Keeps each LLM call's context LOCALIZED so a full-length episode is achievable:
  1. PLAN  — one call: episode summary + scene-by-scene blueprint (approval gate).
  2. SHOTS — one call PER SCENE (bible + plan scene + cast only) -> that scene's shots.
  3. ASSEMBLE — combine scene shots into the episode script, runtime-check, write.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import prompts
from .bootstrap import ACTIVITY
from .config import get_config
from .review import all_pass, run_reviewers
from .scriptgen import _normalize, _runtime_review
from .show import Show

PLAN_FILE = "plan.json"


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
    synopsis = develop_episode(show, episode, llm=llm, cfg=cfg, notes=notes)
    state = state_tracker_data(show, episode)
    current_plan = None
    if notes.strip():
        prior = read_episode_plan(show, episode)
        current_plan = {k: v for k, v in prior.items()
                        if k not in ("status", "rejected_notes", "synopsis")} or None
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
    plan["status"] = "pending"
    plan["synopsis"] = synopsis
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


def approve_plan(show: Show, episode: int) -> dict[str, Any]:
    plan = read_episode_plan(show, episode)
    plan["status"] = "approved"
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


def reject_scene_details(show: Show, episode: int, notes: str = "") -> dict[str, Any]:
    data = read_scene_details(show, episode)
    data["status"] = "rejected"
    data["rejected_notes"] = notes
    _details_path(show, episode).write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                            encoding="utf-8")
    return data


def _scene_runtime_target(plan_scene: dict[str, Any], total_target: int, n_scenes: int) -> int:
    if not n_scenes:
        return 90
    return max(30, total_target // n_scenes)


def generate_scene_shots(show: Show, episode: int, plan_scene: dict[str, Any],
                         llm=None, cfg=None, episode_summary: str = "") -> dict[str, Any]:
    """Chunk 2: write ONE scene's shots with localized context."""
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    bible = show.read_bible()
    cast = [show.read_character(cid) for cid in show.list_characters()]
    sid = plan_scene.get("id", "s01")
    target_s = _scene_runtime_target(plan_scene, _target(show),
                                     len(read_episode_plan(show, episode).get("scenes", [])))
    result = llm.chat_json(
        [{"role": "system", "content": prompts.showrunner_system(
            cfg["show_profile"], bible.get("content_policy", "mature"),
            cfg["show_profile"].get("baseline", "ranma-1-2"))},
         {"role": "user", "content": prompts.scene_shots_prompt(
             bible, episode_summary, plan_scene, cast, target_s)}],
        model=model, temperature=0.8, max_tokens=32768,
        on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
            "detail": f"Episode {episode}: scene {sid} shots ({n} tokens…)",
            "output": t[-500:], "ts": time.time()}}))
    scene = {
        "id": sid,
        "location": plan_scene.get("location", ""),
        "time_of_day": plan_scene.get("time_of_day", ""),
        "summary": plan_scene.get("summary", ""),
        "shots": result.get("shots", []) or [],
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
                            max_revisions: int | None = None) -> dict[str, Any]:
    """Chunk 3: run every plan scene's shot generation, assemble, normalize, write.

    Resumable: each scene's shots are checkpointed to scenes/<sid>_shots.json, so an
    interrupted run resumes from the checkpointed scenes instead of re-writing shots.
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
                    episodes.append(scene)
                    continue
            except Exception:
                pass
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: writing shots for {sid}…",
                                  "ts": time.time()}
        scene = generate_scene_shots(show, episode, ps, llm=llm, cfg=cfg,
                                     episode_summary=ep_summary)
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
