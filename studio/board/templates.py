"""Card templates for an episode. Cards are deterministic and idempotent."""
from __future__ import annotations

from typing import Any

from .cards import new_card


def episode_id(episode: int) -> str:
    return f"ep{int(episode):02d}"


def show_card_id(show_id: str, *parts: str) -> str:
    """A card id scoped to one show; cards live in per-show stores."""
    return ".".join([str(show_id), *parts])


def episode_cards(show_id: str, episode: int) -> list[dict[str, Any]]:
    """The top-level cards for one episode.

    Render is a milestone that expands into one deterministic ComfyUI card per
    shot. QC is deterministic VLM. Both are separate cards so no card ever holds
    two GPU services at once.
    """
    ep = show_card_id(show_id, episode_id(episode))
    return [
        # One agent card owns the whole outline: the skill writes a compact
        # changes file, a deterministic helper applies it, and the reviewer
        # card gates it. No Python skeleton/beats LLM calls.
        new_card(
            f"{ep}.outline", kind="outline", title=f"Write the EP{episode:02d} outline",
            show_id=show_id, episode=episode, role="outline_writer_agent",
            reviewer="outline_reviewer", lane="llm", priority=5,
        ),
        new_card(
            f"{ep}.script", kind="script", title=f"Write the EP{episode:02d} screenplay",
            show_id=show_id, episode=episode, role="script_writer",
            reviewer="script_reviewer", lane="llm", priority=4,
            depends_on=[f"{ep}.outline"],
        ),
        new_card(
            f"{ep}.objects", kind="objects", title=f"Plan EP{episode:02d} object references",
            show_id=show_id, episode=episode, role="storyboard_planner",
            lane="llm", priority=4, depends_on=[f"{ep}.script"],
        ),
        new_card(
            f"{ep}.keyframes", kind="keyframes", title=f"Draw EP{episode:02d} keyframes",
            show_id=show_id, episode=episode, role="image_exec",
            lane="image", priority=3, depends_on=[f"{ep}.objects"],
            payload={"tool": "build_keyframes", "args": {}},
        ),
        new_card(
            f"{ep}.render", kind="render", title=f"Render every EP{episode:02d} shot",
            show_id=show_id, episode=episode, role="render_exec",
            lane="video", priority=3, depends_on=[f"{ep}.keyframes"],
        ),
        new_card(
            f"{ep}.qc", kind="qc", title=f"Review EP{episode:02d} renders",
            show_id=show_id, episode=episode, role="qc_exec",
            lane="vision", priority=2, depends_on=[f"{ep}.render"],
            max_attempts=4, payload={"tool": "run_episode_qc", "args": {}},
        ),
        new_card(
            f"{ep}.assemble", kind="assemble", title=f"Assemble EP{episode:02d}",
            show_id=show_id, episode=episode, role="assembler_exec",
            lane="io", priority=2, depends_on=[f"{ep}.qc"],
            payload={"tool": "assemble_episode", "args": {}},
        ),
    ]


def shot_cards(show_id: str, episode: int, parent: str,
               shot_ids: list[str]) -> list[dict[str, Any]]:
    """One deterministic render card per shot, owned by the render milestone."""
    ep = show_card_id(show_id, episode_id(episode))
    return [
        new_card(
            f"{ep}.shot.{sid}.render", kind="render_shot",
            title=f"Render EP{episode:02d} shot {sid}",
            show_id=show_id, episode=episode, role="render_exec",
            lane="video", priority=3, max_attempts=3,
            payload={"tool": "render_shot", "args": {"shot_id": sid},
                     "shot_id": sid, "parent": parent},
        )
        for sid in shot_ids
    ]
