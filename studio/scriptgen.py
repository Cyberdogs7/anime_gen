"""Shared script utilities: runtime review and script normalization.

Single-path note: scripts are built ONLY by the prose-first assembly in
planner.assemble_episode_script (prose -> story review -> shots by need).
These helpers (duration snapping, dialogue hygiene, runtime gating) serve that
path; there is no second script writer.
"""
from __future__ import annotations

import re
from typing import Any

from .compile.durations import snap_duration


def _has_spoken_text(line: str) -> bool:
    """True when a dialogue line contains actual speech (not just a stage direction).

    A padded line like "(Grunting)" or "(Internal/Unvoiced)" carries no spoken
    words and must not count toward the dialogue floor — it would reach TTS
    otherwise. A leading delivery note like "(whispering) Stay close." is fine.
    """
    stripped = re.sub(r"\([^)]*\)", "", line or "")
    return bool(re.search(r"\w", stripped))


def _runtime_review(script: dict[str, Any], target: int) -> dict[str, Any] | None:
    """Fail the script if its shot durations total well under the runtime target,
    or if dialogue is severely lacking for the planned length."""
    total = sum(s.get("duration_s", 0) for sc in script.get("scenes", [])
                for s in sc.get("shots", []))
    lines = sum(1 for sc in script.get("scenes", [])
                for s in sc.get("shots", [])
                for d in (s.get("dialogue", []) or [])
                if _has_spoken_text(d.get("line") or ""))
    min_lines = max(1, target // 6)   # ~67% spoken (~220 lines for a 22-min episode)
    if total >= target * 0.8 and lines >= min_lines:
        return None
    notes: list[str] = []
    if total < target * 0.8:
        notes.append(f"The script totals ~{int(total)}s but the target is {target}s "
                      f"(~{target // 60} min). Expand the episode: add roughly "
                      f"{int((target - total) / 10) + 1} more shots across additional scenes "
                      f"and beats so it fills the runtime.")
    if lines < min_lines:
        notes.append(f"The script has only {lines} dialogue lines but needs at least "
                      f"{min_lines} for a {target // 60}-minute episode. Characters must "
                      f"speak in most shots — dialogue drives the story, not silent action. "
                      f"Add lines to shots that have characters on screen.")
    return {"pass": False, "score": 0,
            "summary": f"structural failure (runtime {int(total)}s, {lines} lines)",
            "notes": notes}


def _normalize(script: dict[str, Any], cast_names: list[str]) -> dict[str, Any]:
    """Snap durations to the H3 grid, default fields, drop unknown speakers.

    ``cast_names`` are the characters with approved sheets. A speaker is kept if
    they are either in ``cast_names`` OR listed in the script's own ``cast``
    pre-section (a newly-introduced supporting character that has no sheet yet
    but is canon for this episode — the ref pass will create it).
    """
    script_cast = set(script.get("cast", []) or [])
    valid = set(cast_names) | script_cast

    for scene in script.get("scenes", []):
        for i, shot in enumerate(scene.get("shots", [])):
            shot.setdefault("id", f"{scene.get('id', 's')}_sh{i + 1:02d}")
            shot.setdefault("type", "ref2va")
            shot.setdefault("importance", "standard")
            shot.setdefault("on_camera", True)
            shot["duration_s"] = snap_duration(float(shot.get("duration_s", 10.125)))[2]
            # Drop unknown speakers AND padded lines that carry no spoken words
            # ("(Grunting)") — they must not reach TTS, and they must not count
            # toward the runtime review's dialogue floor.
            kept = [d for d in shot.get("dialogue", [])
                    if d.get("char") in valid
                    and _has_spoken_text(d.get("line") or "")]
            if kept != shot.get("dialogue", []):
                shot["dialogue"] = kept
            # Speech state must be explicit in the data as well as the prompt:
            # `silence` is true iff the shot has no REAL spoken line. Derived from
            # the actual dialogue array so a stale flag can never contradict the
            # prompt the renderer compiles (PROMPTING.md §5.4).
            shot["silence"] = not any(
                _has_spoken_text(d.get("line") or "")
                for d in (shot.get("dialogue") or []))
            shot.setdefault("soundscape", "")
            shot.setdefault("music", "")
            shot.setdefault("references", {})
            if "characters" in shot["references"]:
                shot["references"]["characters"] = [
                    c for c in shot["references"]["characters"] if c in valid]
    # Guarantee the cast pre-section and a top-level summary.
    if not isinstance(script.get("cast"), list):
        seen: list[str] = []
        for scene in script.get("scenes", []):
            for shot in scene.get("shots", []):
                for c in (shot.get("references") or {}).get("characters", []) or []:
                    if c and c not in seen:
                        seen.append(c)
                for d in shot.get("dialogue", []) or []:
                    if d.get("char") and d["char"] not in seen:
                        seen.append(d["char"])
        script["cast"] = seen
    if not (script.get("summary") or "").strip():
        script["summary"] = " ".join(
            (sc.get("summary") or "").strip() for sc in script.get("scenes", [])
            if (sc.get("summary") or "").strip())
    return script
