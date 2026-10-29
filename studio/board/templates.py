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
    # Single-responsibility text passes: each card owns exactly one concern
    # and writes per-scene fragments under runs/EPxx/passes/. The h3prompts
    # card merges the fragments into the standard script_scenes/ + script.rN
    # shape, so keyframes/render/QC/assemble are untouched by the split.
    # Agent-reviewed cards auto-accept on pass; the four human-gated stages
    # (outline, dialogue, camera, h3prompts) carry human_gate and wait for a
    # person after the agent reviewer passes.
    pass_cards: list[dict[str, Any]] = []
    passes = [
        ("locations", "locations", "Set EP%02d locations", "location_writer",
         "location_reviewer", False),
        ("cast", "cast", "Cast EP%02d scenes", "cast_writer",
         "cast_reviewer", False),
        ("outfits", "outfits", "Dress EP%02d cast", "outfit_writer",
         "outfit_reviewer", False),
        ("objects", "objects", "Plan EP%02d object references", "storyboard_planner",
         "object_reviewer", False),
        ("narrative", "narrative", "Write EP%02d scene narratives", "narrative_writer",
         "narrative_reviewer", False),
        ("dialogue", "dialogue", "Write EP%02d dialogue", "dialogue_writer",
         "dialogue_reviewer", True),
        ("shots", "shots", "Split EP%02d shots", "shot_splitter",
         "shot_reviewer", False),
        ("camera", "camera", "Direct EP%02d cameras", "camera_director",
         "camera_reviewer", True),
        ("action", "action", "Choreograph EP%02d action", "action_writer",
         "action_reviewer", False),
        ("sound", "sound", "Design EP%02d sound", "sound_writer",
         "sound_reviewer", False),
        ("h3prompts", "h3prompts", "Compile EP%02d H3 prompts", "h3_writer",
         "h3_reviewer", True),
    ]
    prev = f"{ep}.outline"
    for suffix, kind, title, role, reviewer, gated in passes:
        payload: dict[str, Any] = {}
        if gated:
            payload["human_gate"] = True
        pass_cards.append(
            new_card(
                f"{ep}.{suffix}", kind=kind, title=title % episode,
                show_id=show_id, episode=episode, role=role,
                reviewer=reviewer, lane="llm", priority=4,
                depends_on=[prev], payload=payload,
            )
        )
        prev = f"{ep}.{suffix}"
    return [
        # One agent card owns the whole outline: the skill writes a compact
        # changes file, a deterministic helper applies it, and the reviewer
        # card gates it. No Python skeleton/beats LLM calls.
        new_card(
            f"{ep}.outline", kind="outline", title=f"Write the EP{episode:02d} outline",
            show_id=show_id, episode=episode, role="outline_writer_agent",
            reviewer="outline_reviewer", lane="llm", priority=5,
            payload={"human_gate": True},
        ),
        *pass_cards,
        new_card(
            f"{ep}.keyframes", kind="keyframes", title=f"Draw EP{episode:02d} keyframes",
            show_id=show_id, episode=episode, role="image_exec",
            lane="image", priority=3, depends_on=[f"{ep}.h3prompts"],
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
