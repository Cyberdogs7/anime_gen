"""Episode video rendering: storyboard keyframes -> H3 video shots (DESIGN §10.3).

Turns an approved episode script + its storyboard keyframes into video. Each shot
is one MiniMax H3 generation:

  1. compile the shot's MiniMax-notation prompt (deterministic, no LLM),
  2. upload the character refs + the shot's storyboard keyframe as ref2va inputs,
  3. build the H3 Director workflow, run it, save the MP4 to runs/EP##/video/.

Runs in a background thread with progress (mirrors the storyboard job model).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .bootstrap import ACTIVITY
from .clients.comfy import ComfyClient
from .compile.h3_prompt import compile_h3_prompt
from .config import get_config
from .show import Show

# Background jobs: show_id -> {"state", "done", "total", "detail"}
RENDER_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _latest_script(show: Show, episode: int) -> dict[str, Any] | None:
    d = show.dir / "runs" / f"EP{episode:02d}"
    scripts = sorted(d.glob("script.r*.json"), key=lambda p: p.stat().st_mtime)
    if not scripts:
        return None
    return json.loads(scripts[-1].read_text(encoding="utf-8"))


def _render_client(cfg=None) -> ComfyClient:
    cfg = cfg or get_config()
    node = cfg.get("comfy", "nodes", {}).get("renderer", {})
    return ComfyClient(node.get("url", "http://127.0.0.1:8188"),
                       node.get("api_key"))


def _subjects_for(names: list[str]) -> tuple[list[str], list[str]]:
    """Map character names to H3 <Subject N> tokens + <Picture N> definitions."""
    definitions = [f"<Subject {i + 1}> is the character shown in <Picture {i + 1}>."
                   for i in range(len(names))]
    tokens = [f"<Subject {i + 1}>" for i in range(len(names))]
    return tokens, definitions


def compile_shot_prompt(script: dict[str, Any], scene: dict[str, Any],
                        shot: dict[str, Any], names: list[str]) -> str:
    """Deterministic H3 prompt for one shot (compile_h3_prompt, no LLM)."""
    tokens, definitions = _subjects_for(names)
    dialogue = " ".join(d.get("line", "") for d in (shot.get("dialogue") or [])
                        if d.get("line"))
    global_desc = (script.get("summary") or "").strip()[:400]
    single = {
        "id": shot.get("id", "shot"),
        "action": (shot.get("action") or "").strip(),
        "duration_s": float(shot.get("duration_s", 10.125)),
        "camera": shot.get("camera", ""),
        "dialogue": dialogue,
        "subjects": tokens or None,
    }
    return compile_h3_prompt(
        global_description=global_desc or f"{scene.get('summary', '')}".strip(),
        shots=[single],
        subject_definitions=definitions,
        soundscape=shot.get("soundscape", ""),
        music=shot.get("music", ""),
    )


def _render_shot(show: Show, episode: int, client: ComfyClient, cfg,
                 script: dict[str, Any], scene: dict[str, Any],
                 shot: dict[str, Any], seed: int,
                 timeout_s: float = 1800.0) -> Path:
    from .h3 import build_h3_shot_workflow, run_h3_shot
    sid = shot.get("id", "shot")
    names, _ = _shot_refs(show, shot)
    prompt = compile_shot_prompt(script, scene, shot, names)

    ref_filenames = []
    for p in _shot_ref_paths(show, episode, scene, shot):
        try:
            ref_filenames.append(client.upload_image(p))
        except Exception:
            pass

    h3_cfg = cfg.get("comfy", "h3", {})
    wf = build_h3_shot_workflow(
        prompt, float(shot.get("duration_s", 10.125)), seed, cfg=cfg,
        segment_prompt=(shot.get("action") or "").strip(),
        use_ref2va=bool(ref_filenames), ref_images=ref_filenames or None,
        use_spectrum=h3_cfg.get("spectrum", True),
        use_first_block_cache=h3_cfg.get("first_block_cache", False),
        steps=int(h3_cfg.get("steps", 8)),
        sampler_name=h3_cfg.get("sampler") or "res_multistep",
    )
    out = show.dir / "runs" / f"EP{episode:02d}" / "video" / f"{sid}.mp4"
    return run_h3_shot(client, wf, out, timeout_s=timeout_s)


def render_episode(show: Show, episode: int, cfg=None, progress=None,
                   timeout_s: float = 1800.0) -> int:
    """Render every shot of the latest episode script to video. Returns shot count."""
    cfg = cfg or get_config()
    script = _latest_script(show, episode)
    if not script:
        return 0
    client = _render_client(cfg)
    shots = [s for sc in script.get("scenes", []) for s in sc.get("shots", [])]
    done = 0
    for sc in script.get("scenes", []):
        for shot in sc.get("shots", []):
            sid = shot.get("id", f"sh{done}")
            out = show.dir / "runs" / f"EP{episode:02d}" / "video" / f"{sid}.mp4"
            if out.exists():
                done += 1
                if progress:
                    progress(done, len(shots), sid)
                continue
            ACTIVITY[show.show_id] = {"detail": f"Rendering {sid} (H3 video)…",
                                      "ts": time.time()}
            try:
                _render_shot(show, episode, client, cfg, script, sc, shot,
                             seed=done, timeout_s=timeout_s)
            except Exception as exc:
                ACTIVITY.setdefault(show.show_id, {})["detail"] = (
                    f"Render {sid} failed: {exc}")
            done += 1
            if progress:
                progress(done, len(shots), sid)
    return done


def build_render(show: Show, episode: int, cfg=None) -> None:
    """Render all shots in a background thread with progress."""
    def _run():
        job = {"state": "running", "done": 0, "total": 0, "detail": "Preparing render…"}
        RENDER_JOBS[show.show_id] = job

        def prog(done, total, label):
            job["done"], job["total"] = done, total
            job["detail"] = f"Rendering {done}/{total}: {label}"
            ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}

        try:
            script = _latest_script(show, episode)
            total = len([s for sc in (script or {}).get("scenes", [])
                         for s in sc.get("shots", [])])
            job["total"] = total
            job["state"] = "running"
            done = render_episode(show, episode, cfg=cfg, progress=prog)
            job["state"] = "done"
            job["detail"] = f"Render complete ({done}/{total} shots)"
        except Exception as exc:
            job["state"] = "failed"
            job["detail"] = f"Render failed: {exc}"
        finally:
            ACTIVITY.pop(show.show_id, None)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def render_status(show_id: str) -> dict[str, Any]:
    return dict(RENDER_JOBS.get(show_id, {"state": "idle", "done": 0, "total": 0, "detail": ""}))
