"""Deterministic MiniMax H3 storyboard-prompt compiler.

Turns a shot list + subject definitions into the exact MiniMax-notation prompt
the H3 Director compiles (DESIGN.md §10.4). No LLM in the loop - what the log
shows is what the model receives.
"""
from __future__ import annotations

from typing import Any


def _fmt_ts(seconds: float) -> str:
    """MM:SS.mmm (strictly increasing per H3 guide)."""
    ms = round(seconds * 1000)
    mm, rem = divmod(ms, 60000)
    ss, mmm = divmod(rem, 1000)
    return f"{mm:02d}:{ss:02d}.{mmm:03d}"


def compile_h3_prompt(
    global_description: str,
    shots: list[dict[str, Any]],
    subject_definitions: list[str] | None = None,
    soundscape: str = "",
    music: str = "",
    start_time_s: float = 0.0,
) -> str:
    """Build the H3 prompt from a list of shot dicts.

    Each shot: {"id", "action", "duration_s", "camera": "", "dialogue": "", "subjects": []}
    - The first shot carries no timestamp; every later cut carries a strictly
      increasing MM:SS.mmm timestamp computed from cumulative durations.
    - ``subjects`` is a list of tokens already in `<Subject N>` form that the
      shot should reference.
    """
    lines: list[str] = []

    if subject_definitions:
        lines.append("subject_definitions: " + " ".join(subject_definitions))
        lines.append("")

    retention_subjects = " and ".join(dict.fromkeys(
        tok for s in shots for tok in (s.get("subjects") or [])
    ))
    if retention_subjects:
        lines.append(
            "retention_analysis: Keep the identity, face and clothing of "
            f"{retention_subjects} consistent across every shot."
        )
        lines.append("")

    lines.append(f"detailed_description: {global_description.strip() or 'Live-action, cinematic.'}")

    cursor = start_time_s
    for i, shot in enumerate(shots):
        dur = float(shot.get("duration_s", 5.0))
        parts = []
        if i == 0:
            parts.append(f"[Shot {i + 1}] {shot.get('action', '').strip()}")
        else:
            parts.append(f"[Shot {i + 1}] At {_fmt_ts(cursor)}, {shot.get('action', '').strip()}")
        cam = shot.get("camera", "").strip()
        if cam:
            parts.append(f", {cam}")
        subjects = shot.get("subjects") or []
        if subjects:
            parts.append(", featuring " + ", ".join(subjects))
        dialogue = shot.get("dialogue", "").strip()
        if dialogue:
            parts.append(f', "{dialogue}"')
        lines.append("".join(parts))
        cursor += dur

    if soundscape.strip():
        lines.append(f"overall_soundscape: {soundscape.strip()}")
    if music.strip():
        lines.append(f"non_diegetic_music: {music.strip()}")

    return "\n".join(lines)


def example() -> str:
    """A worked example for tests / documentation."""
    return compile_h3_prompt(
        global_description="Live-action, cinematic.",
        subject_definitions=["<Subject 1> is the character shown in <Picture 1>."],
        shots=[
            {"id": "s01", "action": "the courier lands on the rooftop",
             "duration_s": 5.167, "camera": "wide establishing, slow push-in",
             "subjects": ["<Subject 1>"]},
            {"id": "s02", "action": "Kiyo holds out the package",
             "duration_s": 5.167, "camera": "medium shot", "subjects": ["<Subject 1>"]},
        ],
        soundscape="wind, distant sirens",
        music="bass drone",
    )
