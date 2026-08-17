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
    CHAR_PROPOSAL_PENDING, CHAR_PROPOSAL_REJECTED, CHAR_REFS_APPROVED,
    CHAR_REFS_PENDING, CHAR_REFS_REJECTED, CONCEPT_APPROVED, CONCEPT_PENDING,
    CONCEPT_REJECTED, COSTUME_APPROVED, COSTUME_REJECTED, OBJECT_REF_APPROVED,
    OBJECT_REF_REJECTED, SCENE_REGISTRY_APPROVED, SCENE_REGISTRY_PENDING,
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


def _variant_label(show: Show, cid: str, notes: str) -> str:
    """Resolve a costume label from the reject payload.

    The frontend sends the costume label in a dedicated ``notes``/``costume``
    field (notes carries the reject feedback). For backward compatibility, if the
    notes don't match a known variant it is treated as the label.
    """
    rd = show.character_refs_dir(cid)
    rj = rd / "refs.json"
    known: list[str] = []
    if rj.exists():
        try:
            import json as _json
            prior = _json.loads(rj.read_text(encoding="utf-8"))
            known = list((prior.get("variants") or {}).keys())
        except Exception:
            pass
    label = notes.strip() or ""
    if label in known:
        return label
    # legacy: notes may be reject feedback, not a label -> we need the label;
    # require the payload to carry it. Fall back to matching by token.
    for k in known:
        if k.lower() in label.lower() or label.lower() in k.lower():
            return k
    raise ValueError(f"no costume variant for '{label}' (known: {known})")


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

def approve_step(show_id: str, step: str, char: str = "", notes: str = "",
                 costume: str = "", slug: str = "", episode: int | str = "") -> list[str]:
    show = Show(show_id)
    st = show.bootstrap_state()
    payload = {"manual": True, "notes": notes}
    if step == "costume":
        from .storyboard import mark_costume_approved
        c = _char_by_name(show, char)
        if not c:
            raise ValueError(f"no character named '{char}'")
        label = costume.strip()
        mark_costume_approved(show, c.get("id", ""), label)
        _emit(show, COSTUME_APPROVED, {**payload, "char": char, "costume": label})
        return [f"costume '{label}' for {char} approved"]
    if step == "object":
        from .storyboard import mark_object_approved
        ep = _episode_num(episode)
        mark_object_approved(show, ep, slug.strip())
        _emit(show, OBJECT_REF_APPROVED, {**payload, "episode": str(ep), "object": slug})
        return [f"object ref '{slug}' approved"]
    if step == "concept":
        st["concept"]["status"] = "approved"
        _emit(show, CONCEPT_APPROVED, payload)
    elif step == "bible":
        st["bible"]["status"] = "approved"
        _emit(show, BIBLE_APPROVED, payload)
    elif step in ("character", "voice", "refs"):
        entry = _find_char(st, char)
        if entry is None:
            raise ValueError(f"no character named '{char}' in bootstrap state")
        if step == "character":
            entry["proposal"] = "approved"
            entry["refs"] = "approved"
            entry["voice"] = "approved"
            _emit(show, CHAR_PROPOSAL_APPROVED, {**payload, "char": char})
            _emit(show, VOICE_APPROVED, {**payload, "char": char})
        elif step == "refs":
            entry["refs"] = "approved"
            _emit(show, CHAR_REFS_APPROVED, {**payload, "char": char})
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


def reject_step(show_id: str, step: str, char: str = "", notes: str = "no notes",
                costume: str = "", mode: str = "edit",
                slug: str = "", episode: int | str = "") -> list[str]:
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

    if step == "object":
        # Reject ONE recurring-object ref: unapprove it, drop the image, then
        # regenerate from the notes. Keeps the rest of the episode untouched.
        ep = _episode_num(episode)
        s = slug.strip()
        from .storyboard import (regenerate_object_ref, unapprove_object)
        if not regenerate_object_ref(show, ep, s, notes, cfg=None):
            raise ValueError(f"object ref '{s}' regeneration failed")
        unapprove_object(show, ep, s)
        _emit(show, OBJECT_REF_REJECTED, {**payload, "episode": str(ep), "object": s})
        return [f"object ref '{s}' rejected; regenerated from notes"]

    if step == "char_ref":
        # Feedback on a character's BASE reference image — works for bootstrap AND
        # episode-supporting characters (Apex-734, group units like the pods).
        from .casting import regenerate_character_ref
        if not regenerate_character_ref(show, char, notes):
            raise ValueError(f"character ref for '{char}' regeneration failed")
        entry = _find_char(st, char)
        if entry is not None:
            entry["refs"] = "pending"
            entry["rejected_notes"] = notes
            show.set_bootstrap_state(st)
        _emit(show, CHAR_REFS_REJECTED, {**payload, "char": char})
        return [f"character ref '{char}' rejected; regenerated from notes"]

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
        elif step == "costume":
            # Reject ONE costume variant: optionally LLM-revise its generation
            # prompt from the reject notes (same feedback loop as the base ref),
            # drop the old image, then regenerate just that variant.
            # `char` is the character name; `notes` carries the reject feedback.
            cname = char
            c = _char_by_name(show, cname)
            if not c or not c.get("id"):
                raise ValueError(f"no character named '{cname}'")
            label = costume.strip() or _variant_label(show, c.get("id"), notes)
            rd = show.character_refs_dir(c.get("id"))
            rj = rd / "refs.json"
            if not rj.exists():
                raise ValueError("character has no refs.json")
            prior = json.loads(rj.read_text(encoding="utf-8"))
            variants = prior.get("variants") or {}
            if label not in variants:
                raise ValueError(f"no costume variant '{label}' for {cname}")
            name = c.get("name", cname)
            canon = (c.get("appearance_canonical") or "").strip()
            # With feedback, LLM-revise the costume DESCRIPTION (driven by
            # personality + use case, never the current look) and rebuild the edit
            # prompt. Without feedback, regenerate with the stored prompt.
            new_prompt = None
            if mode == "edit":
                personality = ""
                pers = c.get("personality") or []
                if isinstance(pers, list):
                    personality = ", ".join(str(x) for x in pers)
                else:
                    personality = str(pers or "")
                situation = f"{name} wears '{label}' in combat"
                if notes.strip():
                    try:
                        desc = BootstrapChain(show).describe_costume(
                            label, name, personality, situation, feedback=notes)
                    except Exception:
                        desc = ""
                    new_prompt = f"Replace the character's clothes to: {desc}" if desc else None
                # When the LLM feedback path gave us nothing (desc empty), use the
                # raw rejection notes directly as the edit instruction instead of
                # silently falling back to the stale prompt.
                if not new_prompt:
                    stored_prompt = (prior.get("prompts") or {}).get(label, "")
                    if notes.strip() and notes.strip() not in (stored_prompt or ""):
                        new_prompt = notes.strip()
                    else:
                        new_prompt = stored_prompt or None
            # Remove the old image + registry entry.
            old = rd / variants[label]
            if old.exists():
                try:
                    old.unlink()
                except Exception:
                    pass
            variants.pop(label, None)
            prior["variants"] = variants
            rj.write_text(json.dumps(prior), encoding="utf-8")
            # Regenerate just this variant.
            from .storyboard import generate_costume_variant
            generate_costume_variant(show, c.get("id"), name, label, canon,
                                     prompt=new_prompt, mode=mode)
        else:
            raise ValueError(f"unknown step '{step}' ({'|'.join(GATE_STEPS)})")
    finally:
        ACTIVITY.pop(show_id, None)
    if step == "scene":
        return [f"scene {char} image rejected; regenerating from your notes"]
    if step == "costume":
        label = costume.strip() or _variant_label(show, char, notes)
        return [f"costume '{label}' for {char} rejected; regenerating"]
    return [f"{label} rejected; regenerated from notes"]


# ---------------------------------------------------------------------------
# Gate 3 — story (plan + script) per episode
# ---------------------------------------------------------------------------

def _episode_label(episode: int | str) -> str:
    ep = str(episode).upper()
    if ep.startswith("EP"):
        ep = ep[2:]
    return f"EP{int(ep):02d}"


def _episode_num(episode: int | str) -> int:
    """Parse an episode to its integer, tolerating the "EP01" dir/display form."""
    ep = str(episode).upper().replace("EP", "").strip()
    return int(ep or 0)


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
    from .planner import reject_plan, reject_scene_details
    show = Show(show_id)
    raw = str(episode).strip().upper()
    if raw.startswith("EP"):
        raw = raw[2:]
    ep = int(raw)
    # Cascading reject: story → plan → scene details → script → storyboard.
    reject_plan(show, ep, notes)
    reject_scene_details(show, ep, notes)
    # Delete the assembled script + storyboard so the reconciler rebuilds
    # from the new plan with current generation prompts.
    runs = show.dir / "runs" / _episode_label(ep)
    for r in sorted(runs.glob("script.r*.json")):
        try: r.unlink()
        except Exception: pass
    import shutil
    sbd = runs / "storyboard"
    if sbd.exists():
        shutil.rmtree(sbd)
    # The reconciler regenerates the plan from the rejection notes in its own
    # thread (never block the HTTP request on a long LLM regen — a client
    # timeout would abort it and strand the episode). needs_episode_reconcile
    # now returns True for a rejected plan, so the self-healer picks it up.
    return [f"{_episode_label(ep)} rejected — plan + scene details reset; "
            f"regenerating from your notes in the background…"]
