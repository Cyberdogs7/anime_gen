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


def _shot_ref_paths(show: Show, episode: int, scene: dict[str, Any],
                    shot: dict[str, Any]) -> list[str]:
    """Reference-image paths for a shot: object refs + the shot's storyboard
    keyframe (composition anchor). Character refs are NOT included here — they
    travel in the timeline's ``characters`` slots so the Director binds each to
    a <Subject N> definition."""
    from .storyboard import _shot_object_refs
    paths: list[str] = []
    for obj in _shot_object_refs(show, episode, shot):
        if obj not in paths:
            paths.append(obj)
    sid = shot.get("id", "")
    kf = show.dir / "runs" / f"EP{episode:02d}" / "storyboard" / f"{sid}.png"
    if kf.exists():
        paths.append(str(kf))
    return paths


def _shot_character_ref(show: Show, name: str) -> str | None:
    """First approved reference-image path for one character, or None."""
    for cid in show.list_characters():
        c = show.read_character(cid)
        if c.get("name") != name:
            continue
        rd = show.character_refs_dir(cid)
        rj = rd / "refs.json"
        if not rj.exists():
            continue
        try:
            prior = json.loads(rj.read_text(encoding="utf-8"))
        except Exception:
            prior = {}
        if prior.get("status") != "real":
            continue
        refs = prior.get("refs") or []
        if refs and (rd / refs[0]).exists():
            return str(rd / refs[0])
    return None


def _voice_sample_path(show: Show, name: str) -> Path | None:
    """Approved voice-sample path for a character name (assets/voice/<id>_voice.wav)."""
    for cid in show.list_characters():
        c = show.read_character(cid)
        if c.get("name") == name:
            vp = show.dir / "assets" / "voice" / f"{cid}_voice.wav"
            return vp if vp.exists() else None
    return None


def _character_appearance(show: Show, name: str) -> str:
    """appearance_canonical for a character name, or ''."""
    for cid in show.list_characters():
        c = show.read_character(cid)
        if c.get("name") == name:
            return (c.get("appearance_canonical") or "").strip()
    return ""


def compile_shot_prompt(show: Show | None, script: dict[str, Any],
                        scene: dict[str, Any], shot: dict[str, Any],
                        names: list[str],
                        audio_refs: list[dict[str, str]] | None = None) -> str:
    """Full-Reference Mode rewrite prompt, EXACTLY per MiniMax's
    VIDEO_PROMPT_WRITING_GUIDE_ref_en.md (six sections, in order):

        subject_definitions
        summary
        retention_analysis
        detailed_description
        overall_soundscape
        non_diegetic_music

    Rules followed verbatim from the guide:
    - ``<Subject N>`` is the reusable person/identity; ``<Picture N>`` is the
      raw reference frame it comes from. Define each subject as
      ``<Subject N> is <name>, shown in <Picture N>, <appearance>.``
    - ``<Audio N>`` bound to its speaker:
      ``<Audio 1> is the voice-timbre reference for <Subject 1> (S1).``
    - ``summary`` begins with a task-type prefix: ``[reference generation +
      audio reference]``.
    - ``retention_analysis``: ``<Subject N> (appears in [Shot 1]):
      fully_preserved - ...`` and ``<Audio N>: reference - its vocal timbre
      guides the dialogue delivery of <Subject N> without copying the original
      signal.`` (no (Sx) here).
    - ``detailed_description``: style opening, then ``[Shot 1] ...`` with
      speakers as ``<Subject N> (Sx)`` and dialogue ONLY inside
      ``<d>[English] exact words.</d>``.
    - ``overall_soundscape`` / ``non_diegetic_music`` last; N/A when absent.

    ``audio_refs`` carries {"char": name, "token": "<Audio N>"} per voice ref.
    """
    # --- subject_definitions ---
    subject_by_name: dict[str, str] = {}
    subject_lines: list[str] = []
    for i, name in enumerate(names):
        pic = f"<Picture {i + 1}>"
        subj = f"<Subject {i + 1}>"
        subject_by_name[name] = subj
        appearance = _character_appearance(show, name) if show else ""
        appearance = appearance[:1].lower() + appearance[1:] if appearance else ""
        appearance = appearance.rstrip(" .") if appearance else ""
        subject_lines.append(f"{subj} is {name}, shown in {pic}"
                             + (f", {appearance}" if appearance else "") + ".")
    for a in (audio_refs or []):
        char = a.get("char", "")
        subj = subject_by_name.get(char, f"<Subject {len(subject_by_name) + 1}>")
        sid = _speaker_id(char, shot)
        subject_lines.append(f"{a['token']} is the voice-timbre reference "
                             f"for {subj} {sid}.")

    # --- summary ---
    global_desc = (script.get("summary") or scene.get("summary") or "").strip()[:300]
    task = "[reference generation" + (" + audio reference" if audio_refs else "") + "]"
    summary_txt = f"{task} {global_desc or 'One cinematic shot.'}"

    # --- retention_analysis ---
    retention_lines: list[str] = []
    for name in names:
        subj = subject_by_name.get(name, f"<Subject {len(subject_by_name) + 1}>")
        retention_lines.append(
            f"{subj} (appears in [Shot 1]): fully_preserved - {name}'s identity, "
            f"clothing and appearance are retained exactly as shown.")
    for a in (audio_refs or []):
        char = a.get("char", "")
        subj = subject_by_name.get(char, "<Subject 1>")
        retention_lines.append(
            f"{a['token']}: reference - its vocal timbre guides the dialogue "
            f"delivery of {subj} without copying the original signal.")

    # --- detailed_description ---
    placed: list[str] = []
    for name in names:
        subj = subject_by_name.get(name, "")
        appearance = _character_appearance(show, name) if show else ""
        appearance = appearance[:1].lower() + appearance[1:] if appearance else ""
        appearance = appearance.rstrip(" .") if appearance else ""
        if appearance:
            placed.append(f"{subj} ({name}), {appearance}")
        else:
            placed.append(f"{subj} ({name})")
    placed_txt = ", ".join(placed)

    parts: list[str] = []
    action = (shot.get("action") or "").strip()
    cam = (shot.get("camera") or "").strip()
    if action:
        parts.append(action)
    if cam:
        parts.append(cam)
    for d in (shot.get("dialogue") or []):
        line = (d.get("line") or "").strip()
        if not line:
            continue
        char = d.get("char", "")
        subj = subject_by_name.get(char, "")
        sid = _speaker_id(char, shot)
        audio = next((a["token"] for a in (audio_refs or [])
                      if a.get("char") == char), "")
        if subj and audio:
            parts.append(f"{subj} {sid} says, using the voice timbre "
                         f"referenced from {audio}, <d>[English] {line}</d>")
        elif subj:
            parts.append(f"{subj} {sid} says, <d>[English] {line}</d>")
        else:
            parts.append(f"A voice says, <d>[English] {line}</d>")
    shot_txt = " ".join(p for p in parts if p)

    style = (script.get("style_guide") or
             "The target video uses a cinematic 2D-anime style.").strip()
    detailed = f"{style}\n[Shot 1] {placed_txt}. {shot_txt}".strip()

    # --- overall_soundscape / non_diegetic_music ---
    soundscape = (shot.get("soundscape") or "").strip()
    music = (shot.get("music") or "").strip()

    sections = [
        "subject_definitions:\n" + "\n".join(subject_lines),
        f"summary:\n{summary_txt}",
        "retention_analysis:\n" + "\n".join(retention_lines),
        f"detailed_description:\n{detailed}",
        f"overall_soundscape:\n{soundscape or 'N/A'}",
        f"non_diegetic_music:\n{music or 'N/A'}",
    ]
    return "\n\n".join(sections).strip()


def _speaker_id(char: str, shot: dict[str, Any]) -> str:
    """(Sx) per the shot's dialogue order — first line -> S1, etc."""
    seen = [d.get("char") for d in (shot.get("dialogue") or []) if d.get("line")]
    idx = seen.index(char) + 1 if char in seen else 1
    return f"(S{idx})"


def _render_shot(show: Show, episode: int, client: ComfyClient, cfg,
                 script: dict[str, Any], scene: dict[str, Any],
                 shot: dict[str, Any], seed: int,
                 timeout_s: float = 1800.0) -> Path:
    from .h3 import build_h3_ref2va_workflow, run_h3_shot
    from .storyboard import _shot_refs
    sid = shot.get("id", "shot")
    names, _ = _shot_refs(show, shot)

    # Official ref2va graph: refs are SOCKETS on MiniMaxH3ReferenceToVideo.
    # Images (character refs first, then objects/keyframe) become <Picture N>;
    # voice samples become <Audio N>. The prompt references those tags.
    # Character refs are the identity anchors — they must come first so their
    # <Picture N> numbers match `names`.
    image_filenames: list[str] = []
    for name in names:
        ref = _shot_character_ref(show, name)
        if not ref:
            continue
        try:
            image_filenames.append(client.upload_image(ref))
        except Exception:
            continue
    for p in _shot_ref_paths(show, episode, scene, shot):
        try:
            fname = client.upload_image(p)
            if fname not in image_filenames:
                image_filenames.append(fname)
        except Exception:
            pass

    # Native dialogue: each speaking character's raw voice sample becomes an
    # <Audio N> reference (the PROVEN example workflow uploads the raw wav as-is
    # — no conversion). Audio refs must accompany image refs. Max 3 clips.
    audio_filenames: list[str] = []
    audio_refs: list[dict[str, str]] = []
    if image_filenames:
        speakers = [d.get("char") for d in (shot.get("dialogue") or []) if d.get("line")]
        for name in dict.fromkeys(speakers):
            if not name or len(audio_filenames) >= 3:
                break
            vp = _voice_sample_path(show, name)
            if not vp or name not in names:
                continue
            try:
                audio_filenames.append(client.upload_audio(vp))
            except Exception:
                continue
            audio_refs.append({"char": name,
                               "token": f"<Audio {len(audio_refs) + 1}>"})

    prompt = compile_shot_prompt(show, script, scene, shot, names,
                                 audio_refs=audio_refs or None)
    if cfg.get("pipeline", "h3_rewrite_prompt", False):
        from .clients.lmstudio import LMStudioClient
        from .prompts import h3_rewrite_prompt
        llm = LMStudioClient(cfg.get("llm", "base_url"), timeout=600)
        try:
            prompt = h3_rewrite_prompt(llm, prompt, shot)
        except Exception:
            log.warning("H3 prompt rewriter failed; using deterministic prompt")

    # Sampling from config: 8 steps, res_multistep/simple at 864x480.
    # Measured: Spectrum + FirstBlockCache make 8-step renders 2.2x SLOWER
    # (FBC needs ~20 steps to pay off), so both are OFF by default.
    h3_cfg = cfg.get("comfy", "h3", {})
    width = int(h3_cfg.get("width", 864) or 864)
    height = int(h3_cfg.get("height", 480) or 480)
    wf = build_h3_ref2va_workflow(
        prompt, float(shot.get("duration_s", 10.125)), seed, cfg=cfg,
        ref_image_filenames=image_filenames or None,
        ref_audio_filenames=audio_filenames or None,
        width=width, height=height,
        steps=int(h3_cfg.get("steps", 8) or 8),
        sampler_name=h3_cfg.get("sampler") or "res_multistep",
        scheduler=h3_cfg.get("scheduler") or "simple",
        use_spectrum=bool(h3_cfg.get("spectrum", False)),
        use_first_block_cache=bool(h3_cfg.get("first_block_cache", False)),
    )
    out = show.dir / "runs" / f"EP{episode:02d}" / "video" / f"{sid}.mp4"
    try:
        return run_h3_shot(client, wf, out, timeout_s=timeout_s)
    finally:
        # Release the H3 checkpoint + cached models from VRAM after every shot,
        # so the renderer never leaves the GPU occupied between shots / episodes.
        client.free_memory()


def _free_gpu(client: ComfyClient) -> None:
    """Best-effort VRAM release; never raise on cleanup."""
    try:
        client.free_memory()
    except Exception:
        log.debug("gpu free failed", exc_info=True)


def render_episode(show: Show, episode: int, cfg=None, progress=None,
                   timeout_s: float = 1800.0) -> int:
    """Render every shot of the latest episode script to video. Returns shot count.

    The whole episode runs under the GPU manager's exclusive COMFYUI ownership
    (§15.4): acquire() ensures the H3 instance is up, and release() evicts it
    (or, with ``comfy.manage_lifecycle``, shuts it down) so the GPU is never
    left occupied after a render.
    """
    from .gpu_manager import ServiceType, get_gpu_manager

    cfg = cfg or get_config()
    script = _latest_script(show, episode)
    if not script:
        return 0
    client = _render_client(cfg)
    shots = [s for sc in script.get("scenes", []) for s in sc.get("shots", [])]
    done = 0
    with get_gpu_manager(cfg).acquire(ServiceType.COMFYUI):
        try:
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
        finally:
            # Always release the GPU after an episode, success or failure.
            _free_gpu(client)
    return done


def build_render(show: Show, episode: int, cfg=None) -> None:
    """Render all shots in a background thread with progress.

    Gated on the same ref-approval set as the storyboard previews: a final
    render must not use a costume variant or recurring-object ref the human
    hasn't approved. Pending refs -> the job parks in "waiting" instead of
    burning GPU on an unapproved identity.
    """
    from .storyboard import pending_ref_approvals
    pending = pending_ref_approvals(show, episode)
    if pending:
        RENDER_JOBS[show.show_id] = {
            "state": "waiting", "done": 0, "total": 0,
            "detail": "Waiting for ref approval: " + ", ".join(pending),
        }
        return

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
