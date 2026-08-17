"""Character-consistency reviewer -> prompt revision -> regeneration loop.

The vision-capable LM Studio model (the same gemma that writes scripts) checks
each storyboard keyframe against the character's APPROVED reference image. If a
shot's character has drifted, the reviewer issues a corrective prompt instruction
and the shot is regenerated through the same krea2 + IPAdapter path. Runs
automatically after each storyboard batch (max_rounds until stable).
"""
from __future__ import annotations

import base64
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .bootstrap import ACTIVITY
from .config import get_config
from .show import Show
from .storyboard import (_keyframe_prompt, _latest_script, _shot_characters,
                         _shot_object_refs, _shot_ref_weights, _shot_refs)
REVIEW_PROMPT = (
    "You are a STRICT character-consistency reviewer for an anime production.\n"
    "You receive a storyboard keyframe and a set of EXPECTED characters, each with an "
    "APPROVED reference image (labeled by character name).\n\n"
    "Examine the keyframe and:\n"
    "1. For EACH expected character, is that character present in the keyframe and "
    "visually consistent with their reference (hair color, hair style/length, eye "
    "color, outfit, build)? A character is INCONSISTENT if any of these clearly "
    "differ (e.g. reference has black hair but the keyframe character has long green "
    "hair).\n"
    "2. Does the keyframe contain ANY character that matches NONE of the expected "
    "references — an unknown/unref'd character (for example a girl with pink hair "
    "when no reference has pink hair, or a girl with short white hair when no "
    "reference matches)? If so, list it under unknown_characters.\n"
    "3. TEXT DEFECT: does the keyframe contain any rendered text, dialogue "
    "bubbles/balloons, subtitles, captions, words, letters, or watermarks drawn "
    "into the image? Any legible text in the frame is a defect.\n\n"
    "pass is true ONLY if every expected character is present and consistent, there "
    "are no unknown characters, AND the image contains no rendered text or "
    "watermarks. Do not excuse differences with lighting or angle unless they are "
    "small.\n"
    "Reply with ONLY a JSON object:\n"
    '{"characters": [{"name": "...", "present": true/false, "consistent": true/false, '
    '"issue": "what differs for this character"}], '
    '"unknown_characters": ["short visual description of any character that matches '
    'no reference"], "text_defect": true/false, "text_details": "what text is visible", '
    '"pass": true/false, "prompt_fix": "a concise, specific prompt instruction that '
    'would correct the inconsistencies AND explicitly forbid any text, dialogue '
    'bubbles, captions, or watermarks"}'
)


def _downscale(path: Path, max_dim: int = 512) -> Path:
    """Downscale for the vision model (ffmpeg); falls back to the original.

    A video path (the 1s H3 preview) is first decoded to its frame 0, then
    downscaled — so the consistency reviewer sees exactly what the video starts
    with.
    """
    try:
        out = Path(tempfile.gettempdir()) / f"cons_{path.stem}.jpg"
        if path.suffix.lower() == ".mp4":
            subprocess.run([get_config().ffmpeg_bin(), "-y", "-i", str(path),
                            "-frames:v", "1", str(out)],
                           capture_output=True, timeout=60)
        else:
            subprocess.run([get_config().ffmpeg_bin(), "-y", "-i", str(path),
                            "-vf", f"scale='min({max_dim},iw)':-2", str(out)],
                           capture_output=True, timeout=30)
        if out.exists():
            return out
    except Exception:
        pass
    return path


def _data_uri(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


def _extract_json(text: str) -> dict[str, Any] | None:
    try:
        s = text[text.find("{"): text.rfind("}") + 1]
        return json.loads(s) if s else None
    except Exception:
        return None


def _prev_keyframe(runs, scene: dict[str, Any], sid: str) -> Path | None:
    """The storyboard preview (mp4) or keyframe (png) of the shot just before
    `sid` within the scene."""
    shots = scene.get("shots", []) or []
    for i, sh in enumerate(shots):
        if sh.get("id") == sid:
            if i == 0:
                return None
            prev_id = shots[i - 1].get("id")
            if not prev_id:
                return None
            for ext in (".mp4", ".png"):
                p = runs / "storyboard" / f"{prev_id}{ext}"
                if p.exists():
                    return p
            return None
    return None


def review_shot(llm, model: str, names: list[str], ref_paths: list[Path],
                keyframe_path: Path) -> dict[str, Any]:
    """One vision call: are all expected characters present, consistent, and are
    there any unref'd characters in the keyframe?"""
    kf = _downscale(keyframe_path)
    expected = "; ".join(names) or "no expected characters"
    content = [{"type": "text", "text": REVIEW_PROMPT + "\n\nExpected characters: " + expected}]
    for i, ref in enumerate(ref_paths[:4]):
        content.append({"type": "text",
                        "text": f"Ref {i + 1}: {names[i]}" if i < len(names) else ""})
        content.append({"type": "image_url", "image_url": {"url": _data_uri(_downscale(ref))}})
    content.append({"type": "image_url", "image_url": {"url": _data_uri(kf)}})
    text = llm.chat([{"role": "user", "content": content}], model=model,
                    temperature=0.2, max_tokens=3000)
    return _extract_json(text) or {"pass": True, "issue": "unparseable",
                                   "prompt_fix": ""}


def revise_keyframe_prompt(llm, model: str, prompt: str, issues: str, fix: str) -> str:
    """Send the current prompt + reviewer corrections to the LLM, which REWRITES
    the whole keyframe prompt. Appending fixes doesn't work; a coherent rewrite does.
    """
    revision = (
        "You are an anime storyboard prompt engineer. Rewrite the keyframe prompt below "
        "to fully incorporate the reviewer's corrections.\n"
        "KEEP the scene's action, camera, setting and mood intact. CHANGE the character "
        "design details the reviewer flagged so they match the approved character "
        "references (hair color, hair style, eye color, outfit/costume). Name EVERY "
        "character who is on screen. If a character must be present, name them explicitly.\n"
        "The image must contain NO text, no dialogue bubbles, no captions, no subtitles, "
        "no watermark.\n"
        "Reply with ONLY JSON: {\"prompt\": \"the fully rewritten keyframe prompt\"}\n\n"
        f"CURRENT PROMPT:\n{prompt}\n\n"
        f"REVIEWER CORRECTIONS:\n{issues}\n{fix}"
    )
    text = llm.chat([{"role": "user", "content": revision}], model=model,
                    temperature=0.6, max_tokens=1500)
    try:
        s = text[text.find("{"): text.rfind("}") + 1]
        out = (json.loads(s).get("prompt") or "").strip()
        return out if len(out) > 40 else prompt
    except Exception:
        return prompt


def run_consistency_check(show: Show, episode: int, cfg=None, llm=None,
                          max_rounds: int = 4,
                          on_progress: "Callable[[], None] | None" = None) -> list[dict[str, Any]]:
    """Review shot keyframes and AUTO-iterate: regenerate failures, re-review only
    those, until everything passes or the round cap is hit. No manual step.

    Returns a per-shot report: [{"shot", "char", "result": pass|regenerated|...,
    "issue", "fix", "round"}].

    ``on_progress`` (optional) is called after every shot review so a caller's
    job progress stamp stays fresh during the (potentially long) vision review —
    the episode reconciler uses it to tell 'working' from 'hung'.
    """
    from .clients.lmstudio import LMStudioClient
    from .comfy_workflows import generate_keyframe_with_ref, load_workflow
    from .remote.ops import ServiceOps
    from .storyboard import _shot_refs

    cfg = cfg or get_config()
    script, _ = _latest_script(show, episode)
    if not script:
        return []
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    wf_path = cfg.workflows_dir / "image_keyframe.json"
    runs = show.dir / "runs" / f"EP{episode:02d}"
    report: list[dict[str, Any]] = []

    # Round 0 reviews every shot that has refs; later rounds only the failures.
    pending: list[tuple[dict, dict]] = [
        (sc, shot) for sc in script.get("scenes", []) for shot in sc.get("shots", [])
        if _shot_refs(show, shot)[1]]
    round_no = 0
    while pending and round_no < max_rounds:
        round_no += 1
        failures: list[dict[str, Any]] = []
        for sc, shot in pending:
            sid = shot.get("id", "")
            kf = runs / "storyboard" / f"{sid}.mp4"
            if not kf.exists():
                kf = runs / "storyboard" / f"{sid}.png"
            if not kf.exists():
                continue
            names, refs = _shot_refs(show, shot)
            ACTIVITY[show.show_id] = {"detail": f"Consistency review {sid} ({', '.join(names)})…",
                                      "ts": __import__("time").time()}
            if on_progress:
                on_progress()
            verdict = review_shot(llm, model, names, [Path(p) for p in refs], kf)
            if not verdict.get("pass"):
                issue_parts = []
                if verdict.get("issue"):
                    issue_parts.append(verdict["issue"])
                issue_parts += [c.get("issue", "") for c in verdict.get("characters", [])
                                if not c.get("consistent", True)]
                issue_parts += list(verdict.get("unknown_characters", []) or [])
                if verdict.get("text_defect"):
                    issue_parts.append(f"TEXT in frame: {verdict.get('text_details', '')}")
                issue = "; ".join(p for p in issue_parts if p) or "inconsistent"
                fix = (verdict.get("prompt_fix") or "").strip()
                failures.append({"sid": sid, "char": ", ".join(names), "scene": sc,
                                 "shot": shot, "refs": refs, "issue": issue, "fix": fix})
                report.append({"shot": sid, "char": ", ".join(names), "round": round_no,
                               "result": "failed", "issue": issue, "fix": fix})
            else:
                report.append({"shot": sid, "char": ", ".join(names), "round": round_no,
                               "result": "pass"})
        if not failures:
            break

        # Regenerate all failures as fresh 1s H3 previews (same workflow as the
        # storyboard pass), then re-review ONLY those shots next round.
        from .render import (_render_client, _shot_character_ref, _voice_sample_path,
                             compile_shot_prompt, _shot_ref_paths)
        from .h3 import build_h3_ref2va_workflow, run_h3_shot
        client = _render_client(cfg)
        try:
            for f in failures:
                sid = f["sid"]
                ACTIVITY[show.show_id] = {"detail": f"Rewriting prompt for {sid} + regenerate…",
                                          "ts": __import__("time").time()}
                enames, _ = _shot_refs(show, f["shot"])
                base = _keyframe_prompt(f["shot"], f["scene"], enames,
                                        _char_appearances(show))
                base = revise_keyframe_prompt(llm, model, base,
                                              f.get("issue", ""), f.get("fix", ""))
                out = runs / "storyboard" / f"{sid}.mp4"
                image_filenames: list[str] = []
                for name in enames:
                    ref = _shot_character_ref(show, name)
                    if not ref:
                        continue
                    try:
                        image_filenames.append(client.upload_image(ref))
                    except Exception:
                        continue
                for p in _shot_ref_paths(show, episode, f["scene"], f["shot"]):
                    try:
                        fname = client.upload_image(p)
                        if fname not in image_filenames:
                            image_filenames.append(fname)
                    except Exception:
                        pass
                h3_cfg = cfg.get("comfy", "h3", {})
                wf = build_h3_ref2va_workflow(
                    base, 1.0, 0, cfg=cfg,
                    ref_image_filenames=image_filenames or None,
                    width=int(h3_cfg.get("width", 864) or 864),
                    height=int(h3_cfg.get("height", 480) or 480),
                    steps=int(h3_cfg.get("steps", 8) or 8),
                    sampler_name=h3_cfg.get("sampler") or "res_multistep",
                    scheduler=h3_cfg.get("scheduler") or "simple",
                    use_spectrum=bool(h3_cfg.get("spectrum", False)),
                    use_first_block_cache=bool(h3_cfg.get("first_block_cache", False)),
                )
                for nid, node in wf.items():
                    if node.get("class_type") == "MiniMaxH3ReferenceToVideo" and \
                       isinstance(node.get("inputs"), dict):
                        node["inputs"]["length"] = 22
                try:
                    run_h3_shot(client, wf, out)
                    for entry in report:
                        if entry.get("shot") == sid:
                            entry["result"] = "regenerated"
                except Exception as exc:
                    for entry in report:
                        if entry.get("shot") == sid:
                            entry["result"] = f"regen failed: {exc}"
                finally:
                    try:
                        client.free_memory()
                    except Exception:
                        pass
        finally:
            try:
                client.free_memory()
            except Exception:
                pass
        pending = [(f["scene"], f["shot"]) for f in failures]

    try:
        (runs / "consistency.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return report
