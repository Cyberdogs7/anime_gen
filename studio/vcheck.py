"""Post-render visual check: VLM verdicts on the rendered clips (vcheck).

STT QC (studio.qc) verifies the AUDIO; this verifies the VIDEO. A vision-
language model (Qwen3-VL-8B via LM Studio) inspects frames sampled from each
rendered clip and answers two questions per shot:

  1. CHARACTERS: are the expected characters on screen and consistent with
     their APPROVED reference images (identity, hair, outfit, build)? Any
     character that matches no reference is flagged.
  2. ACTION: does the shot's scripted action/camera actually play out in the
     frames (the right characters doing the right thing, in the right place)?

Design notes:

- A VLM consumes IMAGES, not video: we sample `vcheck.frames` evenly-spaced
  frames (start, quartiles, end) with ffmpeg and send them as a multi-image
  prompt, plus the character refs the shot is supposed to depict.
- The VLM runs on the GPU (LM Studio on the primary host). It must therefore
  only run when H3 is NOT rendering: `schedule: post_render` hooks it into the
  render worker AFTER the episode render finishes and the GPU is released. It
  never runs inline with a render.
- Failures AUTO-FEED the shot gate: a failed clip is rejected (via
  approval.reject_shot) with the VL issues as the rejection notes, so the
  existing prompt-revision + retake loop fixes it. Capped per shot
  (vcheck.max_auto_retakes) so a stubborn clip doesn't loop forever - beyond
  the cap the clip stays flagged for a human to decide.
- Reports persist to runs/EP##/vcheck/summary.json + the vcheck_results DB
  table (what the dashboard shows), mirroring the STT QC module.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from .config import get_config
from .show import Show

VCHECK_PROMPT = (
    "You are a STRICT post-render video reviewer for an anime production.\n"
    "You receive reference images of the CHARACTERS a shot is supposed to "
    "depict (each labeled by name), a description of the shot's SCRIPTED ACTION "
    "and camera, and sequential FRAMES sampled from the rendered video.\n\n"
    "Examine the frames and judge TWO things:\n\n"
    "1. CHARACTERS: For EACH reference character, is that character present in "
    "the video and visually consistent with their reference (hair color, hair "
    "style/length, eye color, outfit, build)? A character is INCONSISTENT if any "
    "of these clearly differs. Also note any character in the video that matches "
    "NONE of the references (unknown/unref'd character). If a frame shows a "
    "character with the wrong number of arms, fingers, or any morphing/body "
    "distortion, flag that character as inconsistent.\n"
    "2. ACTION: Does the scripted action, camera movement, setting, and any "
    "key objects/characters actually appear in the frames? If the script says a "
    "character speaks or fights or gestures, is that visible? If the script "
    "calls for a specific setting (city, lab, rooftop) or object, is it there?\n"
    "Also flag any rendered text/subtitles/captions/watermarks in the frames "
    "as a text defect.\n"
    "pass is true ONLY if every expected character is present and consistent, "
    "there are no unknown characters, no body morphing, no text defects, AND the "
    "scripted action plays out in the frames.\n"
    "Reply with ONLY a JSON object:\n"
    '{"characters": [{"name": "...", "present": true/false, "consistent": true/false, '
    '"issue": "what differs for this character"}], '
    '"unknown_characters": ["short visual description..."], '
    '"action_match": true/false, "action_issue": "what of the scripted action is '
    'missing or wrong", "text_defect": true/false, "text_details": "what text is '
    'visible", "pass": true/false, "issues": ["each concrete problem, one string"], '
    '"prompt_fix": "a concise, specific prompt instruction that would correct the '
    'problems AND explicitly forbid any text, dialogue bubbles, captions, or '
    'watermarks"}'
)

# Frames are sampled as fractions of the clip duration. When n <= 5 the legacy
# fixed points (start, quartiles, end) are used; for more frames they are spread
# evenly across the whole timeline instead so the VLM sees more of the motion.
_FRAME_FRACTIONS = (0.0, 0.25, 0.5, 0.75, 0.99)


def _frame_fractions(n: int) -> tuple[float, ...]:
    """Fractional sample points for ``n`` frames.

    Keeps the 5 legacy points when n == 5 (stable verdicts / tests); for other n
    spreads frames evenly across [0, 1) so temporal coverage grows with n.
    """
    n = max(1, int(n))
    if n == len(_FRAME_FRACTIONS):
        return _FRAME_FRACTIONS
    if n == 1:
        return (0.5,)
    return tuple(i / n for i in range(n))


def _extract_json(text: str) -> dict[str, Any] | None:
    try:
        s = text[text.find("{"): text.rfind("}") + 1]
        return json.loads(s) if s else None
    except Exception:
        return None


def _data_uri(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


def _downscale(path: Path, max_dim: int = 640) -> Path:
    """Downscale an image for the VLM (ffmpeg); falls back to the original."""
    try:
        out = Path(tempfile.gettempdir()) / f"vck_{path.stem}_{int(time.time())}.jpg"
        subprocess.run([get_config().ffmpeg_bin(), "-y", "-i", str(path),
                        "-vf", f"scale='min({max_dim},iw)':-2", str(out)],
                       capture_output=True, timeout=60)
        if out.exists() and out.stat().st_size > 0:
            return out
    except Exception:
        pass
    return path


def _clip_duration(clip: Path, cfg) -> float | None:
    """Probe the clip duration (seconds); None on failure.

    Tries ffprobe (if a binary is configured/found), then falls back to parsing
    ffmpeg's own ``-i`` output. Only the runtimes the studio bundles are used.
    """
    import shutil
    ffprobe = (cfg.get("env", "ffprobe", "")
               or shutil.which("ffprobe") or "")
    if ffprobe:
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(clip)],
                capture_output=True, timeout=30)
            if proc.returncode == 0:
                text = (proc.stdout or b"").decode().strip()
                if text:
                    return float(text)
        except Exception:
            pass
    try:
        proc = subprocess.run([cfg.ffmpeg_bin(), "-i", str(clip)],
                              capture_output=True, timeout=30)
        text = (proc.stderr or b"").decode("utf-8", "replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return None


def sample_frames(clip: Path, cfg=None, n: int | None = None,
                  out_dim: int | None = None) -> list[Path]:
    """Extract a temporal sample of frames from a rendered clip as JPEGs.

    Frames are placed by duration fraction via _frame_fractions (the 5 legacy
    points for n == 5, otherwise spread evenly across the clip). Each is
    downscaled to ``out_dim`` on its short side so the vision model can afford
    to see more of the motion without blowing its context window. Returns []
    when the clip can't be probed or decoded, so the check degrades to a
    ``fail`` rather than crashing the pipeline.
    """
    cfg = cfg or get_config()
    n = n or int(cfg.get("qc", "vcheck", {}).get("frames", 5) or 5)
    if out_dim is None:
        out_dim = int(cfg.get("qc", "vcheck", {}).get("frame_sample_dim", 512) or 512)
    duration = _clip_duration(clip, cfg)
    if not duration or duration <= 0:
        log.warning("[vcheck] cannot probe duration of %s", clip)
        return []
    fractions = _frame_fractions(n)
    frames: list[Path] = []
    tmpdir = Path(tempfile.mkdtemp(prefix=f"vcheck_{clip.stem}_"))
    ffmpeg = cfg.ffmpeg_bin()
    for i, frac in enumerate(fractions):
        when = min(duration * frac, max(0.0, duration - 0.05))
        out = tmpdir / f"frame_{i}.jpg"
        try:
            proc = subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-ss", f"{when:.2f}",
                 "-i", str(clip), "-frames:v", "1",
                 "-vf", f"scale='min({out_dim},iw)':-2", str(out)],
                capture_output=True, timeout=60)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            log.warning("[vcheck] ffmpeg frame extract failed for %s", clip,
                        exc_info=True)
            break
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            frames.append(out)
        else:
            log.warning("[vcheck] frame %d from %s returned %d", i, clip,
                        proc.returncode)
    return frames


def review_video(llm, model: str, names: list[str], ref_paths: list[Path],
                 frames: list[Path], action: str,
                 max_refs: int = 4) -> dict[str, Any]:
    """One VLM call: do the expected characters appear (consistent with their
    refs) and does the scripted action play out in the sampled frames?"""
    if not frames:
        return {"pass": False, "issues": ["no frames could be sampled from clip"],
                "characters": [], "unknown_characters": [],
                "action_match": False, "action_issue": "no frames to inspect"}
    expected = "; ".join(names) or "no expected characters"
    content = [{"type": "text", "text": (
        VCHECK_PROMPT + "\n\nExpected characters: " + expected
        + "\nScripted action/camera: " + (action or "unspecified"))}]
    for i, ref in enumerate(ref_paths[:max_refs]):
        name = names[i] if i < len(names) else f"ref_{i + 1}"
        content.append({"type": "text", "text": f"Character ref {i + 1}: {name}"})
        content.append({"type": "image_url", "image_url": {"url": _data_uri(_downscale(ref))}})
    for i, f in enumerate(frames):
        content.append({"type": "text", "text": f"Video frame {i + 1} (chronological)"})
        content.append({"type": "image_url", "image_url": {"url": _data_uri(f)}})
    return _call_and_parse(llm, model, content, max_refs=max_refs)


def review_video_native(llm, model: str, names: list[str], ref_paths: list[Path],
                        clip: Path, action: str,
                        max_refs: int = 4) -> dict[str, Any]:
    """One VLM call with the NATIVE clip (vLLM video input support).

    The rendered mp4 is sent whole (base64 data URI) instead of sampled frames,
    so the VLM sees true temporal continuity: character identity across motion,
    the scripted action actually playing out, and any morphing/text defects in
    real motion. Sends character refs as images alongside.
    """
    expected = "; ".join(names) or "no expected characters"
    content = [{"type": "text", "text": (
        VCHECK_PROMPT + "\n\nExpected characters: " + expected
        + "\nScripted action/camera: " + (action or "unspecified"))}]
    for i, ref in enumerate(ref_paths[:max_refs]):
        name = names[i] if i < len(names) else f"ref_{i + 1}"
        content.append({"type": "text", "text": f"Character ref {i + 1}: {name}"})
        content.append({"type": "image_url", "image_url": {"url": _data_uri(_downscale(ref))}})
    data = base64.b64encode(clip.read_bytes()).decode()
    content.append({"type": "text", "text": "Rendered clip (mp4, chronological)"})
    content.append({"type": "video_url",
                    "video_url": {"url": "data:video/mp4;base64," + data}})
    return _call_and_parse(llm, model, content, max_refs=max_refs)


def _call_and_parse(llm, model: str, content: list[dict], max_refs: int) -> dict[str, Any]:
    text = llm.chat([{"role": "user", "content": content}], model=model,
                    temperature=0.2, max_tokens=3500, retries=2, role="describer")
    data = _extract_json(text) or {}
    issues = list(data.get("issues") or [])
    chars = data.get("characters") or []
    for c in chars:
        if not c.get("present", True):
            issues.append(f"{c.get('name', 'character')} is absent")
        elif not c.get("consistent", True):
            issues.append(f"{c.get('name', 'character')} inconsistent: "
                          f"{c.get('issue', '')}".strip())
    for u in data.get("unknown_characters") or []:
        issues.append(f"unknown character: {u}")
    if not data.get("action_match", True):
        issues.append(f"scripted action missing: {data.get('action_issue', '')}".strip())
    if data.get("text_defect"):
        issues.append(f"text in frame: {data.get('text_details', '')}".strip())
    if not issues:
        data["pass"] = True
    return {
        "pass": bool(data.get("pass")),
        "issues": issues,
        "characters": chars,
        "unknown_characters": data.get("unknown_characters") or [],
        "action_match": bool(data.get("action_match")),
        "action_issue": data.get("action_issue", ""),
        "text_defect": bool(data.get("text_defect")),
        "prompt_fix": data.get("prompt_fix", ""),
    }


def _vcheck_dir(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / "vcheck"


def _vcheck_summary(show: Show, episode: int) -> dict[str, Any]:
    p = _vcheck_dir(show, episode) / "summary.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    return {}


def _save_summary(show: Show, episode: int, entry: dict[str, Any]) -> None:
    d = _vcheck_dir(show, episode)
    d.mkdir(parents=True, exist_ok=True)
    summary = _vcheck_summary(show, episode)
    summary[entry["shot_id"]] = entry
    (d / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


def _persist_db(show: Show, sid: str, report: dict[str, Any]) -> None:
    try:
        from .db import open_db
        db = open_db()
        db.conn.execute(
            "INSERT OR REPLACE INTO vcheck_results(id, shot_id, checks_json, "
            "verdict, passed, created_at) VALUES(?,?,?,?,?,datetime('now'))",
            (f"{show.show_id}:{sid}", sid,
             json.dumps({k: v for k, v in report.items()
                         if k not in ("shot_id", "ts")}),
             report.get("verdict", "fail"),
             1 if report.get("verdict") == "pass" else 0))
        db.conn.commit()
        db.close()
    except Exception:
        log.debug("[vcheck] db persist failed", exc_info=True)


def _make_vcheck_client(cfg) -> Any:
    """The chat client for the configured vcheck server.

    ``server: vllm`` -> an OpenAI-compatible client pointed at the WSL vLLM
    endpoint (native video input via ``video_url``). ``server: lmstudio`` -> the
    standard LM Studio client (frame-sampling fallback path).
    """
    vck = cfg.get("qc", "vcheck", {})
    if vck.get("server") == "vllm":
        from .clients.lmstudio import LMStudioClient
        vllm = vck.get("vllm", {}) or {}
        return LMStudioClient(vllm.get("url") or "http://127.0.0.1:8001/v1",
                              timeout=float(vck.get("timeout_s", 300)))
    from .clients.lmstudio import LMStudioClient
    return LMStudioClient(cfg.get("llm", "base_url"),
                          timeout=float(vck.get("timeout_s", 300)))


def _vcheck_model(cfg) -> str:
    vck = cfg.get("qc", "vcheck", {})
    if vck.get("server") == "vllm":
        return (vck.get("vllm", {}) or {}).get("model") or vck.get("model") \
            or "Qwen3-VL-8B-Instruct-FP8"
    return vck.get("model") or (cfg.get("llm", "roles", {}).get("describer")
                                or cfg.get("llm", "model"))


def run_shot_vcheck(show: Show, episode: int, clip_path: Path, shot: dict[str, Any],
                    cfg=None, llm=None) -> dict[str, Any]:
    """Run the VLM check on ONE rendered clip and persist the report.

    NATIVE VIDEO (vcheck.server == vllm): the whole clip is streamed to
    Qwen3-VL over vLLM (true temporal/motion understanding). FALLBACK
    (lmstudio): frames are sampled from the clip and sent as images. Both paths
    also send the shot's character refs + the scripted action. Verdict is
    pass/fail/skip. Never raises: a VLM failure records a ``fail`` report.
    Returns the report dict.
    """
    from .storyboard import _shot_refs
    cfg = cfg or get_config()
    vck = cfg.get("qc", "vcheck", {})
    sid = shot.get("id", "shot")
    report = {
        "shot_id": sid, "verdict": "skip", "ts": time.time(), "issues": [],
        "characters": [], "unknown_characters": [], "action_match": False,
        "text_defect": False, "model": _vcheck_model(cfg),
    }
    if not vck.get("enabled", True):
        return report
    names, refs = _shot_refs(show, shot)
    action = " ".join(filter(None, [
        shot.get("action", ""), shot.get("camera", "")]))
    llm = llm or _make_vcheck_client(cfg)
    model = _vcheck_model(cfg)
    try:
        if vck.get("server") == "vllm":
            if not clip_path.exists():
                report["issues"] = ["clip is missing"]
                report["verdict"] = "fail"
            else:
                verdict = review_video_native(llm, model, names,
                                              [Path(p) for p in refs],
                                              clip_path, action,
                                              max_refs=int(vck.get("max_refs", 4)))
                report.update({
                    "verdict": "pass" if verdict["pass"] else "fail",
                    "issues": verdict["issues"],
                    "characters": verdict["characters"],
                    "unknown_characters": verdict["unknown_characters"],
                    "action_match": verdict["action_match"],
                    "action_issue": verdict["action_issue"],
                    "text_defect": verdict["text_defect"],
                    "prompt_fix": verdict["prompt_fix"],
                })
        else:
            frames = sample_frames(clip_path, cfg)
            if not frames:
                report["issues"] = ["could not sample frames from clip"]
                report["verdict"] = "fail"
            else:
                try:
                    verdict = review_video(llm, model, names,
                                           [Path(p) for p in refs],
                                           frames, action,
                                           max_refs=int(vck.get("max_refs", 4)))
                    report.update({
                        "verdict": "pass" if verdict["pass"] else "fail",
                        "issues": verdict["issues"],
                        "characters": verdict["characters"],
                        "unknown_characters": verdict["unknown_characters"],
                        "action_match": verdict["action_match"],
                        "action_issue": verdict["action_issue"],
                        "text_defect": verdict["text_defect"],
                        "prompt_fix": verdict["prompt_fix"],
                    })
                finally:
                    import shutil
                    shutil.rmtree(frames[0].parent, ignore_errors=True) if frames else None
    except Exception as exc:
        report["issues"] = [f"VLM check failed: {exc}"]
        report["verdict"] = "fail"
        log.warning("[vcheck] shot check failed for %s: %s", sid, exc,
                    exc_info=True)
    _save_summary(show, episode, report)
    _persist_db(show, sid, report)
    return report


# ---------------------------------------------------------------------------
# Object VL check: detect humans / people in recurring-object reference images
# ---------------------------------------------------------------------------

OBJECT_VCHECK_PROMPT = (
    "You are a strict visual QA checker for anime reference images.\n"
    "You receive a reference image of a PROPS / OBJECT used in an anime "
    "production. The image MUST depict ONLY the object itself, isolated on a "
    "plain or studio background.\n\n"
    "Examine the image and answer ONE question:\n"
    "Does this image contain ANY humans, people, characters, body parts "
    "(hands, fingers, feet, faces, silhouettes, shadows of people), or any "
    "figure carrying / holding / touching the object?\n\n"
    "Also flag:\n"
    "- Text, watermarks, captions, subtitles, or logos.\n"
    "- The image is blurry, heavily distorted, or unrecognizable.\n"
    "- The object is not clearly the main subject (e.g. a scene with scenery "
    "instead of an isolated reference).\n\n"
    "pass is true ONLY if the image shows the object ALONE with no people, "
    "no text, and is clear and recognizable.\n\n"
    "Reply with ONLY a JSON object:\n"
    '{"pass": true/false, "issues": ["each concrete problem, one string"], '
    '"has_people": true/false, "has_text": true/false, '
    '"clarity": "good"/"poor"/"unrecognizable", '
    '"prompt_fix": "a concise prompt instruction that would correct the '
    'problems AND explicitly forbid any people, hands, faces, or text"}'
)


def _object_vcheck_dir(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / "objects" / "vcheck"


def _object_vcheck_summary(show: Show, episode: int) -> dict[str, Any]:
    p = _object_vcheck_dir(show, episode) / "summary.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    return {}


def _save_object_vcheck(show: Show, episode: int, entry: dict[str, Any]) -> None:
    d = _object_vcheck_dir(show, episode)
    d.mkdir(parents=True, exist_ok=True)
    summary = _object_vcheck_summary(show, episode)
    summary[entry["slug"]] = entry
    (d / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


def run_object_vcheck(show: Show, episode: int, image_path: Path, slug: str,
                      cfg=None, llm=None) -> dict[str, Any]:
    """Run the VLM check on ONE object reference image.

    Sends the image to the vision model and asks whether it contains humans,
    text, or other defects. Verdict is pass/fail. Never raises: a VLM failure
    records a ``fail`` report. Returns the report dict.
    """
    cfg = cfg or get_config()
    vck = cfg.get("qc", "vcheck", {})
    report = {
        "slug": slug, "verdict": "skip", "ts": time.time(), "issues": [],
        "has_people": False, "has_text": False, "clarity": "good",
        "model": _vcheck_model(cfg),
    }
    if not vck.get("enabled", True):
        return report
    if not image_path.exists():
        report["issues"] = ["image file missing"]
        report["verdict"] = "fail"
        _save_object_vcheck(show, episode, report)
        return report
    llm = llm or _make_vcheck_client(cfg)
    model = _vcheck_model(cfg)
    try:
        content = [{"type": "text", "text": OBJECT_VCHECK_PROMPT},
                   {"type": "image_url",
                    "image_url": {"url": _data_uri(_downscale(image_path))}}]
        text = llm.chat([{"role": "user", "content": content}], model=model,
                        temperature=0.2, max_tokens=1500, retries=2, role="describer")
        data = _extract_json(text) or {}
        issues = list(data.get("issues") or [])
        has_people = bool(data.get("has_people", False))
        has_text = bool(data.get("has_text", False))
        clarity = data.get("clarity", "good")
        if has_people:
            issues.append("image contains people / humans / body parts")
        if has_text:
            issues.append("image contains text / watermarks / logos")
        if clarity in ("poor", "unrecognizable"):
            issues.append(f"image clarity: {clarity}")
        if not issues:
            data["pass"] = True
        report.update({
            "verdict": "pass" if data.get("pass") else "fail",
            "issues": issues,
            "has_people": has_people,
            "has_text": has_text,
            "clarity": clarity,
            "prompt_fix": data.get("prompt_fix", ""),
        })
    except Exception as exc:
        report["issues"] = [f"VLM check failed: {exc}"]
        report["verdict"] = "fail"
        log.warning("[vcheck] object check failed for %s: %s", slug, exc,
                    exc_info=True)
    _save_object_vcheck(show, episode, report)
    return report


def clear_object_vcheck(show: Show, episode: int, slug: str) -> None:
    """Drop an object's stored vcheck report (used when an object is regenerated)."""
    try:
        summary = _object_vcheck_summary(show, episode)
        if slug in summary:
            summary.pop(slug)
            d = _object_vcheck_dir(show, episode)
            d.mkdir(parents=True, exist_ok=True)
            (d / "summary.json").write_text(
                json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.debug("[vcheck] clear object summary failed for %s", slug, exc_info=True)


def run_object_vcheck_batch(show: Show, episode: int, cfg=None, llm=None,
                            on_progress=None) -> list[str]:
    """Run the VLM check on every object image that lacks a report.

    Returns a log of actions. Objects that fail (humans detected) are
    auto-rejected with the VL issues as rejection notes.
    """
    from .storyboard import _objects_dir, _approved_object_slugs, regenerate_object_ref
    from .approval import unapprove_object
    cfg = cfg or get_config()
    vck = cfg.get("qc", "vcheck", {})
    if not vck.get("enabled", True):
        return []
    od = _objects_dir(show, episode)
    if not od.exists():
        return []
    stored = _object_vcheck_summary(show, episode)
    objects = sorted(od.glob("*.png"))
    approved = _approved_object_slugs(show, episode)
    actions: list[str] = []
    client = llm or _make_vcheck_client(cfg)
    for i, obj_path in enumerate(objects):
        slug = obj_path.stem
        if slug in stored:
            continue  # already reviewed
        if on_progress:
            on_progress(i + 1, len(objects), slug)
        report = run_object_vcheck(show, episode, obj_path, slug, cfg=cfg,
                                   llm=client)
        if report.get("verdict") != "fail":
            continue
        # Auto-reject objects that contain humans or are unrecognizable.
        if report.get("has_people") or report.get("clarity") == "unrecognizable":
            notes = "Object VL check: " + " ".join(report["issues"])
            if slug in approved:
                unapprove_object(show, episode, slug)
            try:
                prompt_fix = report.get("prompt_fix", "")
                regenerate_object_ref(show, episode, slug,
                                      notes=prompt_fix or notes, cfg=cfg, llm=client)
                clear_object_vcheck(show, episode, slug)  # re-checked after regen
                actions.append(f"{slug}: VL FAIL (humans detected) — auto-regenerated")
            except Exception as exc:
                actions.append(f"{slug}: VL FAIL but regeneration failed: {exc}")
        else:
            actions.append(f"{slug}: VL FAIL — {report['issues']}")
    if on_progress:
        on_progress(len(objects), len(objects), "done")
    return actions


def clear_shot_vcheck(show: Show, episode: int, sid: str) -> None:
    """Drop a shot's stored vcheck report (used when a clip is re-rendered)."""
    try:
        summary = _vcheck_summary(show, episode)
        if sid in summary:
            summary.pop(sid)
            (_vcheck_dir(show, episode) / "summary.json").write_text(
                json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.debug("[vcheck] clear summary failed for %s", sid, exc_info=True)
    try:
        from .db import open_db
        db = open_db()
        db.conn.execute("DELETE FROM vcheck_results WHERE shot_id = ?", (sid,))
        db.conn.commit()
        db.close()
    except Exception:
        log.debug("[vcheck] db clear failed for %s", sid, exc_info=True)


def episode_vcheck_summary(show: Show, episode: int) -> dict[str, Any]:
    """Roll-up counts over every shot's stored vcheck report."""
    summary = _vcheck_summary(show, episode)
    counts = {"pass": 0, "fail": 0, "skip": 0, "total": 0}
    for entry in summary.values():
        v = entry.get("verdict", "fail")
        counts["total"] += 1
        if v in counts:
            counts[v] += 1
        else:
            counts["fail"] += 1
    return {"counts": counts, "shots": summary}


def run_episode_vcheck(show: Show, episode: int, cfg=None, llm=None,
                       on_progress=None, auto_feed: bool | None = None,
                       max_auto_retakes: int | None = None) -> list[str]:
    """Post-render visual check over every rendered clip lacking a report.

    With ``vcheck.server == vllm`` the whole REVIEW pass runs under the GPU
    manager's VLLM lease (the WSL vLLM server is started only here, in the
    post-render window, and stopped when the pass ends — never alongside H3).
    Each clip is streamed to Qwen3-VL natively. Otherwise the LM Studio server
    is used with frame sampling.

    FAILS are collected during the review pass and auto-fed into the shot gate
    AFTER the lease exits: reject_shot's prompt revision talks to the LM Studio
    showrunner (acquire(LLM)), which must not run while vLLM owns VRAM.

    Skips shots that already have a stored verdict (pass or skip is never
    re-litigated; a ``fail`` that survived the retake cap is also kept so a
    stubborn clip waits for a human instead of looping). For each clip WITHOUT
    a verdict: run the VLM check, then auto-feed the shot gate - a fail becomes
    a rejection (prompt revised from the VL issues + retake) up to
    ``max_auto_retakes`` per shot.

    Returns a log of actions for the caller (the render worker or CLI).
    """
    from .render import _latest_script
    from .approval import reject_shot, shot_approvals
    cfg = cfg or get_config()
    vck = cfg.get("qc", "vcheck", {})
    if not vck.get("enabled", True):
        return []
    if auto_feed is None:
        auto_feed = vck.get("auto_feed", True)
    cap = int(max_auto_retakes if max_auto_retakes is not None
              else vck.get("max_auto_retakes", 2))

    script = _latest_script(show, episode)
    if not script:
        return []
    shots = {s.get("id"): s for sc in script.get("scenes", [])
             for s in sc.get("shots", [])}
    vd = show.dir / "runs" / f"EP{episode:02d}" / "video"
    stored = _vcheck_summary(show, episode)
    clips = sorted(vd.glob("*.mp4"))
    approvals = shot_approvals(show, episode)
    actions: list[str] = []
    pending_fails: list[tuple[str, dict]] = []

    def _review_all(client) -> None:
        for i, clip in enumerate(clips):
            sid = clip.stem
            if on_progress:
                on_progress(i + 1, len(clips), sid)
            if sid in stored:
                continue          # already reviewed (pass/skip, or capped fail)
            shot = shots.get(sid)
            if not shot:
                continue
            report = run_shot_vcheck(show, episode, clip, shot, cfg=cfg,
                                     llm=client)
            if report.get("verdict") != "fail":
                continue
            pending_fails.append((sid, report))

    use_vllm = vck.get("server") == "vllm"
    client = llm or _make_vcheck_client(cfg)
    from .gpu_manager import ServiceType, get_gpu_manager
    manager = get_gpu_manager(cfg)
    # Each serving path holds the GPU under a lease so the model is loaded
    # before the pass and unloaded (VRAM released) afterwards:
    #   - server == vllm      -> the WSL vLLM service (native video)
    #   - server == lmstudio  -> the LM Studio VLM (frame sampling): acquire() runs
    #                           `lms load qwen/qwen3-vl-8b` on enter and unloads
    #                           on exit, so the model never has to be resident by hand.
    # A caller-injected client (tests) supplies its own context and must not
    # trigger a real model load / WSL boot.
    if llm is None and use_vllm:
        with manager.acquire(ServiceType.VLLM):
            _review_all(client)
    elif llm is None and not use_vllm:
        with manager.acquire(ServiceType.VLM):
            _review_all(client)
    else:
        _review_all(client)

    # Auto-feed the shot gate AFTER the lease exits (reject_shot needs the
    # showrunner LLM, which must not stack onto vLLM's VRAM).
    for sid, report in pending_fails:
        if not auto_feed:
            actions.append(f"{sid}: vcheck FAIL (auto-feed off) — {report['issues']}")
            continue
        ap = approvals.get(sid, {})
        if ap.get("status") == "approved":
            actions.append(f"{sid}: vcheck FAIL but shot is approved — leaving it")
            continue
        retakes = int(ap.get("retake_count", 0) or 0)
        if retakes >= cap:
            actions.append(f"{sid}: vcheck FAIL ({len(report['issues'])} issues) "
                           f"— auto-retake cap hit ({cap}); awaiting human decision")
            continue
        notes = "Post-render visual check: " + " ".join(report["issues"])
        # Rejections revise the prompt via the showrunner LLM. In the vllm path
        # use the default client (LLM lease, taken after the VLLM lease exits);
        # otherwise reuse the injected client so tests never touch the network.
        msgs = reject_shot(show, episode, sid, notes=notes,
                           llm=None if use_vllm else llm)
        clear_shot_vcheck(show, episode, sid)   # re-checked after the retake
        approvals[sid] = shot_approvals(show, episode).get(sid, approvals.get(sid, {}))
        actions.extend(msgs)

    if on_progress:
        on_progress(len(clips), len(clips), "done")
    return actions