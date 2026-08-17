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

import json
import logging
import shutil
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
from .render import RENDER_JOBS
from .show import Show

log = logging.getLogger(__name__)

_reconciling: set[str] = set()
_guard = threading.Lock()
# stage -> cooldown_until: avoids hammering a persistently-failing step.
_cooldowns: dict[tuple[str, int, str], float] = {}
_COOLDOWN_S = 120.0

# Per-episode pause flag. Persisted so it survives dashboard restarts.
_paused_episodes: set[tuple[str, int]] = set()
_paused_lock = threading.Lock()

# Transient per-episode hold for MANUAL background rebuilds (dashboard approve /
# regenerate-shots threads). While held, the reconciler skips the episode so it
# never races the manual thread by running assemble/storyboard itself. Unlike
# pause_episode it is NOT persisted and does NOT clear running job dicts.
_manual_holds: set[tuple[str, int]] = set()
_manual_holds_lock = threading.Lock()


def hold_manual_rebuild(show_id: str, episode: int) -> None:
    with _manual_holds_lock:
        _manual_holds.add((show_id, episode))


def release_manual_rebuild(show_id: str, episode: int) -> None:
    with _manual_holds_lock:
        _manual_holds.discard((show_id, episode))


def _paused_path(show: Show) -> Any:
    return show.dir / "runs" / ".paused.json"


def _load_paused(show: Show) -> None:
    global _paused_episodes
    pp = _paused_path(show)
    if pp.exists():
        try:
            data = json.loads(pp.read_text(encoding="utf-8"))
            with _paused_lock:
                for entry in (data or []):
                    _paused_episodes.add((entry["show"], entry["episode"]))
        except Exception:
            pass


def _save_paused(show: Show) -> None:
    pp = _paused_path(show)
    pp.parent.mkdir(parents=True, exist_ok=True)
    with _paused_lock:
        entries = [{"show": s, "episode": e} for s, e in _paused_episodes if s == show.show_id]
    pp.write_text(json.dumps(entries), encoding="utf-8")


def is_episode_paused(show_id: str, episode: int) -> bool:
    show = Show(show_id)
    _load_paused(show)
    with _paused_lock:
        return (show_id, episode) in _paused_episodes


def pause_episode(show_id: str, episode: int) -> None:
    from .storyboard import STORYBOARD_JOBS
    show = Show(show_id)
    _load_paused(show)
    with _paused_lock:
        _paused_episodes.add((show_id, episode))
    _save_paused(show)
    # Clear any running storyboard/render job for this show so the UI updates.
    STORYBOARD_JOBS.pop(show_id, None)
    from .render import RENDER_JOBS
    RENDER_JOBS.pop(show_id, None)


def resume_episode(show_id: str, episode: int) -> None:
    show = Show(show_id)
    _load_paused(show)
    with _paused_lock:
        _paused_episodes.discard((show_id, episode))
    _save_paused(show)


def _episode_label(episode: int) -> str:
    return f"EP{episode:02d}"


def delete_episode(show_id: str, episode: int | str) -> list[str]:
    """Delete one episode's generated artifacts without regenerating it."""
    from .bootstrap import run_show_locked

    raw_episode = str(episode).strip().upper()
    if raw_episode.startswith("EP"):
        raw_episode = raw_episode[2:]
    try:
        ep = int(raw_episode)
    except ValueError as exc:
        raise ValueError(f"invalid episode label: {episode!r}") from exc
    if ep < 1:
        raise ValueError("episode must be a positive integer")
    show = Show(show_id)
    path = show.dir / "runs" / _episode_label(ep)
    if not path.is_dir():
        raise ValueError(f"episode {ep} does not exist")
    from .storyboard import STORYBOARD_JOBS
    from .render import RENDER_JOBS
    STORYBOARD_JOBS.pop(show_id, None)
    RENDER_JOBS.pop(show_id, None)
    with _paused_lock:
        _paused_episodes.discard((show_id, ep))
    _save_paused(show)

    def _delete() -> None:
        shutil.rmtree(path)
        for key in list(_cooldowns):
            if key[:2] == (show_id, ep):
                _cooldowns.pop(key, None)

    run_show_locked(show_id, _delete)
    return [f"EP{ep:02d} deleted"]


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


def _stale_job_s(cfg=None) -> float:
    """How long a storyboard/render job may go without any progress update before
    it is declared hung. Every sub-operation in those jobs is now bounded (each
    ComfyUI render carries a hard timeout that interrupts + raises), so no
    progress for this long genuinely means the worker is stuck."""
    cfg = cfg or get_config()
    return float(cfg.get("pipeline", "stale_job_s", 1800) or 1800)


def _job_stale(job: dict, cfg=None) -> bool:
    """True when a running job's last-progress stamp is older than the stale cap.

    A job without a ``ts`` stamp (legacy shape) is never treated as stale — we
    only act on jobs we can positively confirm have stalled.
    """
    ts = job.get("ts")
    if not ts:
        return False
    return time.time() - ts > _stale_job_s(cfg)


def _interrupt_renderers(cfg=None) -> None:
    """Best-effort interrupt of every configured ComfyUI instance.

    Called when a background job is declared hung. A wedged renderer keeps its
    prompt in queue_running forever, so the worker thread waiting on it never
    sees its own timeout — sending /interrupt clears the running prompt so the
    stuck wait() returns/raises promptly and the job can be restarted cleanly.
    """
    cfg = cfg or get_config()
    from .clients.comfy import ComfyClient
    urls: set[str] = set()
    for inst in (cfg.get("env", "comfyui", {}) or {}).values():
        if isinstance(inst, dict) and inst.get("url"):
            urls.add(inst["url"])
    for node in (cfg.get("comfy", "nodes", {}) or {}).values():
        if isinstance(node, dict) and node.get("url"):
            urls.add(node["url"])
    for url in sorted(urls):
        try:
            ComfyClient(url).interrupt()
            log.info("interrupted wedged ComfyUI at %s", url)
        except Exception:
            log.debug("interrupt failed for %s", url, exc_info=True)


def _comfy_wedged(url: str, max_age_s: float) -> bool:
    """True when a ComfyUI instance is hard-wedged: its port is occupied but it
    does not answer /system_stats (executor hung — /interrupt can't clear its
    queue), OR a queue_running job has been stuck for > ``max_age_s``.

    A responsive instance with a *fresh* running job is never wedged — that is a
    legitimate render and must not be restarted.
    """
    from .clients.comfy import ComfyClient
    try:
        if not ComfyClient(url).health():
            return True
    except Exception:
        return True
    try:
        import httpx
        with httpx.Client(timeout=5.0) as client:
            q = client.get(f"{url}/queue").json()
        now_ms = time.time() * 1000.0
        for job in (q.get("queue_running") or []):
            if len(job) > 3 and isinstance(job[3], (int, float)) \
                    and now_ms - float(job[3]) > max_age_s * 1000.0:
                return True
    except Exception:
        return False   # can't check the queue; don't assume wedged
    return False


def _restart_renderers(cfg=None) -> None:
    """Escalation for a HARD-wedged renderer: restart every configured local
    ComfyUI instance that is still wedged after an interrupt.

    A wedged executor (port occupied but no /system_stats answer, or a
    queue_running job stuck for ``pipeline.stale_job_s``) ignores /interrupt, so
    the only recovery is stop + relaunch via the node's run script — the fresh
    executor clears the stuck prompt, and the re-kicked job proceeds on it. The
    bounded waits make this safe: any in-flight prompt is simply retried on the
    next pass.

    Best-effort and never raises. Runs WITHOUT the in-process GPU lock (the hung
    worker thread already owns it), so it drives the instance lifecycle directly
    via ServiceOps; the cross-process port teardown is unaffected by the lock.
    """
    cfg = cfg or get_config()
    from .remote.ops import ServiceOps
    ops = ServiceOps(cfg)
    stale = _stale_job_s(cfg)
    for which in ("krea2", "h3"):
        inst = (cfg.get("env", "comfyui", {}) or {}).get(which) or {}
        url = inst.get("url", "")
        if not url:
            continue
        if not _comfy_wedged(url, stale):
            continue
        log.warning("ComfyUI '%s' (%s) is wedged; restarting it", which, url)
        try:
            ops.comfy_stop(which)
            ops.comfy_start(which)
            log.info("ComfyUI '%s' restarted", which)
        except Exception:
            log.warning("failed to restart ComfyUI '%s'", which, exc_info=True)


def _recover_renderers(cfg=None) -> None:
    """Two-stage recovery for a hung background job.

    Stage 1 (soft): interrupt every configured ComfyUI instance — a responsive
    executor abandons its stuck prompt and the worker's wait() returns/raises.
    Stage 2 (hard): if an instance is still wedged after the interrupt, restart
    it so the re-kicked job runs on a fresh executor.
    """
    _interrupt_renderers(cfg)
    _restart_renderers(cfg)


def _story_auto(cfg=None) -> bool:
    """Story-gate auto-approval: master switch or per-gate mode."""
    cfg = cfg or get_config()
    return bool(cfg.get("approval", "global", {}).get("auto_approve", False)) \
        or cfg.get("approval", "gates", {}).get("story") == "auto"


def _plan_auto(cfg=None) -> bool:
    """Plan (outline) gate auto-approval.

    Independent of the story gate so a human can approve just the plot before
    scene details + script are generated (fast iteration). Defaults to the story
    gate's behaviour when ``gates.plan`` is unset.
    """
    cfg = cfg or get_config()
    if cfg.get("approval", "global", {}).get("auto_approve", False):
        return True
    return cfg.get("approval", "gates", {}).get("plan",
        cfg.get("approval", "gates", {}).get("story", "auto")) == "auto"


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
    keyframes = []
    sbd = d / "storyboard"
    if sbd.exists():
        # Storyboard "done" = every shot has a 1s preview mp4 (new) OR a legacy
        # keyframe png. Count distinct shot ids so png+mp4 of one shot = 1.
        sids = set(p.stem for p in sbd.glob("*.mp4")) | set(p.stem for p in sbd.glob("*.png"))
        keyframes = sorted(sids)
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
    if is_episode_paused(show_id, episode):
        return False
    with _manual_holds_lock:
        if (show_id, episode) in _manual_holds:
            return False
    st = _stage_state(show, episode)
    auto = _story_auto()
    plan_auto = _plan_auto()
    if not st["has_plan"]:
        return True
    if st["plan_status"] == "rejected":
        # A rejected outline must be REGENERATED from its rejection notes — never
        # left stranded. The reconciler picks it up and rewrites it.
        return True
    if st["plan_status"] != "approved":
        return plan_auto      # plan gate gated & not auto -> blocked on human
    if not st["has_details"] or st["missing_detail"]:
        return True
    if st["details_status"] == "rejected":
        # Rejected scene details are regenerated from notes in place.
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
    plan_auto = _plan_auto()
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
        plan = read_episode_plan(show, episode) or {}
        # A REJECTED outline must regenerate from its rejection notes, never be
        # rubber-stamped back to approved (and never left stranded by the gate).
        # This runs regardless of the plan gate — a rejection is an instruction
        # to regenerate, not a "wait for human" state.
        if st["plan_status"] == "rejected":
            if not plan.get("rejected_notes"):
                log.warning("EP%02d plan rejected with no notes; waiting for human", episode)
                return log_
            if _in_cooldown(show_id, episode, "plan"):
                return log_
            ACTIVITY[show_id] = {"detail": f"Episode {episode}: rewriting plan from rejection notes…",
                                 "ts": time.time()}
            try:
                generate_episode_plan(show, episode, notes=plan["rejected_notes"])
                log_.append(f"EP{episode:02d}: plan regenerated from rejection notes")
            except Exception as exc:
                log.warning("plan rewrite failed for %s EP%02d: %s", show_id, episode, exc)
                _mark_cooldown(show_id, episode, "plan")
                return log_
            st = _stage_state(show, episode)
            plan = read_episode_plan(show, episode) or {}
        if not plan_auto:
            return log_      # plan gate -> wait for human outline approval
        # Never auto-approve an outline that failed structural review — halt for a
        # human instead of letting a broken outline drive script + storyboard.
        if (plan.get("plan_review") or {}).get("passed") is False:
            log.warning("EP%02d plan failed structural review; holding for human", episode)
            return log_
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
    if st["details_status"] == "rejected":
        # Rejected scene details regenerate from their rejection notes in place —
        # never auto-approve a stale rejected treatment.
        details = read_scene_details(show, episode)
        notes = (details or {}).get("rejected_notes", "") or ""
        if _in_cooldown(show_id, episode, "details"):
            return log_
        ACTIVITY[show_id] = {"detail": f"Episode {episode}: rewriting scene details from rejection notes…",
                             "ts": time.time()}
        try:
            generate_scene_details(show, episode, notes=notes)
            log_.append(f"EP{episode:02d}: scene details regenerated from rejection notes")
        except Exception as exc:
            log.warning("scene details rewrite failed for %s EP%02d: %s", show_id, episode, exc)
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
    #    for this show (jobs are keyed per show, not per episode). A job whose
    #    worker thread is dead is a zombie — restart it so a crash can't stall
    #    the episode forever. A job whose worker is ALIVE but has made no
    #    progress for ``pipeline.stale_job_s`` is a hung renderer (wedged
    #    ComfyUI / deadlock) — interrupt the instances to un-block its wait,
    #    then restart the job. Also skipped entirely when
    #    pipeline.pause_storyboard is set (debugging).
    from .storyboard import storyboard_status
    from .render import render_status
    if get_config().get("pipeline", "pause_storyboard", False):
        return log_
    if st["keyframe_count"] < st["shot_count"]:
        sb = storyboard_status(show_id)
        if sb.get("state") in ("running", "waiting"):
            if sb.get("state") == "running" and sb.get("alive") is False:
                # A "running" job whose worker thread died is a true zombie —
                # restart it so a crash can't stall the episode forever.
                log.warning("storyboard job %s is a zombie (thread dead); restarting", show_id)
            elif sb.get("state") == "running" and _job_stale(sb):
                # A live worker stuck with no progress: the renderer is wedged
                # (job parked in queue_running forever). Interrupt the instances
                # and restart any that are hard-wedged, stop the job so the
                # re-entrancy guard lets a fresh one start, then re-kick below.
                log.warning("storyboard job %s has made no progress for %.0fs; "
                            "recovering renderers and restarting",
                            show_id, time.time() - (sb.get("ts") or time.time()))
                _recover_renderers()
                from .storyboard import stop_storyboard
                stop_storyboard(show_id)
            elif sb.get("state") == "waiting":
                # Parked on the ref-approval gate. Resume ONLY once the gate has
                # actually cleared (a ref was just approved) — otherwise skip.
                # Restarting a parked job on every poll would re-run the whole
                # ref phase (reconcile + LLM object extraction + refs) endlessly
                # while the human reviews the refs.
                from .storyboard import pending_ref_approvals
                script_latest = _script_latest(show, episode)
                if script_latest is None:
                    return log_
                if pending_ref_approvals(show, episode, script_latest):
                    return log_      # still blocked on a human; leave it parked
                log.info("storyboard ref gate cleared for %s; resuming", show_id)
            else:
                return log_      # genuinely working; don't re-kick it
        if _in_cooldown(show_id, episode, "storyboard"):
            return log_
        if not st["shot_count"]:
            return log_
        ACTIVITY[show_id] = {"detail": f"Episode {episode}: storyboard…", "ts": time.time()}
        try:
            build_storyboard(show, episode)
            log_.append(f"EP{episode:02d}: storyboard started ({st['keyframe_count']}/{st['shot_count']} previews)")
        except Exception as exc:
            log.warning("storyboard failed to start for %s EP%02d: %s", show_id, episode, exc)
            _mark_cooldown(show_id, episode, "storyboard")
        return log_

    # 5) Render (background job). Skip if a render job is already running.
    #    build_render itself re-checks the ref-approval gate and parks in
    #    "waiting" until the refs clear, so a render started while refs are
    #    unapproved never burns GPU on them. A render whose worker thread died
    #    is a zombie — restart it; one that has made no progress for
    #    pipeline.stale_job_s is a hung renderer — interrupt + restart.
    if st["video_count"] < st["shot_count"]:
        rd = render_status(show_id)
        if rd.get("state") == "running":
            if rd.get("alive") is False:
                log.warning("render job %s is a zombie (thread dead); restarting", show_id)
            elif _job_stale(rd):
                log.warning("render job %s has made no progress for %.0fs; "
                            "recovering renderers and restarting",
                            show_id, time.time() - (rd.get("ts") or time.time()))
                _recover_renderers()
                RENDER_JOBS.pop(show_id, None)
            else:
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
    A deleted episode that leaves a gap will be backfilled first.
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

    # 2) All existing episodes are complete. Check for gaps first (deleted
    #    episodes that need backfilling), then start the next.
    auto_start = cfg.get("pipeline", "auto_start_episode", True)
    if auto_start and _story_auto(cfg):
        # Find the first missing episode number (gap from deletion).
        next_ep = 1
        for ep in eps:
            if ep == next_ep:
                next_ep += 1
            elif ep > next_ep:
                break
        log_.extend(reconcile_episode(show_id, next_ep, show))
    return log_


def needs_any_episode_reconcile(show_id: str, show: Show | None = None) -> bool:
    """True when any episode of the show needs work (or a NEW episode should start
    or a deleted episode leaves a gap to backfill).

    A new episode only auto-starts when the show is hands-free AND either the
    latest episode is fully produced OR there's a gap in the sequence.
    """
    show = show or Show(show_id)
    if not (show.bootstrap_state() or {}).get("complete"):
        return False
    if get_config().get("pipeline", "pause_storyboard", False):
        return False
    for ep in _episode_dirs(show):
        if needs_episode_reconcile(show_id, ep, show):
            return True
    cfg = get_config()
    auto_start = cfg.get("pipeline", "auto_start_episode", True)
    if auto_start and _story_auto(cfg):
        eps = _episode_dirs(show)
        # Gap in the sequence (e.g. EP01 deleted but EP02–05 exist).
        for n in range(1, (eps[-1] + 1) if eps else 2):
            if n not in eps:
                return True
    return False


def is_episode_reconciling(show_id: str) -> bool:
    """True while a background episode-reconcile thread for this show is alive.

    Lets the dashboard distinguish 'working right now' from a stale ACTIVITY
    detail left behind by a job that already finished.
    """
    with _guard:
        return show_id in _reconciling


def reconcile_episodes_if_stalled(show_id: str) -> bool:
    """Background self-healing for the episode pipeline, once per show.

    Returns True when a reconcile thread was started. No-op when the show is not
    bootstrap-complete, has no work, or is already being reconciled.
    """
    # Dashboard polling is concurrent; claim the show atomically before doing
    # any filesystem or LLM work so duplicate threads cannot be spawned.
    with _guard:
        if show_id in _reconciling:
            return False
        _reconciling.add(show_id)
    try:
        if not needs_any_episode_reconcile(show_id):
            with _guard:
                _reconciling.discard(show_id)
            return False
    except Exception:
        log.exception("episode reconcile precheck failed for %s", show_id)
        with _guard:
            _reconciling.discard(show_id)
        return False

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
