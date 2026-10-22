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

from . import promptlog
from . import epstate
from . import prompts
from .bootstrap import ACTIVITY
from .config import DEFAULT_RUNTIME_TARGET_S, get_config
from .review import (all_pass, failing_reviews, plan_passes, review_notes_text,
                     repetition_review, run_plan_reviewers,
                     run_plan_room_reviewers, run_reviewers)
from .scriptgen import _normalize, _runtime_review
from .show import Show

PLAN_FILE = "plan.json"
# Episode dialogue comes from the reviewed prose screenplay: write_prose_dialogue_spine
# parses it into per-scene ordered lines (dialogue.json) plus the readable
# dialogue.txt, and the shot blocker distributes them verbatim.
DIALOGUE_FILE = "dialogue.json"
DIALOGUE_TXT = "dialogue.txt"

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
    return int((show.read_bible() or {}).get("runtime_target_s", DEFAULT_RUNTIME_TARGET_S) or DEFAULT_RUNTIME_TARGET_S)


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
    text = promptlog.logged_chat(
        llm, show, 0, "story-engine",
        [{"role": "system", "content": prompts.showrunner_system(
            show.style_profile(cfg), bible.get("content_policy", "mature"),
            show.style_profile(cfg).get("baseline", ""))},
         {"role": "user", "content": _p.story_engine_architect_prompt(bible)}],
        model=model, role="showrunner", temperature=0.6, max_tokens=16384,
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
    # Dangling GENERATED_* placeholders were never data-driven — leaving them
    # literal makes the model fill in tokens and bloat/truncate the output. Strip
    # them deterministically so the prompt never carries unfilled braces.
    import re as _re
    out = _re.sub(r"\{\{\s*GENERATED_[A-Z_]+?\s*\}\}", "", out)
    return out


def state_tracker_data(show: Show, episode: int,
                       episode_type: str = "") -> dict[str, Any]:
    """Build the <state_tracker> + <episode_history> payload for the outline prompt.

    When ``episode_type`` is 'character', battle/combat plotlines are excluded from
    the active set so the outline prompt cannot re-index on them (the recurring
    threat thread problem). Battle episodes keep them available.
    """
    from .development import _plotline_state
    cfg = get_config()
    battle_kinds = set(cfg.get("growth", "battle_plotline_kinds", ["battle"]))
    bible = show.read_bible()
    continuity = show.read_continuity()
    plotlines = _plotline_state(show, bible)
    if str(episode_type or "").lower() == "character":
        plotlines = [p for p in plotlines if p.get("kind") not in battle_kinds]
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
        epstate.write_json(_director_notes_path(show), notes)
    return notes


class _ModelOverride:
    """Wrap an LLM client so outline-WRITING calls use a chosen model, while
    reviewer calls keep their own cheap role models.

    A one-off outline regeneration picks a model; every non-reviewer call (season,
    development, skeleton, beats) is forced onto it. Reviewer calls (role ending
    ``_reviewer``) pass through untouched, so a regeneration never bills the
    chosen model for the whole writers' room.
    """

    def __init__(self, client, model: str | None = None,
                 remote_model: str | None = None, prefer_local: bool = False):
        self._client = client
        self._model = model
        self._remote_model = remote_model
        self._prefer_local = prefer_local

    def _apply(self, kw: dict[str, Any]) -> dict[str, Any]:
        if str(kw.get("role") or "").endswith("_reviewer"):
            return kw
        if self._model:
            kw["model"] = self._model
        if self._remote_model:
            kw["remote_model"] = self._remote_model
        if self._prefer_local:
            kw["prefer_local"] = True
        return kw

    def chat(self, messages, **kw):
        return self._client.chat(messages, **self._apply(kw))

    def chat_json(self, messages, **kw):
        return self._client.chat_json(messages, **self._apply(kw))

    def __getattr__(self, name):
        return getattr(self._client, name)


def _cast_flaws_block(show: Show) -> str:
    """Each character's role + FLAW/MOTIVATION, as a prompt block.

    The stakes/agency reviewers fail outlines by citing these sheets, but the
    outline generator only saw the bible — so it invented generic, out-of-character
    beats. Shared by the draft AND the revision so neither path loses the people.
    """
    try:
        lines = []
        for cid in show.list_characters():
            c = show.read_character(cid)
            nm = c.get("name")
            if not nm:
                continue
            pers = c.get("personality") or []
            pers = (", ".join(str(x) for x in pers) if isinstance(pers, list)
                    else str(pers))
            lines.append(f"- {nm} ({c.get('role', '')}): {pers[:220]}")
    except Exception:
        return ""
    if not lines:
        return ""
    return ("\n\nCAST FLAWS/MOTIVATIONS (who they are; every beat MUST respect "
            "these, and each action must come from the character who wants it):\n"
            + "\n".join(lines))


def generate_episode_plan(show: Show, episode: int, llm=None, cfg=None,
                          notes: str = "") -> dict[str, Any]:
    """Chunk 1: produce the episode plan (summary + scene blueprint). Writes plan.json.

    SEASON-FIRST: ensures the covering season plan exists (auto-generates it
    hands-free on first use), then development expands that season entry.
    The season entry is strict canon carried on the plan as ``_season_entry``.
    """
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
    # (Re)generating the outline invalidates everything below it: a script built
    # from a previous — or a missing/stale — outline must not survive. Stop any
    # running render and drop the orphaned script/storyboard/video/dialogue. On a
    # clean first generation there is nothing downstream, so this is a no-op.
    if (read_episode_plan(show, episode).get("status") or "") != "approved":
        _clear_scenes_downstream(show, episode)
    # Season-first: the season file is the canon above this episode. Generate it
    # hands-free when this is the first episode to need it.
    season_entry: dict[str, Any] = {}
    season_no: int | None = None
    try:
        from .season import ensure_season
        season_no, season_entry = ensure_season(show, episode, llm=llm, cfg=cfg)
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "season ensure failed for %s EP%02d: %s", show.show_id, episode, exc)
    dev = develop_episode(show, episode, llm=llm, cfg=cfg, notes=notes)
    synopsis = dev.get("synopsis", "") if isinstance(dev, dict) else str(dev or "")
    # What this episode features is decided at development time but must only be
    # COMMITTED to continuity/bible when the plan is approved — otherwise every
    # rejected/regenerated episode re-grows the same plotlines. Carry it on the
    # plan so approve_plan can commit it.
    plan_dev = {"featured": (dev or {}).get("featured", []),
                "new_plotline": (dev or {}).get("new_plotline"),
                "episode_type": (dev or {}).get("episode_type", ""),
                "arc_lengths": (dev or {}).get("arc_lengths", {}),
                "new_arc_length": (dev or {}).get("new_arc_length"),
                "season": (dev or {}).get("season", season_no)}
    # Strict canon: refresh the entry from what development actually used
    # (develop_episode re-reads the season file, so prefer its season number;
    # fall back to the entry ensure_season returned).
    if not season_entry:
        try:
            from .season import read_season_entry
            _sno, _ent = read_season_entry(show, episode, cfg)
            if _ent:
                season_no, season_entry = _sno, _ent
        except Exception:
            pass
    season_mandate = ""
    if season_entry:
        season_mandate = (
            "\nSEASON CANON (STRICT — this episode's high-level plot points from "
            f"S{int(season_no or 0):02d}; the outline must realize ALL of them, "
            "in order, and invent nothing that contradicts them):\n"
            f"{json.dumps(season_entry, ensure_ascii=False)}\n")
    state = state_tracker_data(show, episode,
                               episode_type=(dev or {}).get("episode_type", ""))
    current_plan = None
    if notes.strip():
        prior = read_episode_plan(show, episode)
        current_plan = {k: v for k, v in prior.items()
                        if k not in ("status", "rejected_notes", "synopsis",
                                     "_plan_dev", "_season")} or None
    # Durable director constraints shape the outline directly.
    dir_notes = read_director_notes(show)

    system = prompts.showrunner_system(
        show.style_profile(cfg), bible.get("content_policy", "mature"),
        show.style_profile(cfg).get("baseline", ""))

    # Canonical locations: hand the outline the scene registry so every scene
    # names a recurring place (a stable location_id), instead of inventing a
    # free-text spot that no reference image is attached to.
    from . import scenes as _scenes
    _registry = _scenes.registry_block(show)

    if current_plan and notes.strip():
        # Revision path: rewrite the rejected outline in one call from the notes.
        user_prompt = prompts.episode_plan_prompt(
            bible, synopsis, continuity, names, _target(show), current_plan, notes,
            dir_notes, state, fixed_episode_type=plan_dev.get("episode_type", ""),
            scene_registry=_registry)
        if season_mandate:
            user_prompt += season_mandate
        plan = promptlog.logged_chat_json(
            llm, show, episode, "plan-revise",
            [{"role": "system", "content": system},
             {"role": "user", "content": user_prompt}],
            model=model, role="showrunner", temperature=0.6, max_tokens=32768,
            on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                "detail": f"Episode {episode}: revising plan ({n} tokens…)",
                "output": t[-500:], "ts": time.time()}}))
    else:
        # CHUNKED create path: Phase 1 skeleton (one call), Phase 2 beats (one
        # call per batch). This lets a scene-count-driven outline fit the model's
        # output budget where a single huge JSON would truncate.
        beats_batch = max(1, int(cfg.get("pipeline", "plan_beats_batch", 2) or 2))
        from . import service as _service
        _reward = _service.is_reward_episode(show, episode, cfg)
        skeleton_prompt = prompts.plan_skeleton_prompt(
            bible, state, dev, dir_notes, _target(show), continuity=continuity,
            reward=_reward, scene_registry=_registry)
        # Feed each character's FLAW/MOTIVATION so the outline's beats honour them —
        # the stakes/agency reviewers cite these sheets, but the generator (which only
        # saw the bible) did not, producing interchangeable beats.
        skeleton_prompt += _cast_flaws_block(show)
        if season_mandate:
            skeleton_prompt += season_mandate
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: outlining scenes (skeleton)…",
                                  "ts": time.time()}
        skeleton = promptlog.logged_chat_json(
            llm, show, episode, "plan-skeleton",
            [{"role": "system", "content": system},
             {"role": "user", "content": skeleton_prompt}],
            model=model, role="showrunner", temperature=0.6, max_tokens=32768,
            on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                "detail": f"Episode {episode}: scene skeleton ({n} tokens…)",
                "output": t[-500:], "ts": time.time()}}))
        plan = dict(skeleton or {})
        plan_scenes = [s for s in (plan.get("scenes") or [])
                       if isinstance(s, dict) and s.get("id")]
        if not plan_scenes:
            raise RuntimeError(f"episode {episode} skeleton returned no scenes")
        # Phase 2: expand beats in batches.
        expanded: dict[str, dict[str, Any]] = {}
        for i in range(0, len(plan_scenes), beats_batch):
            batch = plan_scenes[i:i + beats_batch]
            ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: writing beats "
                                       f"({batch[0].get('id')}..{batch[-1].get('id')})…",
                                      "ts": time.time()}
            out = promptlog.logged_chat_json(
                llm, show, episode,
                f"plan-beats-{batch[0].get('id', '?')}..{batch[-1].get('id', '?')}",
                [{"role": "system", "content": system},
                 {"role": "user", "content": prompts.plan_beats_prompt(
                     bible, batch, dev, dir_notes, scene_registry=_registry)}],
                model=model, role="showrunner", temperature=0.6, max_tokens=32768,
                on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                    "detail": f"Episode {episode}: scene beats ({n} tokens…)",
                    "output": t[-500:], "ts": time.time()}}))
            for sc in (out or {}).get("scenes", []):
                if isinstance(sc, dict) and sc.get("id"):
                    expanded[sc["id"]] = sc
        merged = []
        for sc in plan_scenes:
            scid = sc.get("id", "")
            full = expanded.get(scid) or dict(sc)
            # The beats pass returns its own scene dicts; keep the skeleton's
            # canonical location_id / location_setting when the beats pass dropped
            # or blanked them.
            if sc.get("location_id") and not full.get("location_id"):
                full["location_id"] = sc["location_id"]
            if sc.get("location_setting") and not full.get("location_setting"):
                full["location_setting"] = sc["location_setting"]
            merged.append(full)
        plan["scenes"] = merged
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
    # A character episode must not carry a city-level threat: force the threat
    # field to 'none' so the outline and downstream stages never re-index on it.
    ep_type = str(plan.get("episode_type") or "").lower()
    if ep_type == "character":
        plan["threat_of_the_week"] = "none"
    # Plan-level structural review: review the outline itself (plot vs synopsis vs
    # scenes, cold-open-without-context for episode 1, resolved threat, distinct
    # climax). If it fails, revise the outline with the reviewer notes as ABSOLUTE
    # feedback and re-review, up to plan_max_revisions. The outcome is recorded on
    # the plan as plan_review so approve_plan can refuse an outline that never
    # cleared review.
    plan = _plan_review_loop(show, episode, plan, llm, cfg, model, system,
                             bible, synopsis, continuity, names, state, dir_notes,
                             fixed_episode_type=plan_dev.get("episode_type", ""),
                             rejection_notes=notes)
    # Canonicalize each scene's location onto the registry (the final, reviewed
    # scene list can differ from the skeleton the LLM first labelled).
    _canonicalize_locations(show, plan)
    plan["status"] = "pending"
    plan["synopsis"] = synopsis
    # Deferred plotline commit: carry what this episode featured so approve_plan
    # can stamp continuity/bible ONLY on approval (never on a rejected/regenerated
    # outline). Stripped from the stored plan by approve_plan.
    plan["_plan_dev"] = plan_dev
    # Season canon carried on the plan so approval can enforce it.
    if season_entry:
        plan["_season"] = {"season": season_no, "entry": season_entry}
    epstate.save_plan(show, episode, plan)
    return plan


def _canonicalize_locations(show: Show, plan: dict[str, Any]) -> dict[str, Any]:
    """Stamp a canonical ``location_id`` on every plan scene.

    The outline writer names a free-text ``location``; only a registry id carries
    a location reference image. Accept the LLM's ``location_id`` when it names a
    real registry entry; otherwise resolve it from the free-text location with
    the same confident matcher; otherwise leave it empty (the renderer then falls
    back to the scene's own first-preview frame as its grounding image). Never
    invents a new registry entry — no side effects on a draft outline.
    """
    try:
        from . import scenes as _scenes
        registry = set(show.list_scenes() or [])
        for sc in plan.get("scenes", []) or []:
            if not isinstance(sc, dict):
                continue
            lid = str(sc.get("location_id") or "").strip()
            if lid not in registry:
                lid = _scenes.resolve_slug(show, sc.get("location") or "") or ""
            sc["location_id"] = lid
    except Exception:
        pass
    return plan


def _describe_new_locations(show: Show, plan: dict[str, Any],
                            pending: list[dict[str, Any]], llm, cfg) -> dict[str, dict[str, Any]]:
    """One LLM pass: canonical description + Krea 2 setting prompt per new location."""
    out: dict[str, dict[str, Any]] = {}
    locs = [{"location": str(sc.get("location") or ""),
             "summary": str(sc.get("summary") or ""),
             "beats": sc.get("beats") or []} for sc in pending]
    try:
        from .clients.lmstudio import LMStudioClient
        cfg = cfg or get_config()
        llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=180)
        model = (cfg.get("llm", "roles", {}) or {}).get("script") or cfg.get("llm", "model")
        res = llm.chat_json(
            [{"role": "user", "content": prompts.new_locations_prompt(show.read_bible(), locs)}],
            model=model, role="showrunner", temperature=0.5, max_tokens=2000)
        for item in (res or {}).get("locations", []) or []:
            if isinstance(item, dict) and item.get("location"):
                out[str(item["location"]).strip()] = item
    except Exception:
        pass
    return out


def _register_plan_locations(show: Show, plan: dict[str, Any], llm=None, cfg=None) -> list[str]:
    """Register locations new to the series; called at plan APPROVAL.

    The commit boundary: every scene must end up with a registry ``location_id``.
    Scenes that match an existing entry reuse it; genuinely-new places are added
    to the registry (canonical description + Krea 2 setting fragment) and their
    location reference image is generated in the background. Best-effort — a
    failure never blocks approval. Returns the newly-registered ids.
    """
    new_ids: list[str] = []
    try:
        from . import scenes as _scenes
        registry = set(show.list_scenes() or [])
        pending: list[dict[str, Any]] = []
        for sc in plan.get("scenes", []) or []:
            if not isinstance(sc, dict):
                continue
            name = str(sc.get("location") or "").strip()
            lid = str(sc.get("location_id") or "").strip()
            # The free-text `location` is the SPECIFIC set the scene is in; it
            # wins over a broader `location_id` the outline may have chosen. If it
            # already resolves to a registry entry, reuse it; otherwise register
            # it as its own top-level place (below).
            resolved = _scenes.resolve_slug(show, name) if name else None
            if resolved:
                sc["location_id"] = resolved
                continue
            if name:
                pending.append(sc)
            elif lid in registry:
                sc["location_id"] = lid
        if not pending:
            return new_ids
        briefs = _describe_new_locations(show, plan, pending, llm, cfg)
        for sc in pending:
            name = str(sc.get("location") or "").strip()
            b = briefs.get(name) or {}
            desc = str(b.get("description") or "").strip() or name
            setting = (str(sc.get("location_setting") or "").strip()
                       or str(b.get("setting_prompt") or "").strip())
            sid = _scenes.register_location(show, name, description=desc,
                                            setting_prompt=setting)
            if sid not in registry:
                registry.add(sid)
                new_ids.append(sid)
            sc["location_id"] = sid
    except Exception:
        pass
    return new_ids


def read_episode_plan(show: Show, episode: int) -> dict[str, Any]:
    p = _plan_path(show, episode)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _plan_review_feedback(reviews: dict[str, dict[str, Any]]) -> str:
    """The failing outline reviewers' free-form reviews, as revision feedback."""
    return review_notes_text(reviews)


def _plan_review_loop(show: Show, episode: int, plan: dict[str, Any], llm, cfg,
                      model: str, system: str, bible: dict[str, Any],
                      synopsis: str, continuity: dict[str, Any],
                      names: list[str], state: dict[str, Any],
                      dir_notes: list[str],
                      fixed_episode_type: str = "",
                      rejection_notes: str = "") -> dict[str, Any]:
    """Review the episode outline and revise it up to plan_max_revisions.

    Mirrors ``_writer_review`` but on the OUTLINE: the structure reviewer checks
    plot-vs-synopsis-vs-scenes coherence and episode-1 context, and a failing
    outline is revised with the reviewer notes as ABSOLUTE feedback. The final
    verdict is stored on the plan as ``plan_review`` so approval gates can refuse
    an outline that never cleared review.

    ``fixed_episode_type`` (when set) is forced onto every draft and every
    revision: development decides the episode type deterministically (battle
    cadence), and the outline must NOT flip it back to battle/character on
    revision.

    ``rejection_notes`` (when set) is the human's rejection feedback. It is
    carried as a STANDING ABSOLUTE constraint into every review revision so the
    reviewer loop cannot silently undo what the human asked for (e.g. "don't
    meet until scene 6").

    Season canon is also carried into every review revision so the loop cannot
    drift off the season entry.
    """
    # Convergence: at most ONE auto-revision, then stop and surface to the human.
    # And only the 3 blocking reviewers gate the outline (advisory ones recorded).
    max_revisions = min(int(cfg.get("reviewers", "plan_max_revisions", 2) or 2), 2)
    notes_dir = show.dir / "runs" / f"EP{episode:02d}" / "reviews"
    try:
        from .season import read_season_entry as _read_entry
        _sno, _sentry = _read_entry(show, episode)
        _season_mandate = (
            "\nSEASON CANON (STRICT — realize ALL of these beats in order):\n"
            f"{json.dumps(_sentry, ensure_ascii=False)}\n") if _sentry else ""
    except Exception:
        _season_mandate = ""

    def _review(plan: dict[str, Any], round_no: int) -> dict[str, dict[str, Any]]:
        # 1) the plan-specific structure reviewer (coherence, episode-1 context),
        # then 2) the WRITERS' ROOM on the outline itself. The outline decides
        # length, locations, fan-service placement and beat shape before any
        # downstream writer sees it, so the room that guards those must review the
        # OUTLINE - not only the finished script, where the outline is already locked.
        reviews = run_plan_reviewers(plan, show=show, cfg=cfg, llm=llm, episode=episode,
                                     round_no=round_no, notes_dir=notes_dir)
        try:
            room = run_plan_room_reviewers(plan, show=show, cfg=cfg, llm=llm,
                                           episode=episode, round_no=round_no,
                                           notes_dir=notes_dir)
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "plan room review failed EP%02d r%d: %s", episode, round_no, exc)
            room = {}
        reviews.update(room)
        return reviews

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
        if "episode_type" in plan:
            p.setdefault("episode_type", plan["episode_type"])
        # The episode type is decided deterministically by development — force it.
        if fixed_episode_type:
            p["episode_type"] = fixed_episode_type
        # Character episodes never carry a city-level threat.
        if str(p.get("episode_type") or "").lower() == "character":
            p["threat_of_the_week"] = "none"
        return p

    round_no = 1
    reviews = _review(plan, round_no)
    passed = plan_passes(reviews)
    while not passed and round_no < max_revisions:
        round_no += 1
        feedback = _plan_review_feedback(reviews)
        if rejection_notes.strip():
            feedback = ("HUMAN REJECTION FEEDBACK (ABSOLUTE — the single most "
                        "important instruction; the outline MUST satisfy it and "
                        "the reviewer notes below must NOT contradict or undo it):\n"
                        f"{rejection_notes}\n\n" + feedback)
        ACTIVITY[show.show_id] = {
            "detail": f"Episode {episode}: revising outline from plan review (round {round_no})…",
            "ts": time.time()}
        from . import service as _service
        from . import scenes as _scenes
        _rev_prompt = prompts.episode_plan_prompt(
            bible, synopsis, continuity, names, _target(show), plan, feedback,
            dir_notes, state, fixed_episode_type=fixed_episode_type,
            cast_brief=_cast_flaws_block(show),
            reward=_service.is_reward_episode(show, episode, cfg),
            scene_registry=_scenes.registry_block(show))
        if _season_mandate:
            _rev_prompt += _season_mandate
        revised = promptlog.logged_chat_json(
            llm, show, episode, f"plan-outline-revise-r{round_no}",
            [{"role": "system", "content": system},
             {"role": "user", "content": _rev_prompt}],
            model=model, role="showrunner", temperature=0.6, max_tokens=32768,
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
        "reviews": {r: {"grade": v.get("grade"), "review": v.get("review", "")}
                    for r, v in reviews.items()},
    }
    return plan


def approve_plan(show: Show, episode: int, llm=None, cfg=None, override: bool = False) -> dict[str, Any]:
    plan = read_episode_plan(show, episode)
    # Never rubber-stamp an outline that failed structural review — the fix must
    # land before approval (either it passed review or a human regenerated it).
    # ``override=True`` is the explicit human override for a reviewer-rejected
    # outline; it records the override reason so the approval is auditable.
    review = (plan or {}).get("plan_review") or {}
    if review.get("passed") is False and not override:
        reviews = review.get("reviews") or {}
        notes = "; ".join(str(v.get("review") or "").strip()
                          for v in reviews.values()
                          if str(v.get("grade", "")).lower() != "pass").strip() \
            or "failed structural review"
        raise ValueError(
            f"plan EP{episode:02d} failed structural review ({review.get('rounds', 1)} "
            f"rounds): {notes} — reject it with notes to regenerate, or approve with "
            "override=True to override the reviewer.")
    # Strict season canon: the outline must realize its season entry (same
    # episode type). A mismatch means the outline drifted off-canon — reject
    # with notes to regenerate, unless the human explicitly overrides.
    season_block = _season_mismatch(show, episode, plan)
    if season_block and not override:
        raise ValueError(season_block + " — reject it with notes to regenerate, "
                         "or approve with override=True to override the season.")
    plan["status"] = "approved"
    if override:
        plan["approved_override"] = True
    # Register any location new to the series BEFORE persisting the approved plan,
    # so every scene ships a canonical location_id (and the new place gains a
    # reference image). Idempotent: an already-registered location is reused.
    try:
        _register_plan_locations(show, plan, llm=llm, cfg=cfg)
    except Exception as _exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "location registration failed for %s EP%02d (plan still approved): %s",
            show.show_id, episode, _exc)
    # Commit the plotline bookkeeping NOW (this is the real approval gate): stamp
    # last_seen on featured plotlines and persist a reviewed new plotline to the
    # bible + continuity. Development only carried it on the plan, so a rejected
    # or deleted episode never mutated continuity.
    dev = (plan or {}).get("_plan_dev") or {}
    try:
        from .growth import commit_plotline_on_approval
        commit_plotline_on_approval(show, dev.get("featured", []),
                                    dev.get("new_plotline"), episode, llm=llm, cfg=cfg,
                                    episode_type=dev.get("episode_type", ""),
                                    arc_lengths=dev.get("arc_lengths", {}),
                                    new_arc_length=dev.get("new_arc_length"))
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "plotline commit failed for %s EP%02d (plan still approved): %s",
            show.show_id, episode, exc)
    # The plan payload is downstream data; don't persist the internal dev marker.
    plan.pop("_plan_dev", None)
    plan.pop("_season", None)
    epstate.save_plan(show, episode, plan)
    return plan


def _season_mismatch(show: Show, episode: int, plan: dict[str, Any]) -> str:
    """Return an error string when the outline contradicts its season entry."""
    entry: dict[str, Any] = {}
    carried = (plan or {}).get("_season") or {}
    if isinstance(carried.get("entry"), dict) and carried["entry"].get("episode") == episode:
        entry = carried["entry"]
    else:
        try:
            from .season import read_season_entry
            _, entry = read_season_entry(show, episode)
        except Exception:
            entry = {}
    if not entry:
        return ""
    problems: list[str] = []
    want_type = str(entry.get("episode_type", "")).lower()
    got_type = str((plan or {}).get("episode_type", "")).lower()
    if want_type and got_type and want_type != got_type:
        problems.append(f"episode_type is '{got_type}' but season mandates '{want_type}'")
    want_feats = {f for f in (entry.get("featured_plotlines", []) or []) if isinstance(f, str)}
    if want_feats:
        plot_txt = json.dumps((plan or {}).get("plot", ""))
        missing = [f for f in want_feats
                   if f.lower().replace("-", " ") not in plot_txt.lower()
                   and f.lower() not in plot_txt.lower()]
        # Only hard-fail on type drift; missing featured ids are advisory
        # (the plot prose rarely names ids verbatim). Kept for future strictness.
        _ = missing
    if problems:
        return (f"plan EP{episode:02d} contradicts its season entry "
                f"(S{int((carried or {}).get('season', 0) or 0):02d}): "
                + "; ".join(problems))
    return ""


def reject_plan(show: Show, episode: int, notes: str = "") -> dict[str, Any]:
    plan = read_episode_plan(show, episode)
    plan["status"] = "rejected"
    plan["rejected_notes"] = notes
    epstate.save_plan(show, episode, plan)
    # A rejected outline invalidates everything below it: stop any running render
    # and drop the stale script/storyboard/video/dialogue built from the old plan.
    _clear_scenes_downstream(show, episode)
    # A rejection note is a durable director constraint: it must survive past this
    # regeneration and shape future episodes too. Persist it (deduped) so the
    # outline prompt carries it as ABSOLUTE.
    if notes.strip():
        add_director_note(show, notes)
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
            breakdown = promptlog.logged_chat_json(
                llm, show, episode, "scene-breakdown",
                [{"role": "system", "content": prompts.showrunner_system(
                    show.style_profile(cfg), bible.get("content_policy", "mature"),
                    show.style_profile(cfg).get("baseline", ""))},
                 {"role": "user", "content": prompts.scene_breakdown_prompt(
                     bible, outline, cast, _target(show))}],
                model=model, role="showrunner", temperature=0.8, max_tokens=8192,
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
    # Dialogue DEPTH target for the AUTHORED spine: aim the whole episode at
    # 3000-4500 spoken words (~170 wpm), split evenly across its scenes.
    _target_s = _target(show)
    _ep_words = int((_target_s / 60) * 170)
    _dbt = max(6, round(_ep_words / max(1, len(scenes)) / 12))

    details: list[dict[str, Any]] = []
    for i, ps in enumerate(scenes, start=1):
        sid = ps.get("id", f"s{i:02d}")
        if not notes.strip() and sid in existing:
            details.append(existing[sid])
            continue
        current = prior_by_id.get(sid)
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: scene detail {sid}…",
                                  "ts": time.time()}
        # Durable director constraints shape every treatment, not just revisions:
        # learned HIGHs (single-spike canon, no repeated payoffs, service pacing)
        # must prevent the next rejection at draft time.
        dir_notes = read_director_notes(show)
        detail = promptlog.logged_chat_json(
            llm, show, episode, f"scene-detail-{sid}",
            [{"role": "system", "content": prompts.showrunner_system(
                show.style_profile(cfg), bible.get("content_policy", "mature"),
                show.style_profile(cfg).get("baseline", ""))},
             {"role": "user", "content": prompts.scene_detail_prompt(
                 bible,
                 # Only THIS scene's slice + a one-line episode context — not the whole
                 # episode blueprint (overloading the scene call made it re-narrate).
                 {"title": outline.get("title", ""),
                  "plot": outline.get("plot", ""),
                  "scene": ps},
                 cast, current, notes,
                 director_constraints=dir_notes,
                 dialogue_beats_target=_dbt)}],
            model=model, role="showrunner", temperature=0.8, max_tokens=8192,
            on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                "detail": f"Episode {episode}: scene detail {sid} ({n} tokens…)",
                "output": t[-500:], "ts": time.time()}}))
        detail.setdefault("id", sid)
        detail["id"] = sid   # force: the model often echoes a wrong id in content
        detail.setdefault("location", ps.get("location", ""))
        detail.setdefault("time_of_day", ps.get("time_of_day", ""))
        detail.setdefault("summary", ps.get("summary", ""))
        detail.setdefault("characters", ps.get("characters", []))
        epstate.write_json(sc_dir / f"{sid}.json", detail)
        details.append(detail)
    # Deterministic repetition gate: an episode that replays the same scene again
    # and again gives the dialogue writer nothing to do but invent filler quips.
    # Reject the treatments (bounded) BEFORE they reach dialogue; the reconciler
    # regenerates them from the notes.
    thresholds = cfg.get("reviewers", "thresholds", {})
    rep = repetition_review(
        details,
        similarity=float(thresholds.get("repetition_similarity", 0.5)),
        min_cluster=int(thresholds.get("repetition_min_cluster", 3)))
    attempts = int(read_scene_details(show, episode).get("rep_attempts", 0) or 0)
    payload: dict[str, Any] = {"status": "pending", "scenes": details,
                               **({"rejected_notes": notes} if notes.strip() else {})}
    payload["_review"] = rep
    reasons: list[str] = []
    if not rep["pass"]:
        attempts += 1
        payload["rep_attempts"] = attempts
        if attempts <= _MAX_DETAILS_REGENS:
            reasons.append("REPETITION: " + "; ".join(
                n.get("note", "") for n in (rep.get("notes") or [])[:4]))
    # Mature-service ledger: the countable FLOOR (panty shots, bare-chest
    # reveals, service scenes) plus the payoff CEILING on a scheduled reward
    # episode. A shortfall is a required element MISSING -> reject and
    # regenerate, bounded, exactly like the repetition gate.
    from . import service as _service
    led = _service.service_ledger(details)
    srv_short = _service.ledger_shortfalls(
        bible, led, reward=_service.is_reward_episode(show, episode, cfg))
    payload["_service"] = {"ledger": led, "shortfalls": srv_short}
    if srv_short:
        serv_attempts = int(
            read_scene_details(show, episode).get("serv_attempts", 0) or 0) + 1
        payload["serv_attempts"] = serv_attempts
        if serv_attempts <= _MAX_DETAILS_REGENS:
            reasons.append("SERVICE FLOOR NOT MET (stage these ON-PAGE): "
                           + "; ".join(srv_short))
    if reasons:
        payload["status"] = "rejected"
        payload["rejected_notes"] = " | ".join(reasons)
    epstate.save_details(show, episode, payload)
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
    epstate.save_details(show, episode, data)
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
    # Halt any running storyboard/render worker FIRST: it loops until its job is
    # "stopped" and would otherwise keep writing clips for the very artifacts we
    # are about to delete (and for an outline that no longer exists).
    try:
        from .storyboard import stop_storyboard
        from .render import stop_render
        stop_storyboard(show.show_id)
        stop_render(show.show_id)
    except Exception:
        pass
    import shutil
    runs = show.dir / "runs" / f"EP{episode:02d}"
    sc_dir = _scenes_dir(show, episode)
    if sc_dir.exists():
        for f in (list(sc_dir.glob("*_shots.json")) + list(sc_dir.glob("*.p*.json"))
                  + list(sc_dir.glob("*_anchor.png")) + list(sc_dir.glob("*.continuity*.txt"))):
            try:
                f.unlink()
            except Exception:
                pass
    for r in list(runs.glob("script.r*.json")):
        try:
            r.unlink()
        except Exception:
            pass
    # Per-shot DECISIONS + derived reports are keyed by shot id (deterministic and
    # reused), so they must go too — otherwise a rebuilt shot inherits a stale
    # approved/rejected status, a stale revised_prompt/retake_count, and a stale
    # VL/STT verdict for an image the human never saw.
    for name in ("shots.approval.json", "prompts.json", "consistency.json"):
        fp = runs / name
        if fp.exists():
            try:
                fp.unlink()
            except Exception:
                pass
    # The episode dialogue is authored from the scene treatments too — a rejected
    # episode must regenerate it, not silently reuse the old lines.
    for name in (DIALOGUE_FILE, DIALOGUE_TXT):
        fp = runs / name
        if fp.exists():
            try:
                fp.unlink()
            except Exception:
                pass
    for sub in ("reviews", "storyboard", "objects", "video", "qc", "vcheck"):
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
    epstate.save_details(show, episode, data)
    _clear_scenes_downstream(show, episode)
    return data


def _scene_runtime_target(plan_scene: dict[str, Any], total_target: int, n_scenes: int) -> int:
    if not n_scenes:
        return 90
    return max(30, total_target // n_scenes)


# --- Episode dialogue (author once, then distribute) ------------------------
#
# Dialogue is context-hungry: callbacks, escalation and "who knows what yet" only
# work with the whole episode in view. Writing it scene-by-scene, last, from a
# handful of quips is what produced repetitive filler and incoherent lines. So we
# author the whole episode's dialogue as one plain-text script, then a mechanical
# pass places each line into a shot. See DESIGN conversation; PROMPTING.md.

def _dialogue_path(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / DIALOGUE_FILE


def _parse_scene_header(line: str) -> str | None:
    """Return the scene id when ``line`` is a scene header, else None."""
    s = line.strip()
    if not s:
        return None
    stripped = s.strip("=[]# \t-")
    m = re.match(r"^(s\d{1,3})\b", stripped, re.I)
    if not m:
        return None
    header_shaped = (
        s.startswith("=") or s.startswith("[") or s.startswith("#")
        or bool(re.fullmatch(r"s\d{1,3}", s, re.I))
        or bool(re.match(r"^s\d{1,3}\b\s*[:\-]", s, re.I))
        or bool(re.match(r"^s\d{1,3}\b\s+[A-Z]", s)))
    return m.group(1).lower() if header_shaped else None


def _cast_lookup(cast_names: list[str]) -> dict[str, str]:
    """Map a spoken name to its canonical cast name.

    Full names always map; an unambiguous first/last name also maps (writers often
    shorten names), but a token shared by two cast members is dropped so it can
    never attribute a line to the wrong character.
    """
    lut: dict[str, str] = {}
    ambiguous: set[str] = set()
    for n in cast_names:
        if not n:
            continue
        lut[n.lower()] = n
        parts = n.strip().split()
        for tok in {parts[0].lower(), parts[-1].lower()} if len(parts) > 1 else ():
            if tok in lut and lut[tok] != n:
                ambiguous.add(tok)
            else:
                lut.setdefault(tok, n)
    for tok in ambiguous:
        lut.pop(tok, None)
    return lut


def parse_nickname_ledger(text: str,
                          cast_names: list[str]) -> dict[str, dict[str, list[str]]]:
    """Parse the ``NICKNAMES:`` block into {speaker: {addressee: [terms]}}.

    The writer declares, per speaker, what they call each other person, e.g.
    ``Speaker -> Person: term, term``. Persisted to continuity so the same terms
    are reused in later episodes. Parsing is deterministic.
    """
    lut = _cast_lookup(cast_names)
    ledger: dict[str, dict[str, list[str]]] = {}
    in_block = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _parse_scene_header(line):
            in_block = False
            continue
        if line.upper().rstrip(":") == "NICKNAMES":
            in_block = True
            continue
        if not in_block or "->" not in line or ":" not in line:
            continue
        left, _, right = line.partition(":")
        speaker_part, _, addressee_part = left.partition("->")
        speaker = lut.get(speaker_part.strip().lower(), speaker_part.strip())
        addressee = lut.get(addressee_part.strip().lower(), addressee_part.strip())
        terms = [t.strip() for t in right.split(",") if t.strip()]
        if speaker and addressee and terms:
            bucket = ledger.setdefault(speaker, {}).setdefault(addressee, [])
            for t in terms:
                if t not in bucket:
                    bucket.append(t)
    return ledger


def merge_nickname_ledger(existing: dict[str, Any] | None,
                          new: dict[str, Any] | None) -> dict[str, dict[str, list[str]]]:
    """Fold a new ledger into the established one.

    An ESTABLISHED (speaker -> person) pair is locked: its terms are kept as-is and
    the new ledger can never extend or replace them. Only pairs not yet established
    are added. This is what keeps nicknames consistent across episodes (a stray
    extra term in a later episode can't silently widen the allowed set).
    """
    out: dict[str, dict[str, list[str]]] = {}
    for speaker, addrs in (existing or {}).items():
        for addressee, terms in (addrs or {}).items():
            out.setdefault(speaker, {})[addressee] = [t for t in (terms or []) if t]
    for speaker, addrs in (new or {}).items():
        for addressee, terms in (addrs or {}).items():
            if addressee in out.get(speaker, {}):
                continue
            out.setdefault(speaker, {})[addressee] = [t for t in (terms or []) if t]
    return out


def parse_dialogue_script(text: str, cast_names: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Parse the plain-text dialogue script into {scene_id: [line, ...]}.

    Deterministic: the LLM returns lines like ``SPEAKER: words`` under ``=== sNN ===``
    headers; everything before the first header (the VOICE block) is ignored. A
    parenthetical marks a delivery note for the following words, or a speaker tag
    (``V.O.`` / ``O.S.`` / ``OFF``) that makes the line an off-screen voiceover. A
    ``(silent)`` scene maps to [] (an intentional silent beat); a header whose lines
    all failed to parse is dropped so the caller can fall back.
    """
    cast_lut = _cast_lookup(cast_names)
    scenes: dict[str, list[dict[str, Any]]] = {}
    silent_marked: set[str] = set()
    current: str | None = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        header = _parse_scene_header(line)
        if header:
            current = header
            scenes.setdefault(current, [])
            continue
        if current is None:
            continue                      # VOICE notes / preamble
        if line.strip().lower() in ("(silent)", "silent"):
            silent_marked.add(current)
            continue
        if ":" not in line:
            continue
        name_part, _, spoken = line.partition(":")
        name_part, spoken = name_part.strip(), spoken.strip()
        if not spoken:
            continue
        on_camera: bool | None = None
        tag_m = re.search(r"\(([^)]*)\)\s*$", name_part)
        if tag_m:
            tag = re.sub(r"[^a-z]", "", tag_m.group(1).lower())
            name_part = name_part[:tag_m.start()].strip()
            if tag in ("vo", "os", "off") or "voiceover" in tag or tag.startswith("off"):
                on_camera = False
        delivery = ""
        del_m = re.match(r"^\(([^)]*)\)\s*(.*)$", spoken)
        if del_m:
            delivery, spoken = del_m.group(1).strip(), del_m.group(2).strip()
        if not spoken:
            continue
        entry: dict[str, Any] = {"char": cast_lut.get(name_part.lower(), name_part),
                                 "line": spoken}
        if on_camera is False:
            entry["on_camera"] = False
        if delivery:
            entry["delivery"] = delivery
        scenes[current].append(entry)
    # Keep a scene only if it has real lines or was explicitly marked (silent). A
    # header whose lines all failed to parse is dropped, so the caller falls back
    # to the legacy writer instead of silently silencing a scene that spoke.
    return {sid: lines for sid, lines in scenes.items()
            if lines or sid in silent_marked}


def read_episode_dialogue(show: Show, episode: int) -> dict[str, list[dict[str, Any]]]:
    """Read the parsed per-scene dialogue spine, or {} when none exists."""
    p = _dialogue_path(show, episode)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        scenes = data.get("scenes")
        return scenes if isinstance(scenes, dict) else {}
    except Exception:
        return {}


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
            "duration_s": "float 4-30 (shots with dialogue ~10.125, action inserts ~5.167)",
            "beat": "one sentence: what happens in this shot and which characters are present",
        }]},
        "instructions": (
            "Break the scene into enough shots to fill ~{target_s}s of runtime "
            "(~{n_shots} shots). Fully realize every beat of the scene treatment; "
            "vary what happens across the shots instead of replaying one movement. "
            "ONE SHOT = ONE ENVIRONMENT: never let a single shot travel from one "
            "place into another (e.g. start outside then move inside a cockpit) — "
            "that is two shots; cut on the transition, because each shot is grounded "
            "on one location reference. "
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
            "handheld). Match the shot's beat. DIALOGUE COVERAGE IS NOT COVERAGE FOR ITS "
            "OWN SAKE: for a run of consecutive shots carrying lines of the SAME exchange, "
            "HOLD the established framing and change it only gently - a small angle/height "
            "shift or a short reframe (classic shot/reverse-shot at the SAME distance). Do "
            "NOT jump from wide to close, or cross to a new axis, between consecutive "
            "dialogue lines. Reserve a big framing change (new distance or axis) for a new "
            "story beat, a location change, or a marked hero money shot."),
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
                           "via_display": ["of those, the names visible ONLY through a screen/TV/monitor/hologram/broadcast (NOT physically present)"],
                           "scene": "the SINGLE set this shot is physically in (its specific environment, e.g. 'Mira's dead cockpit')"},
        }]},
        "instructions": (
            "For every shot, list the EXACT cast names visible on screen in "
            "references.characters (empty array if no characters are visible) and the "
            "specific SET the shot is physically in for references.scene. Only a "
            "character the CAMERA ACTUALLY FRAMES belongs in references.characters: a "
            "two-shot declares two, a single declares one. Characters merely present in "
            "the room - standing in the background, edging frame, or part of the crowd - "
            "are NOT framed and must not be declared (they get no reference and would be "
            "rendered as extras). When the camera names specific characters, declare only "
            "those plus anyone who speaks ON CAMERA. Also: a speaker heard off-screen "
            "(their line is on_camera false) is NOT visible and must never be listed, and "
            "an insert of a prop/dial/monitor, or an exterior with no bodies, has an EMPTY "
            "characters list. One shot is in ONE environment: if the shot's action would "
            "move between places, the blocker must have split it - report only the single "
            "environment this shot occupies. Use the most specific name (a cockpit, a "
            "monitor wall, the apron), not the broad venue. Separately, list in "
            "references.via_display any name visible ONLY through a "
            "screen/monitor/broadcast/hologram (e.g. a rival laughing on a jumbotron) - "
            "they are NOT physically present, so the render must show them INSIDE the "
            "display, never standing in the scene, and they must NOT also appear in "
            "references.characters. Names must match the cast exactly."),
        "nested": [("references", ["characters", "via_display", "scene"])],
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
            "dialogue": [{"char": "exact cast name",
                          "line": "exact spoken words: a COMPLETE, natural sentence (or two short ones), ~5-25 words with a subject and a verb - spoken, never a noun list or clipped fragment",
                          "on_camera": "bool (speaker visible on screen; false = off-screen/voiceover)",
                          "delivery": "optional one-phrase delivery note, e.g. 'in a low, breathy voice'"}],
            "silence": "bool - true when NO ONE speaks in this shot (dialogue must be an empty array); false or omit when characters speak",
        }]},
        "instructions": (
            "Write the scene's dialogue from its dialogue_beats / narrative. These lines "
            "become SYNTHESIZED AUDIO (voice + lip-sync), so every line is SPOKEN, not "
            "read: write plain, direct speech with contractions; a character speaks from "
            "what they WANT or feel right now - never for effect. NEVER lyrical or "
            "overwritten, techno-poetic, or narration-style lines, and no punchlines, "
            "slogans or balanced quips ('You've got no power and I've got sparks' is "
            "WRONG). USE CONTRACTIONS ('I'm', 'don't', 'it's' - never 'I am', 'do not'); "
            "DON'T narrate the visible action, and don't chain ', so ...' / ', because ...' "
            "on every line. Each line MUST be a COMPLETE, natural sentence (or two short "
            "ones) a person would actually say, with a subject and a verb - as long as the "
            "thought needs, never clipped to sound cool. "
            "NEVER write a clipped fragment, a stacked noun/adjective list, a label, or a "
            "stage direction as a line ('Borrowed suit, live wire, bad plan. Scoot up.' is "
            "WRONG - a noun list plus a fragment). Keep natural spoken rhythm (contractions "
            "are fine) and rewrite anything that reads awkwardly aloud. A ~10s shot carries "
            "up to 4-5 lines (~40-70 words total) when the beat is dialogue-heavy. Write numbers and abbreviations "
            "as they should be spoken (e.g. 'twenty-one', 'five-g'). EVERY shot must "
            "explicitly be either SPEAKING or "
            "SILENT. SPEAKING: `silence: false` (or omit) with real spoken lines in the "
            "`dialogue` array. SILENT: `silence: true` AND an EMPTY `dialogue` array — only "
            "when genuinely no one speaks (a silent beat, a reaction shot, an action "
            "insert). Roughly 60-70% of the scene's runtime should be spoken aiming for "
            "~{n_lines} total spoken lines, but a silent beat is intentional, never faked: "
            "every line must contain REAL spoken words — a leading delivery note like "
            "'(whispering)' is allowed only before actual speech, and a line that is only "
            "a stage direction, grunt, or internal note is FORBIDDEN (it would be spoken "
            "as gibberish by the video model). Keep each character's VOICE distinct (see "
            "their personality/traits below): before writing the scene, write 1-2 "
            "voice-anchor lines per speaking character that could only come from that "
            "character, then keep every line in that voice. If how a line is said matters "
            "— shouted, whispered, teasing, flat — carry it in `delivery` (e.g. 'in a low, "
            "breathy voice'); never use ALL-CAPS or stacked punctuation inside `line` to "
            "convey emotion. Use only exact cast names."),
        "fields": ["dialogue", "silence"],
        "cast_key": "personality",
    },
]


def _blocking_pass(with_dialogue_spine: bool) -> dict[str, Any]:
    """The blocking pass config, optionally dialogue-aware.

    With a spine, blocking builds shots AROUND the already-written dialogue and
    reports, per shot, which numbered lines it carries (``lines``). Without a
    spine (prose had no dialogue for the scene), a later pass writes it.
    """
    p = dict(_SCENE_PASSES[0])
    if not with_dialogue_spine:
        return p
    p["schema"] = {"shots": [{
        "id": "s01_sh01, s01_sh02, ...",
        "type": "'ref2va' if characters are on screen, else 'fl2va'",
        "importance": "'hero' for the scene's 1-2 money shots, else 'standard'",
        "duration_s": "float 4-30 (a shot with dialogue ~10.125, an action insert ~5.167)",
        "beat": "one sentence: what happens in this shot and which characters are present",
        "lines": "array of dialogue-line NUMBERS this shot carries, ascending; [] for a silent shot",
    }]}
    p["instructions"] = (
        "Break the scene into shots built AROUND the numbered DIALOGUE SPINE below. "
        "Assign EVERY spine line to exactly ONE shot, keeping the numbers in ascending "
        "order across the shots; a shot carries 1-3 consecutive lines. Insert SILENT "
        "shots (lines: []) for action beats that need no speech. You only PLACE the "
        "given lines - never write, reword, add or drop dialogue. Mark the scene's 1-2 "
        "hero money shots. Size each shot to what it holds: about 2 lines ~10s, a "
        "single short line or a silent insert ~5s. Write each shot's `beat` as PLAIN, "
        "DIRECT screen action (what we SEE happen) - no run-on sentences. Do NOT "
        "describe costumes, looks, props or UI/scoreboards here; those are owned by the "
        "scene details and referenced by later passes. Do NOT restate the narrative. "
        "Keep ONE ENVIRONMENT per shot: a shot that would move between two places (e.g. "
        "outside then inside a cockpit) must be split into two shots.")
    return p


def _scene_passes(with_dialogue_spine: bool) -> list[dict[str, Any]]:
    """The per-scene pass chain; with a spine, drop the dialogue WRITER pass (the
    lines already exist) and make blocking dialogue-aware."""
    passes = [_blocking_pass(with_dialogue_spine)]
    for p in _SCENE_PASSES[1:]:
        if p.get("name") == "dialogue" and with_dialogue_spine:
            continue
        passes.append(p)
    return passes


def _dialogue_entry(src: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"char": src.get("char", ""), "line": src.get("line", "")}
    if src.get("on_camera") is not None:
        entry["on_camera"] = bool(src["on_camera"])
    if (src.get("delivery") or "").strip():
        entry["delivery"] = str(src["delivery"]).strip()
    return entry


def _inject_dialogue(draft: list[dict[str, Any]],
                     spine: list[dict[str, Any]]) -> None:
    """Place the pre-written spine lines into shots from each shot's ``lines`` indices.

    Deterministic: the words come from the authored script, never from the LLM. A
    line the blocker failed to place is appended to the scene's last shot so no
    written dialogue is silently lost.
    """
    used: set[int] = set()
    for shot in draft:
        idxs = shot.pop("lines", None)
        entries: list[dict[str, Any]] = []
        if isinstance(idxs, list):
            for i in idxs:
                try:
                    i = int(i)
                except (TypeError, ValueError):
                    continue
                if 0 <= i < len(spine) and i not in used:
                    used.add(i)
                    entries.append(_dialogue_entry(spine[i] or {}))
        shot["dialogue"] = entries
        shot["silence"] = not entries
    leftover = [i for i in range(len(spine)) if i not in used]
    if leftover and draft:
        last = draft[-1]
        last.setdefault("dialogue", [])
        for i in leftover:
            last["dialogue"].append(_dialogue_entry(spine[i] or {}))
        last["silence"] = False


def _restore_dialogue(script: dict[str, Any], sc_dir: Path,
                      spine: dict[str, list[dict[str, Any]]] | None = None) -> None:
    """Force every shot's dialogue to the APPROVED lines, deterministically.

    1. Prefer the per-scene shot checkpoint's own distribution (exact when the
       reviewed script kept the same shot ids).
    2. Otherwise distribute the scene's approved spine lines across the scene's
       shots, in order — so no revision can leave a rewritten line behind.
    """
    for scene in script.get("scenes", []):
        sid = scene.get("id", "")
        shots = scene.get("shots", [])
        if not shots:
            continue
        # The AUTHORED spine is canonical: distribute its approved lines across
        # the scene's shots, in order, so no downstream rewrite can leave a line
        # the spine never had. Spine and shot script can never diverge.
        lines = (spine or {}).get(sid)
        if lines is not None:
            n, m = len(lines), len(shots)
            base, extra, idx = n // m, n % m, 0
            for j, sh in enumerate(shots):
                take = base + (1 if j < extra else 0)
                sh["dialogue"] = list(lines[idx:idx + take])
                sh["silence"] = not sh["dialogue"]
                idx += take
            continue
        # No spine for this scene: keep the checkpoint's own distribution.
        ck = sc_dir / f"{sid}_shots.json"
        ck_map: dict[str, Any] = {}
        if ck.exists():
            try:
                data = json.loads(ck.read_text(encoding="utf-8"))
                ck_map = {sh.get("id"): sh.get("dialogue")
                          for sh in (data.get("shots") or []) if sh.get("id")}
            except Exception:
                ck_map = {}
        if ck_map and all(sh.get("id") in ck_map for sh in shots):
            for sh in shots:
                dlg = ck_map.get(sh.get("id")) or []
                sh["dialogue"] = dlg
                sh["silence"] = not dlg
            continue


def _finalize_on_camera(draft: list[dict[str, Any]]) -> None:
    """Set on_camera from actual on-screen presence (after the references pass).

    A speaker not listed in the shot's references.characters is off-screen
    (voiceover) -> on_camera False. An explicit V.O. mark from the script is kept.
    """
    for shot in draft:
        onscreen = set((shot.get("references") or {}).get("characters") or [])
        for d in shot.get("dialogue") or []:
            if not isinstance(d, dict):
                continue
            if d.get("char") not in onscreen:
                d["on_camera"] = False
            elif d.get("on_camera") is None:
                d["on_camera"] = True


def _finalize_on_camera_script(script: dict[str, Any]) -> None:
    """Apply :func:`_finalize_on_camera` to every scene of an assembled script.

    Run on the FINAL script (after review/rewrite/restore) so ``on_camera`` always
    reflects the shot's declared on-screen cast (``references.characters``). A
    speaker who is not on screen becomes OFF-SCREEN (voiceover): they keep a VOICE
    reference but get no picture, so H3 voices them without rendering them.
    """
    for sc in script.get("scenes", []):
        _finalize_on_camera(sc.get("shots") or [])


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


def _effective_shot_notes(show: Show, notes: str = "") -> str:
    """Merge per-call director feedback with durable standing constraints.

    ``notes`` is the rejection feedback for this regeneration (may be empty on
    first draft). Standing rules from director_notes.json (promoted HIGHs via
    studio.learn) are prepended as ABSOLUTE so every blocking/camera/action pass
    prevents repeat rejections at draft time, not just on revision.
    """
    standing = read_director_notes(show)
    parts: list[str] = []
    if standing:
        parts.append("STANDING DIRECTOR CONSTRAINTS (ABSOLUTE — learned from prior "
                     "writers'-room rejections; apply every one):\n- " + "\n- ".join(standing))
    if (notes or "").strip():
        parts.append((notes or "").strip())
    return "\n\n".join(parts)


def generate_scene_shots(show: Show, episode: int, plan_scene: dict[str, Any],
                         llm=None, cfg=None, episode_summary: str = "",
                         notes: str = "",
                         dialogue_spine: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Chunk 2: write ONE scene's shots as a chain of focused passes.

    blocking -> camera -> action -> references -> costumes -> soundscape [-> dialogue].
    Each pass is its own LLM call with a single job description; later passes merge
    only their own field(s) into the draft. The draft is checkpointed after every
    pass (``scenes/<sid>.p<N>.json``) so an interrupted run resumes mid-scene.
    ``notes`` (director feedback) is appended to every pass prompt.

    ``dialogue_spine`` (the pre-written lines for this scene, from the reviewed
    prose screenplay): when given, blocking is dialogue-aware and the lines are
    injected verbatim, so the authoring dialogue pass is skipped. When None, the
    per-scene dialogue writer runs.
    """
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    bible = show.read_bible()
    cast = [show.read_character(cid) for cid in show.list_characters()]
    sid = plan_scene.get("id", "s01")
    target_s = _scene_runtime_target(plan_scene, _target(show),
                                     len(read_episode_plan(show, episode).get("scenes", [])))
    showrunner_sys = prompts.showrunner_system(
        show.style_profile(cfg), bible.get("content_policy", "mature"),
        show.style_profile(cfg).get("baseline", ""))
    sc_dir = _scenes_dir(show, episode)
    sc_dir.mkdir(parents=True, exist_ok=True)
    start, draft = _partial_draft(sc_dir, sid)
    passes = _scene_passes(dialogue_spine is not None)
    effective_notes = _effective_shot_notes(show, notes)

    # One-time visual grounding: a short environment brief derived from the
    # scene's location ref (when the plan's location resolves to one). Carried
    # into EVERY pass so all shots in the scene share one layout and lighting
    # instead of each pass re-inventing the set.
    continuity = ""
    try:
        from .scene_anchor import anchor_path, continuity_brief
        anc = anchor_path(show, episode, plan_scene, cfg=cfg)
        if anc:
            vision_llm = llm if hasattr(llm, "chat") else None
            continuity = continuity_brief(show, episode, plan_scene, anc,
                                          llm=vision_llm, cfg=cfg)
    except Exception:
        continuity = ""

    for i, pass_cfg in enumerate(passes):
        if i < start:
            continue
        name = pass_cfg["name"]
        # Dialogue gets its own system prompt: the showrunner prompt's "say more
        # with less / no on-the-nose lines" pushes overwritten, unspoken prose.
        system = (prompts.dialogue_system(
            show.style_profile(cfg), bible.get("content_policy", "mature"),
            show.style_profile(cfg).get("baseline", ""))
            if name == "dialogue" else showrunner_sys)
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: scene {sid} — {name} pass…",
                                  "ts": time.time()}
        out = promptlog.logged_chat_json(
            llm, show, episode, f"shot-{sid}-{name}",
            [{"role": "system", "content": system},
             {"role": "user", "content": prompts.scene_pass_prompt(
                 pass_cfg, bible, episode_summary, plan_scene, cast, draft, target_s, effective_notes,
                 dialogue_spine=dialogue_spine, continuity=continuity)}],
            model=model, role="showrunner", temperature=0.8, max_tokens=32768,
            on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                "detail": f"Episode {episode}: scene {sid} — {name} ({n} tokens…)",
                "output": t[-500:], "ts": time.time()}}))
        if i == 0:
            draft = [s for s in (out.get("shots") or []) if isinstance(s, dict) and s.get("id")]
            if not draft:
                raise RuntimeError(f"scene {sid} blocking pass returned no shots")
            if dialogue_spine is not None:
                _inject_dialogue(draft, dialogue_spine)
        else:
            _apply_pass(draft, out, pass_cfg)
        (sc_dir / f"{sid}.p{i}.json").write_text(
            json.dumps({"shots": draft}, indent=2, ensure_ascii=False), encoding="utf-8")

    if dialogue_spine is not None:
        _finalize_on_camera(draft)

    scene = {
        "id": sid,
        "location": plan_scene.get("location", ""),
        "time_of_day": plan_scene.get("time_of_day", ""),
        "summary": plan_scene.get("summary", ""),
        "shots": draft,
    }
    # Reconcile the declared cast with what is ACTUALLY visible: drop off-screen
    # speakers (and display-only names) the references pass listed on insert /
    # monitor / exterior shots, so H3 never renders a character over a prop shot.
    try:
        from .storyboard import _shot_characters
        for sh in draft:
            if not isinstance(sh.get("references"), dict):
                continue
            visible = _shot_characters(sh)
            sh["references"]["characters"] = visible
            sh["type"] = "ref2va" if visible else "fl2va"
    except Exception:
        pass
    # Per-shot environment grounding: a shot can sit in a specific set of its own
    # (a dropped cockpit, a monitor wall, the apron) that differs from the scene's
    # broader place. Resolve every shot's `references.scene` to a registry
    # environment id — registering a new top-level entry (and its ref) the first
    # time a set appears — so each shot grounds on its OWN environment, not the
    # scene's umbrella place.
    try:
        _canonicalize_shot_environments(show, episode, scene, llm=llm, cfg=cfg)
    except Exception:
        pass
    return scene


def _repair_shot_locations(show: Show, script: dict[str, Any]) -> None:
    """Make every shot's location_id and cast point at real data after review.

    - A shot whose ``references.location_id`` has no registry entry is re-resolved
      from ``references.scene`` and, failing that, registered (filesystem only, no
      GPU) so the storyboard's GPU phase can render its ref. This catches a
      writers'-room revision that splits one shared set into several named sets
      textually without creating those entries.
    - ``references.characters`` / ``type`` are re-derived from the final dialogue:
      off-screen speakers and display-only names are not visual subjects.
    """
    from . import scenes as _scenes
    from .storyboard import _shot_characters
    registry = set(show.list_scenes() or [])
    for sc in script.get("scenes", []) or []:
        for sh in sc.get("shots", []) or []:
            if not isinstance(sh, dict):
                continue
            refs = sh.get("references")
            if not isinstance(refs, dict):
                refs = {}
                sh["references"] = refs
            lid = str(refs.get("location_id") or "").strip()
            name = str(refs.get("scene") or sc.get("location") or "").strip()
            if lid not in registry:
                resolved = _scenes.resolve_slug(show, name) if name else None
                if not resolved and (name or lid):
                    resolved = _scenes.register_location(
                        show, name or lid, description=name or lid)
                if resolved:
                    refs["location_id"] = resolved
                    registry.add(resolved)
            visible = _shot_characters(sh)
            refs["characters"] = visible
            sh["type"] = "ref2va" if visible else "fl2va"


def _canonicalize_shot_environments(show: Show, episode: int, scene: dict[str, Any],
                                    llm=None, cfg=None) -> None:
    """Stamp a registry environment id on every shot's ``references``.

    Existing matches reuse their entry; a genuinely-new set is registered as a
    top-level location (canonical description + Krea 2 setting) and its ref is
    rendered in the background. Idempotent: re-running resolves to the same ids.
    """
    shots = scene.get("shots") or []
    if not shots:
        return
    from . import scenes as _scenes
    scene_loc = str(scene.get("location") or "").strip()
    pending: dict[str, list[dict[str, Any]]] = {}
    for sh in shots:
        if not isinstance(sh, dict):
            continue
        refs = sh.get("references")
        if not isinstance(refs, dict):
            refs = {}
            sh["references"] = refs
        name = str(refs.get("scene") or scene_loc or "").strip()
        if not name:
            continue
        lid = str(refs.get("location_id") or "").strip()
        if lid and _scenes.environment_ref(show, lid):
            continue
        resolved = _scenes.resolve_slug(show, name)
        if resolved:
            refs["location_id"] = resolved
            continue
        pending.setdefault(name, []).append(sh)
    if not pending:
        return
    items = [{"location": name, "summary": scene.get("summary", ""), "beats": []}
             for name in pending]
    briefs = _describe_new_locations(show, {"scenes": []}, items, llm, cfg)
    # Register ONLY (write the entry). Ref images are rendered later, in a GPU
    # phase (scenes.generate_missing_refs) — never here: this runs while the
    # local LLM owns the GPU, and a concurrent krea2 render would OOM it.
    for name, shs in pending.items():
        b = briefs.get(name) or {}
        desc = str(b.get("description") or "").strip() or name
        setting = str(b.get("setting_prompt") or "").strip()
        lid = _scenes.register_location(show, name, description=desc,
                                        setting_prompt=setting)
        for sh in shs:
            sh.setdefault("references", {})["location_id"] = lid


def attach_review_verdict(script: dict[str, Any], reviews: dict[str, dict[str, Any]],
                          passed: bool, round_no: int) -> dict[str, Any]:
    """Attach the writers'-room verdict to a script (in place) and return it.

    Each reviewer contributes its grade and free-form review (no structured notes).
    """
    script["_review"] = {
        "passed": passed,
        "rounds": round_no,
        "reviews": {k: {"grade": v.get("grade"), "pass": v.get("pass"),
                        "review": v.get("review", "")}
                    for k, v in reviews.items()},
    }
    return script


def _writer_review(show: Show, episode: int, script: dict[str, Any],
                   llm=None, cfg=None, max_revisions: int | None = None) -> dict[str, Any]:
    """Writers'-room review + revise loop for an assembled script.

    Reviews it (slop/continuity/fan_service + duration), and if it fails, asks
    the showrunner to revise it addressing every note, re-reviews, up to
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
        show.style_profile(cfg), bible.get("content_policy", "mature"),
        show.style_profile(cfg).get("baseline", ""))
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
    last_path = runs / "script.r1.json"
    reviews = _review(script, round_no)
    passed = all_pass(reviews)
    while not passed and round_no < max_revisions:
        round_no += 1
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: writers' room revision (round {round_no})…",
                                  "ts": time.time()}
        revised = promptlog.logged_chat_json(
            llm, show, episode, f"script-revise-r{round_no}",
            [{"role": "system", "content": system},
             {"role": "user", "content": prompts.revision_prompt(
                 script, review_notes_text(reviews),
                 outline=read_episode_plan(show, episode))}],
            model=model, role="showrunner", temperature=0.6, max_tokens=65536,
            on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                "detail": f"Episode {episode}: writers' room revision ({n} tokens…)",
                "output": t[-500:], "ts": time.time()}}))
        # The revision prompt returns an arbitrated edit letter + the script.
        if isinstance(revised, dict) and isinstance(revised.get("script"), dict):
            revised = revised["script"]
        if not isinstance(revised, dict) or not revised.get("scenes"):
            break
        script = _normalize(revised, cast_names)
        last_path = runs / f"script.r{round_no}.json"
        last_path.write_text(
            json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
        reviews = _review(script, round_no)
        passed = all_pass(reviews)
    # Store the review outcome on the script AND PERSIST IT. The pipeline gates on
    # the on-disk script (episode_repair._script_latest reads script.r*.json), so a
    # verdict that only lives in memory is invisible: the gate silently never fired
    # and slop/failed scripts shipped anyway. Write the verdict back to the latest
    # revision so the reject/regenerate path can actually see it.
    attach_review_verdict(script, reviews, passed, round_no)
    from . import epstate
    epstate.save_script(last_path, script)
    return script


_MAX_SCRIPT_REGENS = 2  # auto-regeneration attempts before holding for human
_MAX_DETAILS_REGENS = 2  # reps of scene details rewrites before accepting them as-is


def revise_script(show: Show, episode: int, script: dict[str, Any],
                  reviews: dict, llm=None, cfg=None) -> dict[str, Any]:
    """One full revision of the script from the failing reviewers' free-form notes.

    Gives the editor the OUTLINE (story context), the CURRENT SCRIPT (what it edits),
    and the notes, then writes the result as the next script revision. Returns the
    revised (still UNREVIEWED) script. ``reviews`` may be a raw reviewer map or the
    persisted ``_review`` verdict (whose ``reviews`` key is unwrapped).
    """
    from .clients.lmstudio import LMStudioClient
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    cast_names = [c.get("name") for c in (show.read_character(cid)
                  for cid in show.list_characters()) if c.get("name")]
    system = prompts.showrunner_system(
        show.style_profile(cfg), show.read_bible().get("content_policy", "mature"),
        show.style_profile(cfg).get("baseline", ""))
    runs = show.dir / "runs" / f"EP{episode:02d}"
    if isinstance(reviews, dict) and isinstance(reviews.get("reviews"), dict):
        reviews = reviews["reviews"]
    notes_text = review_notes_text(reviews)
    if not notes_text:
        return script
    ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: revising script from review notes…",
                              "ts": time.time()}
    out = promptlog.logged_chat_json(
        llm, show, episode, "script-revise-from-review",
        [{"role": "system", "content": system},
         {"role": "user", "content": prompts.revision_prompt(
             script, notes_text, outline=read_episode_plan(show, episode))}],
        model=model, role="showrunner", temperature=0.6, max_tokens=65536,
        on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
            "detail": f"Episode {episode}: revising script ({n} tokens…)",
            "output": t[-500:], "ts": time.time()}}))
    if isinstance(out, dict) and isinstance(out.get("script"), dict):
        out = out["script"]
    if not isinstance(out, dict) or not out.get("scenes"):
        return script
    script = _normalize(out, cast_names)
    rounds = (script.get("_review") or {}).get("rounds", 0)
    # The revision is UNREVIEWED — drop the prior verdict so a stale fail cannot
    # survive onto the fixed script (it must be re-reviewed).
    script.pop("_review", None)
    (runs / f"script.r{rounds + 1}.json").write_text(
        json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
    return script


# --- Continuity extraction (finished episode -> durable facts) ---------------
#
# The continuity state declares world/characters/unresolved_threads/
# continuity_rules but nothing used to populate them, so "remember this" was
# inert. extract_continuity runs once per (re)assembled episode, tags every fact
# with its episode, and replaces that episode's facts on re-run (idempotent).

def _entry_episode(entry: Any) -> int | None:
    if isinstance(entry, dict):
        try:
            return int(entry.get("episode"))
        except (TypeError, ValueError):
            return None
    return None


def purge_episode_continuity(state: dict[str, Any], episode: int) -> None:
    """Drop every fact tagged with this episode (in place) so extraction replaces
    rather than duplicates, and a rejected episode leaves no stale facts."""
    world = state.get("world")
    if isinstance(world, dict) and isinstance(world.get("facts"), list):
        world["facts"] = [f for f in world["facts"] if _entry_episode(f) != episode]
    chars = state.get("characters")
    if isinstance(chars, dict):
        for rec in chars.values():
            if isinstance(rec, dict) and isinstance(rec.get("notes"), list):
                rec["notes"] = [n for n in rec["notes"] if _entry_episode(n) != episode]
    for key in ("unresolved_threads", "continuity_rules"):
        items = state.get(key)
        if isinstance(items, list):
            state[key] = [x for x in items if _entry_episode(x) != episode]


def _merge_continuity_items(items: list[Any], new_items: Any, episode: int) -> None:
    """Append ``{text, episode}`` entries, de-duped by normalized text."""
    def norm(x: Any) -> str:
        s = x.get("text") if isinstance(x, dict) else x
        return " ".join(str(s or "").lower().split())
    seen = {norm(x) for x in items}
    for it in (new_items or []):
        text = str(it).strip() if isinstance(it, str) else ""
        if not text or norm(text) in seen:
            continue
        items.append({"text": text, "episode": episode})
        seen.add(norm(text))


def _continuity_digest(script: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact per-scene view (location, summary, dialogue) for the extractor."""
    out = []
    for sc in script.get("scenes", []) or []:
        lines = [f"{d.get('char')}: {d.get('line')}"
                 for sh in (sc.get("shots") or [])
                 for d in (sh.get("dialogue") or []) if isinstance(d, dict)]
        out.append({"id": sc.get("id"), "location": sc.get("location", ""),
                    "summary": sc.get("summary", ""), "dialogue": lines})
    return out


def extract_continuity(show: Show, episode: int, script: dict[str, Any],
                       llm=None, cfg=None) -> dict[str, Any]:
    """Extract durable facts from ONE finished episode into the continuity state.

    Populates world.facts / characters[*].notes / unresolved_threads /
    continuity_rules, each entry tagged with the episode so a re-run replaces
    (never duplicates) it. These are then carried forward: the planner, the
    dialogue writer and the continuity reviewer all read this state.
    """
    from .clients.lmstudio import LMStudioClient
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    bible = show.read_bible()
    characters = [show.read_character(cid) for cid in show.list_characters()]
    lut = _cast_lookup([c.get("name") for c in characters if c.get("name")])
    state = show.read_continuity()
    purge_episode_continuity(state, episode)
    ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: extracting continuity…",
                              "ts": time.time()}
    out = promptlog.logged_chat_json(
        llm, show, episode, "continuity-extract",
        [{"role": "system", "content": prompts.showrunner_system(
            show.style_profile(cfg), bible.get("content_policy", "mature"),
            show.style_profile(cfg).get("baseline", ""))},
         {"role": "user", "content": prompts.continuity_extract_prompt(
             bible, episode, _continuity_digest(script), state, characters)}],
        model=model, role="showrunner", temperature=0.3, max_tokens=8192)
    world = state.get("world")
    if not isinstance(world, dict):
        world = state["world"] = {}
    _merge_continuity_items(world.setdefault("facts", []), out.get("world_facts"), episode)
    chars = state.get("characters")
    if not isinstance(chars, dict):
        chars = state["characters"] = {}
    for name, notes in (out.get("character_notes") or {}).items():
        if not (name and isinstance(notes, list)):
            continue
        rec = chars.setdefault(lut.get(str(name).lower(), str(name)), {})
        if isinstance(rec, dict):
            _merge_continuity_items(rec.setdefault("notes", []), notes, episode)
    for key in ("unresolved_threads", "continuity_rules"):
        items = state.setdefault(key, [])
        if isinstance(items, list):
            _merge_continuity_items(items, out.get(key), episode)
    show.write_continuity(state)
    return state


def _script_reject_path(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / "script_reject.json"


def reject_script_review(show: Show, episode: int, reviews: dict) -> None:
    """Mark a script as rejected by review and persist notes for auto-regen.

    Stores the reviewer notes and an attempt counter so the reconciler can
    regenerate the script with feedback.  Clears scene checkpoints so
    ``assemble_episode_script`` re-generates shots instead of skipping them.
    """
    path = _script_reject_path(show, episode)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    notes_full: list[dict[str, Any]] = []
    for r, data in reviews.items():
        for n in (data.get("notes") or []):
            if isinstance(n, dict):
                entry = dict(n)
                entry.setdefault("reviewer", r)
                notes_full.append(entry)
    # Human- and LLM-readable string carries EVERY note (never truncated: the
    # regen prompt and the debugging user both need the full blocking set).
    notes = "; ".join(
        f"[{n.get('reviewer', r)}] {n.get('note', '')}"
        for r, data in reviews.items()
        for n in (data.get("notes") or [])
        if isinstance(n, dict)
    )
    path.write_text(json.dumps({
        "notes": notes,
        "notes_full": notes_full,
        "attempts": existing.get("attempts", 0) + 1,
    }, indent=2), encoding="utf-8")
    # Clear scene checkpoints so assembly re-generates shots with the feedback.
    sc_dir = _scenes_dir(show, episode)
    if sc_dir.exists():
        for f in sc_dir.glob("*.json"):
            f.unlink(missing_ok=True)
    # A rejected script must not leave its extracted facts in continuity; the
    # regenerated script re-extracts them.
    try:
        state = show.read_continuity()
        purge_episode_continuity(state, episode)
        show.write_continuity(state)
    except Exception:
        pass


def read_script_rejection(show: Show, episode: int) -> dict[str, Any]:
    """Read stored script rejection data, or {} if none."""
    path = _script_reject_path(show, episode)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def clear_script_rejection(show: Show, episode: int) -> None:
    """Remove the script rejection file (called on successful regeneration)."""
    path = _script_reject_path(show, episode)
    if path.exists():
        path.unlink(missing_ok=True)


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
    # Scene-details-first: the writers' room already wrote each scene's narrative
    # AND its approved dialogue (dialogue_beats). Build the plan scenes from the
    # scene details and write the dialogue spine from them — there is NO separate
    # prose pass and NO second dialogue writer that could rewrite the approved lines.
    # Raises when there are no scene details (the reconciler cools down and retries).
    from .prose import write_prose_dialogue_spine
    details = (read_scene_details(show, episode) or {}).get("scenes") or []
    if not details:
        raise RuntimeError(
            f"episode {episode}: no scene details — approve the scenes first")
    scenes = [{"id": sd.get("id", f"s{i:02d}"),
               "location": sd.get("location", ""),
               "summary": sd.get("summary", ""),
               "narrative": sd.get("narrative", ""),
               "characters": sd.get("characters", [])}
              for i, sd in enumerate(details, start=1)]
    write_prose_dialogue_spine(show, episode, scenes)
    ep_summary = plan.get("plot") or plan.get("summary", "")
    episodes: list[dict[str, Any]] = []
    cast_names = [c.get("name") for c in (show.read_character(cid) for cid in show.list_characters())
                  if c.get("name")]
    runs = show.dir / "runs" / f"EP{episode:02d}"
    runs.mkdir(parents=True, exist_ok=True)
    sc_dir = _scenes_dir(show, episode)
    sc_dir.mkdir(parents=True, exist_ok=True)
    # The dialogue spine was written from the reviewed prose (verbatim lines,
    # cast-resolved) — the shot blocker distributes it. Never regenerate it
    # here: there is no second dialogue writer on this path. A scene the prose
    # omitted falls back to the per-scene dialogue pass inside the shot loop.
    dialogue_scenes = read_episode_dialogue(show, episode)
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
        # A scene present in the episode spine gets its verbatim lines; a scene the
        # writer omitted falls back to the legacy dialogue pass.
        spine = dialogue_scenes.get(sid) if dialogue_scenes else None
        ACTIVITY[show.show_id] = {"detail": f"Episode {episode}: writing shots for {sid}…",
                                  "ts": time.time()}
        scene = generate_scene_shots(show, episode, ps, llm=llm, cfg=cfg,
                                     episode_summary=ep_summary, notes=notes,
                                     dialogue_spine=spine)
        for pf in sc_dir.glob(f"{sid}.p*.json"):
            pf.unlink(missing_ok=True)
        epstate.write_json(ck, scene)
        episodes.append(scene)
    script = {"episode": episode, "summary": ep_summary,
              "cast": plan.get("characters") or plan.get("cast") or cast_names,
              "scenes": episodes}
    script = _normalize(script, cast_names)
    epstate.save_script(runs / "script.r1.json", script)
    script = _writer_review(show, episode, script, llm=llm, cfg=cfg,
                            max_revisions=max_revisions)
    # The approved dialogue is IMMUTABLE: the review/revise pass may touch action
    # and prose, but never the lines. Restore them from the shot checkpoints (the
    # spine distribution) and re-save the final revision.
    _restore_dialogue(script, sc_dir, spine=dialogue_scenes)
    _finalize_on_camera_script(script)
    # The writers'-room revision can rewrite `references.location_id` (e.g. split
    # one shared set into several named sets) without creating the registry
    # entries, leaving dangling ids whose refs never render. Ensure each
    # shot points at a REAL entry (registering it — filesystem only, no GPU) and
    # re-derive the on-screen cast/type from the final dialogue.
    try:
        _repair_shot_locations(show, script)
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "shot-location repair failed for %s EP%02d: %s", show.show_id, episode, exc)
    _scripts = sorted(runs.glob("script.r*.json"),
                      key=lambda p: int(p.stem.rsplit("r", 1)[-1]))
    if _scripts:
        epstate.save_script(_scripts[-1], script)
    # Distill this episode's durable facts into continuity (idempotent). Best
    # effort: a failure here must never break script assembly.
    try:
        extract_continuity(show, episode, script, llm=llm, cfg=cfg)
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "continuity extraction failed for %s EP%02d: %s", show.show_id, episode, exc)
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

    # Known canonical wardrobe per character = existing refs.json variants, plus
    # the "nude" appearance state (a selectable state, not a costume) — so the
    # reconciler reuses it verbatim rather than inventing one.
    from .refs import NUDE_LABEL
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
        if NUDE_LABEL not in variants:
            variants.append(NUDE_LABEL)
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
            out = promptlog.logged_chat_json(
                llm, show, episode, f"costume-reconcile-{name}",
                [{"role": "system", "content": prompts.showrunner_system(
                    show.style_profile(cfg), (show.read_bible() or {}).get("content_policy", "mature"),
                    show.style_profile(cfg).get("baseline", ""))},
                 {"role": "user", "content": prompts.costume_reconcile_prompt(
                     char, known.get(name, []), candidates)}],
                model=model, role="showrunner", temperature=0.2, max_tokens=8192,
                on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
                    "detail": f"Episode {episode}: reconciling {name}'s costumes ({n} tokens…)",
                    "output": t[-500:], "ts": time.time()}}))
        except Exception as exc:
            # A failed call must not silently let every label through to the
            # ref generator. Log it; the labels stay unmapped (kept) but the
            # transient guard above already dropped the wasteful ones.
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "costume reconcile LLM failed for %s/%s EP%02d: %s",
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

    # Scene wardrobe propagation: the shot writer labels only a FEW shots, so most
    # shots default to BASE even though the whole scene shares one outfit. Fill each
    # shot's costumes from the SCENE's dominant label per character, for every shot
    # where that character is on screen and has no label of their own.
    from collections import Counter as _Counter
    for _sc in script.get("scenes", []):
        _shots = _sc.get("shots", [])
        _counts: dict[str, Any] = {}
        for _sh in _shots:
            for _nm, _lb in (((_sh.get("references") or {}).get("costumes") or {}) or {}).items():
                _lb = str(_lb).strip()
                if _lb and _lb.lower() != "base" and not is_transient_costume_label(_lb):
                    _counts.setdefault(_nm, _Counter())[_lb] += 1
        _dom = {nm: c.most_common(1)[0][0] for nm, c in _counts.items() if c}
        for _sh in _shots:
            _refs = _sh.setdefault("references", {})
            _costs = _refs.get("costumes") or {}
            _onscreen = list(_refs.get("characters") or [])
            for _d in (_sh.get("dialogue") or []):
                if isinstance(_d, dict) and _d.get("char") and _d["char"] not in _onscreen:
                    _onscreen.append(_d["char"])
            for _nm in _onscreen:
                if _costs.get(_nm):
                    continue
                if _nm in _dom:
                    _costs[_nm] = _dom[_nm]
            _refs["costumes"] = _costs

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
