"""Self-healing for the episode pipeline (plan -> details -> script -> storyboard -> render).

Mirrors bootstrap.reconcile_if_stalled for Gate 0, but for episodes: the dashboard
calls :func:`reconcile_episodes_if_stalled` on every show/activity poll, and it
resumes any episode that is incomplete but not blocked on a *gated* approval.

Each stage is idempotent and resumable (see planner.generate_scene_details /
assemble_episode_script), so a crash or a failed step never strands the episode —
the next reconcile pass picks up exactly where it stopped. When a step raises, it
is retried on later passes (with a cooldown so a permanently-failing step doesn't
hammer the LLM every poll).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import approval
from .bootstrap import ACTIVITY
from .config import get_config
from .planner import (
    approve_plan, approve_scene_details, assemble_episode_script,
    generate_episode_plan, generate_scene_details, read_episode_plan,
    read_scene_details,
)
from .show import Show

log = logging.getLogger(__name__)

_reconciling: set[str] = set()
_guard = threading.Lock()
# stage -> cooldown_until: avoids hammering a persistently-failing step.
_cooldowns: dict[tuple[str, int, str], float] = {}
_COOLDOWN_S = 120.0


def _episode_label(episode: int) -> str:
    return f"EP{episode:02d}"


def _episode_dirs(show: Show) -> list[int]:
    runs = show.dir / "runs"
    if not runs.exists():
        return []
    out = []
    for d in runs.iterdir():
        if d.is_dir() and d.name.startswith("EP"):
            try:
                out.append(int(d.name[2:]))
            except ValueError:
                continue
    return sorted(out)


def _in_cooldown(show_id: str, episode: int, stage: str) -> bool:
    return _cooldowns.get((show_id, episode, stage), 0.0) > time.time()


def _mark_cooldown(show_id: str, episode: int, stage: str) -> None:
    _cooldowns[(show_id, episode, stage)] = time.time() + _COOLDOWN_S


def _story_auto(cfg=None) -> bool:
    """Story-gate auto-approval: master switch or per-gate mode."""
    cfg = cfg or get_config()
    return bool(cfg.get("approval", "global", {}).get("auto_approve", False)) \
        or cfg.get("approval", "gates", {}).get("story") == "auto"


def _episode_complete(show: Show, episode: int) -> bool:
    """True when an episode is fully produced: script + every shot's keyframe + video."""
    st = _stage_state(show, episode)
    return (st["has_script"] and st["shot_count"] > 0
            and st["keyframe_count"] >= st["shot_count"]
            and st["video_count"] >= st["shot_count"])


def _script_latest(show: Show, episode: int) -> dict[str, Any] | None:
    d = show.dir / "runs" / _episode_label(episode)
    scripts = sorted(d.glob("script.r*.json"), key=lambda p: p.stat().st_mtime)
    if not scripts:
        return None
    try:
        return __import__("json").loads(scripts[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def _stage_state(show: Show, episode: int) -> dict[str, Any]:
    """Describe the episode's stage from disk: what exists and its status."""
    import json
    d = show.dir / "runs" / _episode_label(episode)
    plan = read_episode_plan(show, episode)
    details = read_scene_details(show, episode)
    script = _script_latest(show, episode)
    plan_scenes = [s for s in (plan.get("scenes") or []) if isinstance(s, dict) and s.get("id")]
    detail_ids = {s.get("id") for s in (details.get("scenes") or [])}
    missing_detail = [s.get("id") for s in plan_scenes if s.get("id") not in detail_ids]
    shots = [s for sc in (script or {}).get("scenes", []) for s in sc.get("shots", [])]
    keyframes = sorted((d / "storyboard").glob("*.png")) if (d / "storyboard").exists() else []
    videos = sorted((d / "video").glob("*.mp4")) if (d / "video").exists() else []
    return {
        "has_plan": bool(plan),
        "plan_status": (plan or {}).get("status", ""),
        "has_details": bool(details and details.get("scenes")),
        "details_status": (details or {}).get("status", ""),
        "missing_detail": missing_detail,
        "has_script": bool(script),
        "shot_count": len(shots),
        "keyframe_count": len(keyframes),
        "video_count": len(videos),
    }


def needs_episode_reconcile(show_id: str, episode: int, show: Show | None = None) -> bool:
    """True when this episode is incomplete but not blocked on a gated approval.

    Only the plan and scene-details carry human gates; when ``story`` is auto they
    auto-approve and the episode flows plan -> details -> script -> storyboard.
    """
    show = show or Show(show_id)
    if not (show.bootstrap_state() or {}).get("complete"):
        return False
    st = _stage_state(show, episode)
    auto = _story_auto()
    if not st["has_plan"]:
        return True
    if st["plan_status"] != "approved":
        return auto          # gated & not auto -> blocked on human
    if not st["has_details"] or st["missing_detail"]:
        return True
    if st["details_status"] != "approved":
        return auto          # gated & not auto -> blocked on human
    if not st["has_script"]:
        return True
    if st["keyframe_count"] < st["shot_count"]:
        return True
    if st["video_count"] < st["shot_count"]:
        return True
    return False


def _advance_episode(show: Show, episode: int) -> list[str]:
    """Advance ONE episode as far as approvals allow. Returns a log of actions."""
    import json
    from .storyboard import build_storyboard
    from .render import build_render

    log_: list[str] = []
    st = _stage_state(show, episode)
    auto = _story_auto()
    show_id = show.show_id

    # 1) Plan.
    if not st["has_plan"]:
        if _in_cooldown(show_id, episode, "plan"):
            return log_
        ACTIVITY[show_id] = {"detail": f"Episode {episode}: writing plan…", "ts": time.time()}
        try:
            generate_episode_plan(show, episode)
            log_.append(f"EP{episode:02d}: plan generated")
        except Exception as exc:
            log.warning("plan generation failed for %s EP%02d: %s", show_id, episode, exc)
            _mark_cooldown(show_id, episode, "plan")
            return log_
        st = _stage_state(show, episode)
    if st["plan_status"] != "approved":
        if not auto:
            return log_      # gated -> wait for human
        try:
            approve_plan(show, episode)
            log_.append(f"EP{episode:02d}: plan auto-approved")
        except Exception as exc:
            log.warning("plan approve failed: %s", exc)
            return log_

    # 2) Scene details (resumable: only missing scenes regenerate).
    if not st["has_details"] or st["missing_detail"]:
        if _in_cooldown(show_id, episode, "details"):
            return log_
        ACTIVITY[show_id] = {"detail": f"Episode {episode}: writing scene details…",
                             "ts": time.time()}
        try:
            generate_scene_details(show, episode)
            log_.append(f"EP{episode:02d}: scene details ({len(st['missing_detail'])} missing regenerated)"
                        if st["missing_detail"] else f"EP{episode:02d}: scene details generated")
        except Exception as exc:
            log.warning("scene details failed for %s EP%02d: %s", show_id, episode, exc)
            _mark_cooldown(show_id, episode, "details")
            return log_
        st = _stage_state(show, episode)
    if st["details_status"] != "approved":
        if not auto:
            return log_
        try:
            approve_scene_details(show, episode)
            log_.append(f"EP{episode:02d}: scene details auto-approved")
        except Exception as exc:
            log.warning("scene details approve failed: %s", exc)
            return log_

    # 3) Script.
    if not st["has_script"]:
        if _in_cooldown(show_id, episode, "script"):
            return log_
        ACTIVITY[show_id] = {"detail": f"Episode {episode}: assembling script…", "ts": time.time()}
        try:
            assemble_episode_script(show, episode)
            log_.append(f"EP{episode:02d}: script assembled")
        except Exception as exc:
            log.warning("script assembly failed for %s EP%02d: %s", show_id, episode, exc)
            _mark_cooldown(show_id, episode, "script")
            return log_
        st = _stage_state(show, episode)

    # 4) Storyboard (background job). Skip if a storyboard job is already running
    #    for this show (jobs are keyed per show, not per episode).
    from .storyboard import storyboard_status
    from .render import render_status
    if st["keyframe_count"] < st["shot_count"]:
        if storyboard_status(show_id).get("state") == "running":
            return log_      # already working; don't re-kick it
        if _in_cooldown(show_id, episode, "storyboard"):
            return log_
        if not st["shot_count"]:
            return log_
        ACTIVITY[show_id] = {"detail": f"Episode {episode}: storyboard…", "ts": time.time()}
        try:
            build_storyboard(show, episode)
            log_.append(f"EP{episode:02d}: storyboard started ({st['keyframe_count']}/{st['shot_count']} keyframes)")
        except Exception as exc:
            log.warning("storyboard failed to start for %s EP%02d: %s", show_id, episode, exc)
            _mark_cooldown(show_id, episode, "storyboard")
        return log_

    # 5) Render (background job). Skip if a render job is already running.
    if st["video_count"] < st["shot_count"]:
        if render_status(show_id).get("state") == "running":
            return log_
        if _in_cooldown(show_id, episode, "render"):
            return log_
        if not st["shot_count"]:
            return log_
        ACTIVITY[show_id] = {"detail": f"Episode {episode}: rendering video…", "ts": time.time()}
        try:
            build_render(show, episode)
            log_.append(f"EP{episode:02d}: render started ({st['video_count']}/{st['shot_count']} shots)")
        except Exception as exc:
            log.warning("render failed to start for %s EP%02d: %s", show_id, episode, exc)
            _mark_cooldown(show_id, episode, "render")
        return log_

    return log_


def reconcile_episode(show_id: str, episode: int, show: Show | None = None) -> list[str]:
    """Advance one episode under the show's Gate-0 lock (serializes with approve/reject)."""
    from .bootstrap import run_show_locked
    show = show or Show(show_id)
    return run_show_locked(show_id, lambda: _advance_episode(show, episode))


def reconcile_show_episodes(show_id: str, show: Show | None = None) -> list[str]:
    """Advance the episode pipeline STRICTLY SERIAL: only the oldest incomplete
    episode is worked on each pass, so the studio never runs two episodes at once.

    A brand-new episode is started only when the latest one is fully produced
    (config: pipeline.auto_start_episode, default true, plus story-gate auto).
    """
    show = show or Show(show_id)
    log_: list[str] = []
    if not (show.bootstrap_state() or {}).get("complete"):
        return log_
    cfg = get_config()
    eps = _episode_dirs(show)

    # 1) Oldest incomplete episode gets the work slot.
    for ep in eps:
        if needs_episode_reconcile(show_id, ep, show):
            log_.extend(reconcile_episode(show_id, ep, show))
            return log_

    # 2) All existing episodes are complete: start the next, if enabled.
    auto_start = cfg.get("pipeline", "auto_start_episode", True)
    if auto_start and _story_auto(cfg) and eps and _episode_complete(show, eps[-1]):
        next_ep = eps[-1] + 1
        log_.extend(reconcile_episode(show_id, next_ep, show))
    return log_


def needs_any_episode_reconcile(show_id: str, show: Show | None = None) -> bool:
    """True when any episode of the show needs work (or a NEW episode should start).

    A new episode only auto-starts when the show is hands-free AND the latest
    episode is fully produced (script + keyframes + video) — never while the
    current episode is still mid-flight, so the pipeline stays strictly serial.
    """
    show = show or Show(show_id)
    if not (show.bootstrap_state() or {}).get("complete"):
        return False
    for ep in _episode_dirs(show):
        if needs_episode_reconcile(show_id, ep, show):
            return True
    cfg = get_config()
    auto_start = cfg.get("pipeline", "auto_start_episode", True)
    if auto_start and _story_auto(cfg):
        eps = _episode_dirs(show)
        if eps and _episode_complete(show, eps[-1]):
            return True
    return False


def reconcile_episodes_if_stalled(show_id: str) -> bool:
    """Background self-healing for the episode pipeline, once per show.

    Returns True when a reconcile thread was started. No-op when the show is not
    bootstrap-complete, has no work, or is already being reconciled.
    """
    if show_id in _reconciling:
        return False
    try:
        if not needs_any_episode_reconcile(show_id):
            return False
    except Exception:
        log.exception("episode reconcile precheck failed for %s", show_id)
        return False
    _reconciling.add(show_id)

    def _run() -> None:
        try:
            reconcile_show_episodes(show_id)
        except Exception:
            log.exception("episode reconcile failed for %s", show_id)
        finally:
            ACTIVITY.pop(show_id, None)
            with _guard:
                _reconciling.discard(show_id)

    threading.Thread(target=_run, name=f"ep-reconcile-{show_id}", daemon=True).start()
    return True
