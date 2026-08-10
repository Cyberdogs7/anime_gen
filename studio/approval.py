"""Human approval actions shared by the CLI and the dashboard (DESIGN §9.5).

Every gate is an artifact review with an approve/reject action. State lives in
the show's files (bootstrap.json for Gate 0, runs/EP##/story.approval.json for
Gate 3); approval/rejection also emits the matching event on the approval bus
stream so the pipeline sees the decision exactly as if a consumer posted it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .bootstrap import ACTIVITY, BootstrapChain
from .bus.events import (
    BIBLE_APPROVED, BIBLE_PENDING, BIBLE_REJECTED, CHAR_PROPOSAL_APPROVED,
    CHAR_PROPOSAL_PENDING, CHAR_PROPOSAL_REJECTED, CONCEPT_APPROVED, CONCEPT_PENDING,
    CONCEPT_REJECTED, SCENE_REGISTRY_APPROVED, SCENE_REGISTRY_PENDING,
    SCENE_REGISTRY_REJECTED, SCRIPT_APPROVED, SCRIPT_REJECTED, VOICE_APPROVED,
    VOICE_REJECTED, new_event,
)
from .show import Show

GATE_STEPS = ("concept", "bible", "character", "refs", "voice", "scenes")


def _emit(show: Show, event_type: str, payload: dict[str, Any] | None = None) -> None:
    chain = BootstrapChain(show)
    chain._emit(new_event(event_type, show_id=show.show_id, payload=payload or {}))


def _find_char(st: dict, name: str) -> dict | None:
    return next((c for c in st.get("characters", []) if c.get("name") == name), None)


def _char_by_name(show: Show, name: str) -> dict | None:
    for cid in show.list_characters():
        try:
            c = show.read_character(cid)
        except Exception:
            continue
        if c.get("name") == name:
            return c
    return None


def _clear_refs(show: Show, char_id: str) -> None:
    """Delete the reference files so the next generation produces a fresh image."""
    rd = show.character_refs_dir(char_id)
    if rd.exists():
        for f in rd.iterdir():
            try:
                f.unlink()
            except Exception:
                pass


def _clear_voice(show: Show, char_id: str) -> None:
    vp = show.dir / "assets" / "voice" / f"{char_id}_voice.wav"
    if vp.exists():
        try:
            vp.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Gate 0 — bootstrap steps
# ---------------------------------------------------------------------------

def approve_step(show_id: str, step: str, char: str = "", notes: str = "") -> list[str]:
    show = Show(show_id)
    st = show.bootstrap_state()
    payload = {"manual": True, "notes": notes}
    if step == "concept":
        st["concept"]["status"] = "approved"
        _emit(show, CONCEPT_APPROVED, payload)
    elif step == "bible":
        st["bible"]["status"] = "approved"
        _emit(show, BIBLE_APPROVED, payload)
    elif step in ("character", "voice"):
        entry = _find_char(st, char)
        if entry is None:
            raise ValueError(f"no character named '{char}' in bootstrap state")
        if step == "character":
            entry["proposal"] = "approved"
            entry["refs"] = "approved"
            entry["voice"] = "approved"
            _emit(show, CHAR_PROPOSAL_APPROVED, {**payload, "char": char})
            _emit(show, VOICE_APPROVED, {**payload, "char": char})
        else:
            entry["voice"] = "approved"
            _emit(show, VOICE_APPROVED, {**payload, "char": char})
    elif step == "scenes":
        st["scenes"]["status"] = "approved"
        st["complete"] = True
        _emit(show, SCENE_REGISTRY_APPROVED, payload)
    else:
        raise ValueError(f"unknown step '{step}' ({'|'.join(GATE_STEPS)})")
    show.set_bootstrap_state(st)

    # Approving a gate unlocks the next one: advance the chain so the next stage
    # generates (and lands pending for review). Manual mode stops at the next gate.
    messages = [f"{step} approved"]
    try:
        for line in BootstrapChain(show).advance():
            messages.append(line)
    except Exception as exc:
        raise ValueError(
            f"{step} approved, but generating the next stage failed ({exc}). "
            f"The approval is recorded - retry and it will regenerate.") from exc
    finally:
        ACTIVITY.pop(show_id, None)
    return messages


def reject_step(show_id: str, step: str, char: str = "", notes: str = "no notes") -> list[str]:
    """Reject an artifact and regenerate it from the rejection notes.

    The artifact is first persisted as `rejected` (rejection is never a dead
    end), then a new draft is generated with the notes as feedback and lands in
    `pending`. If regeneration fails, the artifact stays `rejected` and a clear
    error is raised so the user can retry.
    """

    show = Show(show_id)
    chain = BootstrapChain(show)
    st = show.bootstrap_state()
    payload = {"manual": True, "notes": notes}
    label = "bible" if step == "bible" else step

    def _regenerate(regen, reject_evt, pending_evt, mark_pending, extra=None):
        st = show.bootstrap_state()
        mark_pending(st, "rejected")
        show.set_bootstrap_state(st)
        _emit(show, reject_evt, {**payload, **(extra or {})})
        try:
            regen()
        except Exception as exc:
            raise ValueError(
                f"{label} rejected; regeneration failed ({exc}). "
                f"It is marked rejected - review the notes and try again.") from exc
        st = show.bootstrap_state()
        mark_pending(st, "pending")
        show.set_bootstrap_state(st)
        _emit(show, pending_evt, {**payload, **(extra or {})})

    try:
        if step == "concept":
            _regenerate(lambda: chain.generate_concept(feedback=notes),
                        CONCEPT_REJECTED, CONCEPT_PENDING,
                        lambda st, s: st.__setitem__("concept", {"status": s, "rejected_notes": notes}))
        elif step == "bible":
            _regenerate(lambda: chain.generate_bible(feedback=notes),
                        BIBLE_REJECTED, BIBLE_PENDING,
                        lambda st, s: st.__setitem__("bible", {"status": s, "rejected_notes": notes}))
        elif step == "character":
            def mark_char(st, status):
                for entry in st.get("characters", []):
                    if entry.get("name") == char:
                        entry["proposal"] = status
                        if status == "rejected":
                            # Whole-character reject -> LLM rewrites the FULL spec,
                            # then both the ref image and voice regenerate from it.
                            entry["refs"] = ""
                            entry["voice"] = ""
                        entry["rejected_notes"] = notes
                        return
                raise ValueError(f"no character named '{char}' in bootstrap state")
            _regenerate(lambda: chain.generate_character_proposal(char, feedback=notes),
                        CHAR_PROPOSAL_REJECTED, CHAR_PROPOSAL_PENDING, mark_char,
                        extra={"char": char})
            c = _char_by_name(show, char)
            if c:
                _clear_refs(show, c.get("id", ""))
                _clear_voice(show, c.get("id", ""))
            # rebuild the review package (ref image + voice) from the new spec
            chain.advance()
        elif step == "refs":
            # Reject only the reference image; keep the proposal text and voice.
            entry = _find_char(st, char)
            if entry is None:
                raise ValueError(f"no character named '{char}' in bootstrap state")
            entry["refs"] = ""
            entry["rejected_notes"] = notes
            show.set_bootstrap_state(st)
            c = _char_by_name(show, char)
            if not c:
                raise ValueError(f"no character named '{char}' in bootstrap state")
            chain.revise_character_appearance(c, notes)
            _clear_refs(show, c.get("id", ""))
            kind = chain.bootstrap_refs(c)
            st2 = show.bootstrap_state()
            e2 = _find_char(st2, char)
            if e2:
                e2["refs"] = kind
                e2["rejected_notes"] = notes
                show.set_bootstrap_state(st2)
        elif step == "voice":
            # Reject only the voice sample; keep the proposal text and image.
            entry = _find_char(st, char)
            if entry is None:
                raise ValueError(f"no character named '{char}' in bootstrap state")
            entry["voice"] = "rejected"
            entry["rejected_notes"] = notes
            show.set_bootstrap_state(st)
            _emit(show, VOICE_REJECTED, {**payload, "char": char})
            c = _char_by_name(show, char)
            if c:
                chain.revise_voice_description(c, notes)
                _clear_voice(show, c.get("id", ""))
                chain.bootstrap_voice(c)
            st2 = show.bootstrap_state()
            e2 = _find_char(st2, char)
            if e2:
                e2["voice"] = "pending"
                e2["rejected_notes"] = notes
                show.set_bootstrap_state(st2)
        elif step == "scenes":
            def mark_scenes(st, status):
                st["scenes"] = {"status": status, "rejected_notes": notes}
                st["complete"] = status == "pending"
            _regenerate(lambda: chain.generate_scenes(feedback=notes),
                        SCENE_REGISTRY_REJECTED, SCENE_REGISTRY_PENDING, mark_scenes)
        elif step == "scene":
            # Reject only ONE scene's reference image; keep the rest of the
            # registry. Revise the scene's setting_prompt from the notes, clear
            # its ref dir, and regenerate just that image.
            import yaml
            sid = char
            p = show.scenes_dir / f"{sid}.yaml"
            if not p.exists():
                raise ValueError(f"no scene named '{sid}'")
            scene = dict(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
            if notes.strip():
                chain.revise_scene_setting(scene, sid, notes)
            rd = show.scenes_dir / sid / "refs"
            if rd.exists():
                for f in list(rd.iterdir()):
                    try:
                        f.unlink()
                    except Exception:
                        pass
            chain.bootstrap_scene_ref(scene, sid)
        else:
            raise ValueError(f"unknown step '{step}' ({'|'.join(GATE_STEPS)})")
    finally:
        ACTIVITY.pop(show_id, None)
    if step == "scene":
        return [f"scene {char} image rejected; regenerating from your notes"]
    return [f"{label} rejected; regenerated from notes"]


# ---------------------------------------------------------------------------
# Gate 3 — story (plan + script) per episode
# ---------------------------------------------------------------------------

def _episode_label(episode: int | str) -> str:
    ep = str(episode).upper()
    if ep.startswith("EP"):
        ep = ep[2:]
    return f"EP{int(ep):02d}"


def story_approval_path(show_id: str, episode: int | str) -> Path:
    show = Show(show_id)
    return show.dir / "runs" / _episode_label(episode) / "story.approval.json"


def story_state(show_id: str, episode: int | str) -> dict[str, Any]:
    p = story_approval_path(show_id, episode)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"status": "pending", "notes": "", "decided_at": None}


def _set_story(show_id: str, episode: int | str, status: str, notes: str) -> None:
    p = story_approval_path(show_id, episode)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "status": status,
        "notes": notes or "",
        "decided_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2), encoding="utf-8")


def approve_story(show_id: str, episode: int | str, notes: str = "") -> list[str]:
    _set_story(show_id, episode, "approved", notes)
    _emit(Show(show_id), SCRIPT_APPROVED, {"episode": str(episode), "manual": True, "notes": notes})
    return [f"{_episode_label(episode)} script approved"]


def reject_story(show_id: str, episode: int | str, notes: str = "no notes") -> list[str]:
    _set_story(show_id, episode, "rejected", notes)
    _emit(Show(show_id), SCRIPT_REJECTED, {"episode": str(episode), "manual": True, "notes": notes})
    return [f"{_episode_label(episode)} script rejected"]
