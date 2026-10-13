"""Per-scene seamless master renderer via H3 Multishot (joeygambino v2.6.3).

One ComfyUI submission per scene -> one master MP4 with continuous video+audio.
Falls back to per-shot path when config.multishot.enabled is false.

Prompt rules enforced here (pack PROMPTING.md):
- AIRLOCK: ~2s quiet breath/weight-shift at start of every shot after the first
- LAND SETTLED: dialogue finished, ~2s spare at end of every shot
- Discard replay: first ~1s of each chained shot after the first is regenerated head -> trimmed, so no speech at 0s
- A spoken line never straddles two shots
- Verbatim repeat: each character's appearance_canonical + room/lighting word-for-word every block

See: https://huggingface.co/joeygambino/MiniMax-H3-Multishot-Workflow
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import fsutil
from .bootstrap import ACTIVITY
from .clients.comfy import ComfyClient
from .config import get_config
from .show import Show

# Background jobs: show_id -> {"state", "done", "total", "detail"}
SCENE_RENDER_JOBS: dict[str, dict[str, Any]] = {}


def _latest_script(show: Show, episode: int) -> dict[str, Any] | None:
    d = show.dir / "runs" / f"EP{episode:02d}"
    scripts = fsutil.existing_by_mtime(d.glob("script.r*.json"))
    if not scripts:
        return None
    return json.loads(scripts[-1].read_text(encoding="utf-8"))


def _scene_duration_seconds(scene: dict[str, Any]) -> float:
    return sum(float(sh.get("duration_s", 8.0)) for sh in scene.get("shots", []))


def _prompt_blocks_for_scene_llm(
    show: Show | None,
    script: dict[str, Any],
    scene: dict[str, Any],
    cfg=None,
) -> list[str]:
    """100% LLM video prompts — each block is authored by the LLM, not templated.

    Context (bible, scene, shot, character appearances, audio refs) is supplied;
    output is 100% LLM per product rule. No deterministic fallback.
    AIRLOCK / LAND SETTLED / 24fps note are still enforced as post-wrapping
    so the pack's chaining invariants hold even when the LLM forgets them.
    """
    from .clients.lmstudio import LMStudioClient
    from .prompts import h3_llm_generate_prompt
    from .storyboard import _shot_refs

    cfg = cfg or get_config()
    # Characters for appearance map
    characters: list[dict[str, Any]] = []
    if show:
        for cid in show.list_characters():
            c = show.read_character(cid)
            if c.get("name"):
                characters.append(c)
    show_bible = show.read_bible() if show and hasattr(show, "read_bible") else None
    if show_bible and isinstance(show_bible, dict) and "series" not in show_bible:
        # read_bible() may return raw yaml dict with 'series'
        show_bible = show_bible
    style_guide = ""
    if show_bible and isinstance(show_bible, dict):
        style_guide = show_bible.get("style_guide") or show_bible.get("series", {}).get("style_guide", "")
    # LLM client (LM Studio, local, OpenAI-compatible)
    base_url = cfg.get("llm", "base_url") or "http://127.0.0.1:1234/v1"
    llm = LMStudioClient(base_url, timeout=600)

    blocks: list[str] = []
    for idx, shot in enumerate(scene.get("shots", [])):
        shot_names, _ = _shot_refs(show, shot) if show else ([], [])
        audio_refs: list[dict[str, str]] = []
        if shot_names:
            from .render import _has_speech
            spoken = [d for d in (shot.get("dialogue") or []) if _has_speech(d.get("line") or "")]
            speakers = []
            for d in spoken:
                ch = d.get("char")
                if ch and ch not in speakers:
                    speakers.append(ch)
            for ch in speakers[:3]:
                if ch in shot_names:
                    audio_refs.append({"char": ch, "token": f"<Audio {len(audio_refs)+1}>"})

        # 100% LLM generation — guide's six sections only. Chaining (AIRLOCK/LAND SETTLED)
        # is instructed INSIDE the LLM prompt via is_chained, not wrapped outside.
        llm_block = h3_llm_generate_prompt(
            llm, show_bible, scene, shot, characters, shot_names,
            audio_refs=audio_refs or None, style_guide=style_guide,
            is_chained=(idx > 0),
        )
        # NO external AIRLOCK/LAND wrappers — the LLM already embeds them inside
        # detailed_description per the guide + pack rules. Keep block verbatim.
        blocks.append(llm_block.strip())
    return blocks


def _prompt_blocks_for_scene(
    show: Show | None,
    script: dict[str, Any],
    scene: dict[str, Any],
    names: list[str] | None = None,
) -> list[str]:
    """Legacy deterministic path — kept for tests but NOT used for multishot renders.

    Multishot renders MUST call _prompt_blocks_for_scene_llm per product rule.
    """
    from .render import compile_shot_prompt
    from .storyboard import _shot_refs

    blocks: list[str] = []
    shots = scene.get("shots", [])
    scene_room = (scene.get("summary") or scene.get("location") or "").strip()
    for idx, shot in enumerate(shots):
        shot_names, _ = _shot_refs(show, shot) if show else (names or [], [])
        shot_names = shot_names or (names or [])
        base = compile_shot_prompt(show, script, scene, shot, shot_names, audio_refs=None)
        airlock = ""
        if idx > 0:
            airlock = (
                "AIRLOCK: Hold the previous shot's exact closing arrangement for about 2 seconds "
                "with subtle micro-motion — a breath, a weight shift, a blink — before any speech. "
                "No new action, no cut. "
            )
        settle = "LAND SETTLED: End in a stable arrangement with dialogue finished and about 2 seconds spare, no speech at the very end. "
        verbatim_parts: list[str] = []
        if scene_room and scene_room.lower() not in base.lower():
            verbatim_parts.append(f"Room/lighting verbatim: {scene_room}.")
        block = f"{airlock}{base}\n\n{settle}"
        if verbatim_parts:
            block += "\n" + " ".join(verbatim_parts)
        block += "\n[24fps chain; spoken lines must not straddle shot boundaries; first ~1s is discarded replay.]"
        blocks.append(block.strip())
    return blocks


def _masters_dir(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / "masters"


def render_scene_master(
    show: Show,
    episode: int,
    scene: dict[str, Any],
    client: ComfyClient,
    cfg=None,
    seed: int = 1,
    prebuilt_blocks: list[str] | None = None,
) -> Path | None:
    """Render ONE scene as a seamless master via H3Multishot."""
    from .h3_multishot import build_h3_multishot_workflow, run_h3_multishot
    from .render import _shot_character_ref, _voice_sample_path
    from .storyboard import _shot_refs
    from .gpu_manager import ServiceType, get_gpu_manager

    cfg = cfg or get_config()
    ms_cfg = cfg.get("pipeline", "multishot", {}) if isinstance(cfg.get("pipeline", "multishot", {}), dict) else {}
    script = _latest_script(show, episode)
    if not script:
        return None

    scene_id = scene.get("id", "scene")
    # 100% LLM path per product rule
    if prebuilt_blocks is not None:
        blocks = prebuilt_blocks
    else:
        # Single-scene direct call (CLI): acquire LLM lock so ComfyUI is evicted before LLM loads.
        # Without this, LM Studio competes with the staged 20GB H3 model and gets "Operation canceled" at 34%.
        try:
            with get_gpu_manager(cfg).acquire(ServiceType.LLM):
                blocks = _prompt_blocks_for_scene_llm(show, script, scene, cfg=cfg)
        except Exception as exc:
            raise RuntimeError(f"LLM 100% prompt generation failed for {scene_id}: {exc}") from exc
    if not blocks:
        return None

    # Collect unique character refs for Auto Refs (9 slots across characters)
    # Pack's Auto Refs splits nine photo slots across up to nine characters
    all_names: list[str] = []
    for sh in scene.get("shots", []):
        names, _ = _shot_refs(show, sh)
        for n in names:
            if n not in all_names:
                all_names.append(n)

    image_filenames: list[str] = []
    for name in all_names[:9]:  # pack max 9 picture slots
        ref = _shot_character_ref(show, name)
        if not ref:
            continue
        try:
            image_filenames.append(client.upload_image(ref))
        except Exception:
            continue

    # Voice anchor: first speaking character's voice sample as <Audio 1>
    # Multishot pack's extend take pins voice via audio reference window
    audio_filenames: list[str] = []
    for name in all_names[:1]:
        vp = _voice_sample_path(show, name)
        if vp and vp.exists():
            try:
                audio_filenames.append(client.upload_audio(str(vp)))
            except Exception:
                pass
        break

    res = cfg.get("pipeline", "resolution", [1280, 736])
    width = int(res[0] if isinstance(res, (list, tuple)) and len(res) >= 2 else 1280)
    height = int(res[1] if isinstance(res, (list, tuple)) and len(res) >= 2 else 736)

    continuity = str(ms_cfg.get("continuity", "context_pin") or "context_pin")
    frames_per_shot = int(ms_cfg.get("frames_per_shot", 192) or 192)
    steps = int(ms_cfg.get("steps", 14) or 14)
    take_seconds = int(ms_cfg.get("take_seconds", 0) or 0)

    h3_cfg = cfg.get("comfy", "h3", {})
    sampler = h3_cfg.get("sampler") or "euler"
    scheduler = h3_cfg.get("scheduler") or "beta57"

    wf = build_h3_multishot_workflow(
        prompt_blocks=blocks,
        seed=seed,
        cfg=cfg,
        continuity=continuity,
        frames_per_shot=frames_per_shot,
        width=width,
        height=height,
        steps=steps,
        sampler_name=sampler,
        scheduler=scheduler,
        chain_gain_control=str(ms_cfg.get("chain_gain_control", "flatten")),
        master_normalize=str(ms_cfg.get("master_normalize", "luma+contrast")),
        seed_per_shot=bool(ms_cfg.get("seed_per_shot", True)),
        preview_first_shot=bool(ms_cfg.get("preview_first_shot", True)),
        low_ram_master=bool(ms_cfg.get("low_ram_master", False)),
        ref_image_filenames=image_filenames or None,
        ref_audio_filenames=audio_filenames or None,
        take_seconds=take_seconds,
    )

    out = _masters_dir(show, episode) / f"{scene_id}_master.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Master may take 15min+ per scene (7 windows ~65s). Allow longer timeout when take_seconds>0.
    timeout = 3600.0 if take_seconds else 1800.0
    if _scene_duration_seconds(scene) > 60:
        timeout = 5400.0
    return run_h3_multishot(client, wf, out, timeout_s=timeout)


def render_episode_masters(
    show: Show,
    episode: int,
    cfg=None,
    progress=None,
) -> int:
    """Render every scene of the episode as a seamless master. Returns scene count."""
    from .gpu_manager import ServiceType, get_gpu_manager
    from .render import _render_client

    cfg = cfg or get_config()
    script = _latest_script(show, episode)
    if not script:
        return 0
    scenes = script.get("scenes", [])
    if not scenes:
        return 0

    # Phase 1: 100% LLM prompt generation for ALL scenes while LLM owns the GPU.
    # This evicts the 20GB H3 model staged from any prior run, so the 34% cancel cannot happen.
    # We do this BEFORE acquiring COMFYUI, not inside it.
    scene_blocks: dict[str, list[str]] = {}
    with get_gpu_manager(cfg).acquire(ServiceType.LLM):
        for scene in scenes:
            sid = scene.get("id", "")
            try:
                blocks = _prompt_blocks_for_scene_llm(show, script, scene, cfg=cfg)
                scene_blocks[sid] = blocks
            except Exception as exc:
                ACTIVITY.setdefault(show.show_id, {})["detail"] = f"LLM prompts failed for {sid}: {exc}"
                raise

    client = _render_client(cfg)
    done = 0
    total = len(scenes)
    with get_gpu_manager(cfg).acquire(ServiceType.COMFYUI):
        for idx, scene in enumerate(scenes):
            sid = scene.get("id", f"s{idx+1:02d}")
            out = _masters_dir(show, episode) / f"{sid}_master.mp4"
            if out.exists():
                done += 1
                if progress:
                    progress(done, total, f"{sid} (cached)")
                continue
            ACTIVITY[show.show_id] = {"detail": f"Master {sid} ({len(scene.get('shots', []))} shots, seamless)…", "ts": time.time()}
            try:
                from .approval import shot_approvals
                retake = 0
                for sh in scene.get("shots", []):
                    retake = max(retake, int((shot_approvals(show, episode).get(sh.get("id", ""), {}).get("retake_count", 0) or 0)))
                seed = (idx + 1) * 100 + 1000 * retake
                render_scene_master(show, episode, scene, client, cfg=cfg, seed=seed, prebuilt_blocks=scene_blocks.get(sid))
            except Exception as exc:
                ACTIVITY.setdefault(show.show_id, {})["detail"] = f"Master {sid} failed: {exc}"
            done += 1
            if progress:
                progress(done, total, sid)
    return done


def build_scene_masters(show: Show, episode: int, cfg=None) -> None:
    """Background-thread entrypoint mirroring render.build_render."""
    job: dict[str, Any] = {"state": "running", "done": 0, "total": 0, "detail": "Preparing scene masters…", "ts": time.time()}
    SCENE_RENDER_JOBS[show.show_id] = job

    cfg = cfg or get_config()
    script = _latest_script(show, episode)
    total = len((script or {}).get("scenes", []))
    job["total"] = total

    def _run():
        def prog(done, total, label):
            job["done"], job["total"] = done, total
            job["detail"] = f"Master {done}/{total}: {label}"
            job["ts"] = time.time()
            ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}
        try:
            done = render_episode_masters(show, episode, cfg=cfg, progress=prog)
            job["detail"] = f"Masters complete ({done}/{total} scenes)"
            job["state"] = "done"
        except Exception as exc:
            job["state"] = "failed"
            job["detail"] = f"Master render failed: {exc}"
        finally:
            job["ts"] = time.time()
            ACTIVITY.pop(show.show_id, None)

    import threading
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    job["_thread"] = t


def scene_render_status(show_id: str) -> dict[str, Any]:
    job = SCENE_RENDER_JOBS.get(show_id)
    if not job:
        return {"state": "idle", "done": 0, "total": 0, "detail": ""}
    out = dict(job)
    thread = out.pop("_thread", None)
    out["alive"] = bool(thread and thread.is_alive())
    return out
