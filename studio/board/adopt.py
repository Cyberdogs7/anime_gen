"""Rebuild a board from a show folder discovered on disk.

Adoption is file-state driven: it never calls an LLM or ComfyUI. Completed
stages become ``accepted`` cards, stages with partial artifacts are ``blocked``
so they are not silently redone, and missing stages stay ``planned``. A marker
in ``shows/<id>/board/adopted.json`` makes this a one-time import, so deleting a
card later does not resurrect it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..planner import read_episode_plan, read_scene_details
from ..show import Show
from ..storyboard import _latest_script
from .cards import now
from .gate0 import bootstrap_cards
from .templates import episode_cards

MARKER_KEY = "adopted"
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".mp4")


def is_adopted(store) -> bool:
    return store.meta_get(MARKER_KEY) is not None


def mark_adopted(store, count: int, *, existing: bool = False) -> None:
    store.meta_set(MARKER_KEY, json.dumps({
        "adopted_at": now(),
        "cards": int(count),
        "existing": bool(existing),
    }))


def _note(card: dict[str, Any], text: str) -> None:
    card.setdefault("notes", []).append(
        {"by": "import", "ts": now(), "text": text})


def _shot_ids(script: dict[str, Any] | None) -> list[str]:
    ids: list[str] = []
    for scene in (script or {}).get("scenes") or []:
        for shot in scene.get("shots") or []:
            sid = str(shot.get("id") or "")
            if sid:
                ids.append(sid)
    return ids


def _has_shot_file(directory: Path, sid: str) -> bool:
    return any((directory / f"{sid}{ext}").exists() for ext in _IMAGE_EXTS)


def _episodes(show: Show) -> list[int]:
    runs = show.dir / "runs"
    if not runs.exists():
        return []
    eps: list[int] = []
    for path in runs.iterdir():
        if not path.is_dir() or not path.name.upper().startswith("EP"):
            continue
        try:
            eps.append(int(path.name[2:]))
        except ValueError:
            continue
    return sorted(set(eps))


def _gate0_cards(show: Show) -> list[dict[str, Any]]:
    st = show.bootstrap_state() or {}
    chars = [c for c in st.get("characters") or [] if isinstance(c, dict)]
    concept = str((st.get("concept") or {}).get("status") or "")
    bible = str((st.get("bible") or {}).get("status") or "")
    scenes = str((st.get("scenes") or {}).get("status") or "")
    complete = bool(st.get("complete"))

    def all_status(key: str) -> bool:
        return all(str(c.get(key) or "") == "approved" for c in chars)

    def any_status(key: str, value: str) -> bool:
        return any(str(c.get(key) or "") == value for c in chars)

    try:
        pitches = (show.read_ideas() or {}).get("pitches") or []
    except Exception:
        pitches = []
    ideas_selected = any(str(p.get("status")) == "selected" for p in pitches)
    ideas_any = bool(pitches)
    bible_done = complete or bible == "approved"
    bible_pending = bible == "pending"
    chars_done = complete or (chars and all_status("proposal"))
    chars_pending = any_status("proposal", "pending")
    scenes_done = complete or scenes == "approved"
    scenes_pending = scenes == "pending"
    text_done = complete or (concept == "approved" and bible_done
                             and scenes_done and chars_done)
    text_pending = (concept == "pending" or bible_pending or scenes_pending
                    or chars_pending)
    images_done = complete or (text_done and all_status("refs"))
    images_pending = any_status("refs", "pending")
    voices_done = complete or (text_done and all_status("voice"))
    voices_pending = any_status("voice", "pending")

    states = {
        "ideas": ("accepted" if (complete or concept == "approved"
                                 or ideas_selected)
                  else "reviewing" if ideas_any else "planned"),
        "bible": ("accepted" if bible_done
                  else "reviewing" if bible_pending else "planned"),
        "characters": ("accepted" if chars_done
                       else "reviewing" if chars_pending else "planned"),
        "scenes": ("accepted" if scenes_done
                   else "reviewing" if scenes_pending else "planned"),
        # Legacy single-shot plan card (pre-partition shows).
        "plan": ("accepted" if text_done
                 else "reviewing" if text_pending else "planned"),
        "images": ("accepted" if images_done
                   else "reviewing" if images_pending else "planned"),
        "voices": ("accepted" if voices_done
                   else "reviewing" if voices_pending else "planned"),
    }
    cards = bootstrap_cards(show.show_id)
    for card in cards:
        domain = str(card.get("id") or "").rsplit(".", 1)[-1]
        state = states.get(domain, "planned")
        card["state"] = state
        if state == "reviewing":
            _note(card, "restored from folder: waiting for human review")
    return cards


def _approved(data: dict[str, Any] | None) -> bool:
    return str((data or {}).get("status") or "") == "approved"


def _episode_cards(show: Show, episode: int) -> list[dict[str, Any]]:
    plan = read_episode_plan(show, episode)
    details = read_scene_details(show, episode)
    script, _ = _latest_script(show, episode)
    script = script or {}
    review = script.get("_review") or {}
    shot_ids = _shot_ids(script)
    ep_dir = show.dir / "runs" / f"EP{episode:02d}"
    storyboard = ep_dir / "storyboard"
    video = ep_dir / "video"
    plan_done = _approved(plan) and _approved(details)
    plan_any = bool(plan or details)
    keyframes_done = bool(shot_ids) and all(_has_shot_file(storyboard, sid)
                                            for sid in shot_ids)
    keyframes_any = any(_has_shot_file(storyboard, sid) for sid in shot_ids)
    render_done = bool(shot_ids) and all(_has_shot_file(video, sid)
                                         for sid in shot_ids)
    render_any = any(_has_shot_file(video, sid) for sid in shot_ids)

    cards = episode_cards(show.show_id, episode)
    for card in cards:
        kind = str(card.get("kind") or "")
        state = "planned"
        note = ""
        if kind == "outline":
            if plan_done:
                state = "accepted"
            elif plan_any:
                state, note = "blocked", "outline exists but is not approved"
        elif kind == "script":
            if script:
                if review.get("passed") is True:
                    state = "accepted"
                elif review.get("passed") is False:
                    state, note = "blocked", "script review failed"
                else:
                    state = "agent_verified"
        elif kind == "objects":
            objects = plan.get("objects") or []
            if plan_done and (not objects
                              or (ep_dir / "object-prompts.json").exists()
                              or any((ep_dir / "objects").glob("*"))):
                state = "accepted"
        elif kind == "keyframes":
            if keyframes_done:
                state = "accepted"
            elif keyframes_any:
                state, note = "blocked", "partial keyframes on disk"
        elif kind == "render":
            if render_done:
                state = "accepted"
            elif render_any:
                state, note = "blocked", "partial render on disk"
        elif kind == "qc":
            if (ep_dir / "qc" / "summary.json").exists() \
                    or (ep_dir / "vcheck" / "summary.json").exists():
                state = "accepted"
        elif kind == "assemble":
            if (ep_dir / "episode.mp4").exists():
                state = "accepted"
        card["state"] = state
        if note:
            _note(card, f"restored from folder: {note}")
    return cards


def discover_cards(show: Show) -> list[dict[str, Any]]:
    cards = _gate0_cards(show)
    for episode in _episodes(show):
        cards.extend(_episode_cards(show, episode))
    return cards


def adopt_show(show: Show, store) -> list[dict[str, Any]]:
    """Populate an empty per-show board from the folder, once."""
    if is_adopted(store):
        return []
    existing = store.all()
    if existing:
        mark_adopted(store, len(existing), existing=True)
        return []
    cards = discover_cards(show)
    created = [store.upsert(card) for card in cards]
    mark_adopted(store, len(created))
    return created
