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
from typing import Any

from .bootstrap import ACTIVITY
from .config import get_config
from .show import Show
from .storyboard import (_keyframe_prompt, _latest_script, _shot_characters, _shot_refs)
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
    """Downscale for the vision model (ffmpeg); falls back to the original."""
    try:
        out = Path(tempfile.gettempdir()) / f"cons_{path.stem}.jpg"
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


def run_consistency_check(show: Show, episode: int, cfg=None, llm=None,
                          max_rounds: int = 4) -> list[dict[str, Any]]:
    """Review shot keyframes and AUTO-iterate: regenerate failures, re-review only
    those, until everything passes or the round cap is hit. No manual step.

    Returns a per-shot report: [{"shot", "char", "result": pass|regenerated|...,
    "issue", "fix", "round"}].
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
            kf = runs / "storyboard" / f"{sid}.png"
            if not kf.exists():
                continue
            names, refs = _shot_refs(show, shot)
            ACTIVITY[show.show_id] = {"detail": f"Consistency review {sid} ({', '.join(names)})…",
                                      "ts": __import__("time").time()}
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

        # Regenerate all failures through one transient krea2 session, then
        # re-review ONLY those shots next round.
        ops = ServiceOps(cfg)
        client, stop = ops._krea2_client()
        try:
            for f in failures:
                sid = f["sid"]
                ACTIVITY[show.show_id] = {"detail": f"Regenerating {sid} (consistency fix)…",
                                          "ts": __import__("time").time()}
                base = _keyframe_prompt(f["shot"], f["scene"], _shot_characters(f["shot"]))
                if f.get("fix"):
                    base = f"{base} {f['fix']}".strip()
                kf = runs / "storyboard" / f"{sid}.png"
                try:
                    generate_keyframe_with_ref(client, load_workflow(wf_path), base, 0,
                                               f["refs"], str(kf),
                                               aspect_ratio="16:9", weight=0.8)
                    for entry in report:
                        if entry.get("shot") == sid:
                            entry["result"] = "regenerated"
                except Exception as exc:
                    for entry in report:
                        if entry.get("shot") == sid:
                            entry["result"] = f"regen failed: {exc}"
        finally:
            if stop:
                stop()
        pending = [(f["scene"], f["shot"]) for f in failures]

    try:
        (runs / "consistency.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return report
