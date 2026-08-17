"""Deterministic MiniMax H3 storyboard-prompt compiler.

Turns a shot list + subject definitions into the exact MiniMax-notation prompt
the H3 Director compiles (DESIGN.md §10.4). No LLM in the loop - what the log
shows is what the model receives. Dialogue/silence discipline mirrors the
ref2va path (PROMPTING.md §5.7): every shot states its speech state — real
spoken lines as `<d>` tokens, or an explicit silence clause.
"""
from __future__ import annotations

import re

from typing import Any


def _fmt_ts(seconds: float) -> str:
    """MM:SS.mmm (strictly increasing per H3 guide)."""
    ms = round(seconds * 1000)
    mm, rem = divmod(ms, 60000)
    ss, mmm = divmod(rem, 1000)
    return f"{mm:02d}:{ss:02d}.{mmm:03d}"


def _has_speech(line: str) -> bool:
    """True when a dialogue entry actually speaks words (not a stage direction).

    A parenthetical like ``(Grunting)`` carries no spoken text and must never
    become a `<d>` token — H3 would synthesize it as gibberish speech.
    """
    body = re.sub(r"\([^)]*\)", "", line or "")
    return bool(re.search(r"\w", body))


def _dialogue_fragment(dialogue: Any) -> str:
    """Render one dialogue entry as an H3 native speech token.

    String form (legacy/narration) wraps in <d> so it is still spoken; list
    entries may carry a subject attribution so the model knows WHO speaks.
    Entries that are only stage directions (e.g. ``(Grunting)``) are dropped —
    they are not speech.
    """
    if isinstance(dialogue, list):
        out: list[str] = []
        for entry in dialogue:
            line = (entry.get("line") or "").strip()
            if not _has_speech(line):
                continue
            subj = (entry.get("subject") or "").strip()
            frag = f"<d>[English] {line}</d>"
            out.append(f"{subj} speaks, {frag}" if subj else frag)
        return ", ".join(out)
    line = str(dialogue or "").strip()
    return f"<d>[English] {line}</d>" if _has_speech(line) else ""


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
    - ``dialogue`` may be a string (treated as narration) OR a list of
      {"subject": "<Subject N>" or "", "line": "..."} dicts. In list form each
      line is emitted as a native speech token ``<d>[English] line</d>`` so H3
      synthesizes the spoken line (with lip sync) instead of reading it as
      narration.
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
        dialogue = _dialogue_fragment(shot.get("dialogue", ""))
        if dialogue:
            parts.append(f", {dialogue}")
        else:
            # Speech state must be explicit: no <d> tokens and no silence clause
            # makes H3 guess the audio track and hallucinate gibberish speech.
            parts.append(", no one speaks; all characters remain silent")
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
