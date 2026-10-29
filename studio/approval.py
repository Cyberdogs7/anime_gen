"""Human approval actions shared by the CLI and the board (DESIGN §9.5).

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

from . import fsutil
from . import bootstrap
from .config import get_config
from .events import (
    BIBLE_APPROVED, BIBLE_PENDING, BIBLE_REJECTED, CHAR_PROPOSAL_APPROVED,
    CHAR_PROPOSAL_PENDING, CHAR_PROPOSAL_REJECTED, CHAR_REFS_APPROVED,
    CHAR_REFS_PENDING, CHAR_REFS_REJECTED, CONCEPT_APPROVED, CONCEPT_PENDING,
    CONCEPT_REJECTED, COSTUME_APPROVED, COSTUME_REJECTED, OBJECT_REF_APPROVED,
    OBJECT_REF_REJECTED, SCENE_REGISTRY_APPROVED, SCENE_REGISTRY_PENDING,
    SCENE_REGISTRY_REJECTED, SCRIPT_APPROVED, SCRIPT_REJECTED, VOICE_APPROVED,
    VOICE_REJECTED, new_event, record,
)
from .show import Show

GATE_STEPS = ("concept", "bible", "character", "refs", "voice", "scenes")


def _emit(show: Show, event_type: str, payload: dict[str, Any] | None = None) -> None:
    record(new_event(event_type, show_id=show.show_id, payload=payload or {}), show.dir)


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
                 costume: str = "", slug: str = "", episode: int | str = "",
                 advance: bool = True) -> list[str]:
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
        # Approving the nude identity root unlocks the clothed canonical (nude-first:
        # the outfit is only rendered once the body is approved).
        from .refs import NUDE_LABEL
        if label.strip().lower() == NUDE_LABEL:
            # Approving the nude unlocks the clothed canonical — render it in the
            # BACKGROUND so the approve click returns immediately (the dashboard
            # polls and the canonical appears when it lands).
            import threading
            cid = c.get("id", "")

            def _gen() -> None:
                try:
                    from .clients.lmstudio import LMStudioClient
                    from .config import get_config
                    from .refs import generate_refs
                    llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=240)
                except Exception:
                    llm = None
                try:
                    generate_refs(show, cid, c, llm=llm, force=False)
                except Exception:
                    pass

            threading.Thread(target=_gen, daemon=True, name="nude-approve-gen").start()
            return [f"nude approved for {char} — generating the clothed canonical…"]
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
        c = _char_by_name(show, char)
        # The refs gate also covers episode-supporting characters (enemy/group
        # units) that have no bootstrap roster entry; only their base ref is
        # approvable here, recorded in refs.json. The proposal/voice gates still
        # require a bootstrap entry.
        if entry is None and (step != "refs" or not (c and c.get("id"))):
            raise ValueError(f"no character named '{char}' in bootstrap state")
        if step == "character":
            # Approving the proposal approves ONLY the proposal. Refs and
            # voice have their own Gate 0 stages; marking them here would
            # silently skip that work (their workers skip "approved" entries).
            entry["proposal"] = "approved"
            if c and c.get("id"):
                from .storyboard import mark_base_ref_approved
                mark_base_ref_approved(show, c.get("id"))
            _emit(show, CHAR_PROPOSAL_APPROVED, {**payload, "char": char})
        elif step == "refs":
            persisted = False
            if entry is not None:
                entry["refs"] = "approved"
                persisted = True
            if c and c.get("id"):
                from .storyboard import mark_base_ref_approved
                persisted = mark_base_ref_approved(show, c["id"]) or persisted
            if not persisted:
                # Nothing was stored anywhere: the character has no bootstrap
                # entry and no refs.json, so the "approval" would be a no-op that
                # still claims success. Fail loudly instead.
                raise ValueError(
                    f"cannot approve the base ref for '{char}': no reference "
                    f"image / refs.json exists yet")
            _emit(show, CHAR_REFS_APPROVED, {**payload, "char": char})
        else:
            entry["voice"] = "approved"
            _emit(show, VOICE_APPROVED, {**payload, "char": char})
    elif step == "scene":
        # Approve one registry location; the registry completes when every
        # tracked location is approved.
        locs = st.setdefault("scenes", {}).setdefault("locations", {})
        sid = str(slug or char or "").strip()
        if not sid:
            raise ValueError("no scene id given for per-scene approval")
        if sid not in locs:
            on_disk = [p.stem for p in show.scenes_dir.glob("*.yaml")] \
                if show.scenes_dir.exists() else []
            if sid in on_disk:
                locs[sid] = "pending"
            else:
                raise ValueError(f"no scene '{sid}' in the registry")
        locs[sid] = "approved"
        if locs and all(v == "approved" for v in locs.values()):
            st["scenes"]["status"] = "approved"
            show.set_bootstrap_state(st)
            _emit(show, SCENE_REGISTRY_APPROVED, payload)
            return [f"scene {sid} approved; registry complete"]
        show.set_bootstrap_state(st)
        _emit(show, SCENE_REGISTRY_APPROVED,
              {**payload, "scene": sid, "partial": True})
        return [f"scene {sid} approved"]
    elif step == "scenes":
        st["scenes"]["status"] = "approved"
        if advance:
            st["complete"] = True
        # Approving the registry accepts the current scene refs: clear any
        # per-scene REJECTED markers so the UI stops flagging them.
        try:
            import yaml as _yaml
            for sp in show.scenes_dir.glob("*.yaml"):
                d = dict(_yaml.safe_load(sp.read_text(encoding="utf-8")) or {})
                if d.pop("rejected_notes", None) is not None:
                    sp.write_text(_yaml.safe_dump(d, allow_unicode=True, sort_keys=False),
                                  encoding="utf-8")
        except Exception:
            pass
        _emit(show, SCENE_REGISTRY_APPROVED, payload)
    else:
        raise ValueError(f"unknown step '{step}' ({'|'.join(GATE_STEPS)})")
    show.set_bootstrap_state(st)

    # Approving a gate unlocks the next one: advance the chain so the next stage
    # generates (and lands pending for review). Manual mode stops at the next gate.
    messages = [f"{step} approved"]
    if advance:
        try:
            for line in bootstrap.BootstrapChain(show).advance():
                messages.append(line)
        except Exception as exc:
            raise ValueError(
                f"{step} approved, but generating the next stage failed ({exc}). "
                f"The approval is recorded - retry and it will regenerate.") from exc
        finally:
            bootstrap.ACTIVITY.pop(show_id, None)
    return messages


def approve_proposal(show_id: str, char: str, notes: str = "") -> list[str]:
    """Approve only a character's text proposal (Gate 0 plan card)."""
    show = Show(show_id)
    st = show.bootstrap_state()
    entry = next((c for c in st.get("characters", []) if c.get("name") == char), None)
    if entry is None:
        raise ValueError(f"no character named '{char}'")
    entry["proposal"] = "approved"
    show.set_bootstrap_state(st)
    _emit(show, CHAR_PROPOSAL_APPROVED,
          {"char": char, "manual": True, "notes": notes})
    return [f"{char} proposal approved"]


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
    chain = bootstrap.BootstrapChain(show)
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
        from .objects import mark_rejected
        mark_rejected(show, ep, s, notes)
        _emit(show, OBJECT_REF_REJECTED, {**payload, "episode": str(ep), "object": s})
        return [f"object ref '{s}' rejected; regenerated from notes"]

    if step == "char_ref":
        # Feedback on a character's BASE reference image — works for bootstrap AND
        # episode-supporting characters (enemy units, group units).
        from .casting import regenerate_character_ref
        if not regenerate_character_ref(show, char, notes):
            raise ValueError(f"character ref for '{char}' regeneration failed")
        c = _char_by_name(show, char)
        if c and c.get("id"):
            # A freshly regenerated base ref needs re-approval: drop it from the
            # refs.json approved list too (regenerate_character_ref preserves it).
            from .storyboard import unapprove_base_ref
            unapprove_base_ref(show, c["id"])
            from . import refs as _refs
            _refs.mark_rejected(show, c["id"], "base", notes)
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
            cid = c.get("id", "")
            from .refs import regenerate_base
            # Notes apply ONLY to the base ref: revise its outfit and re-render it
            # from the approved nude. NEVER re-roll the nude from here.
            if not regenerate_base(show, cid, c, cfg=chain.cfg, llm=chain.llm,
                                   notes=notes):
                raise ValueError("base regen needs an approved nude first — approve "
                                 "the nude, then regenerate the base")
            kind = "real"
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
            # Record the rejection on the scene so the UI can flag this scene's
            # ref as REJECTED — awaiting rerender (cleared when re-approved).
            try:
                sp = show.scenes_dir / f"{sid}.yaml"
                cur = dict(yaml.safe_load(sp.read_text(encoding="utf-8")) or {})
                cur["rejected_notes"] = notes
                sp.write_text(yaml.safe_dump(cur, allow_unicode=True, sort_keys=False),
                              encoding="utf-8")
            except Exception:
                pass
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
            from . import refs
            rd = show.character_refs_dir(c.get("id"))
            prior = refs.load_refs(show, c.get("id"))
            if not prior:
                raise ValueError("character has no refs.json")
            variants = prior.get("variants") or {}
            if label not in variants:
                raise ValueError(f"no costume variant '{label}' for {cname}")
            name = c.get("name", cname)
            canon = (c.get("appearance_canonical") or "").strip()
            # Regenerating the NUDE identity root: the full pipeline revises the BODY
            # (and keeps the canonical look in sync) from the notes, then re-renders.
            if label.strip().lower() == "nude":
                from .clients.lmstudio import LMStudioClient
                from .config import get_config
                from .refs import regenerate_nude
                cfg_ = get_config()
                try:
                    llm = LMStudioClient(cfg_.get("llm", "base_url"), timeout=240)
                except Exception:
                    llm = None
                if not regenerate_nude(show, c.get("id", ""), c, cfg=cfg_, llm=llm,
                                       notes=notes):
                    raise ValueError("nude ref generation failed (ComfyUI/LLM unavailable?)")
                return [f"nude ref for {cname} regenerated"]
            # With feedback, LLM-revise the costume DESCRIPTION (driven by
            # personality + the label alone, never the current look) and rebuild
            # the edit prompt. 'nude' is an appearance strip, not a designed
            # costume, so it skips the description entirely.
            from .prompts import costume_edit_prompt
            new_prompt = None
            if mode == "edit" and label.strip().lower() != "nude":
                personality = ""
                pers = c.get("personality") or []
                if isinstance(pers, list):
                    personality = ", ".join(str(x) for x in pers)
                else:
                    personality = str(pers or "")
                # Rebuild the description every time (with any feedback) so a
                # regenerate always uses the current edit framing — reusing the
                # stored prompt would leave the variant stuck on whatever it was
                # originally generated with.
                try:
                    desc = bootstrap.BootstrapChain(show).describe_costume(
                        label, name, personality, feedback=notes)
                except Exception:
                    desc = ""
                if desc:
                    new_prompt = costume_edit_prompt(desc)
                # LLM unavailable/empty: fall back to the raw notes, else the
                # stored prompt, so a regen still runs.
                if not new_prompt:
                    stored_prompt = (prior.get("prompts") or {}).get(label, "")
                    new_prompt = (notes.strip() or stored_prompt) or None
            # Remove the old image + registry entry.
            old = rd / variants[label]
            if old.exists():
                try:
                    old.unlink()
                except Exception:
                    pass
            variants.pop(label, None)
            prior["variants"] = variants
            refs.save_refs(show, c.get("id"), prior)
            # Regenerate just this variant.
            from .storyboard import generate_costume_variant
            generate_costume_variant(show, c.get("id"), name, label, canon,
                                     prompt=new_prompt, mode=mode)
            refs.mark_rejected(show, c.get("id"), label, notes)
        else:
            raise ValueError(f"unknown step '{step}' ({'|'.join(GATE_STEPS)})")
    finally:
        bootstrap.ACTIVITY.pop(show_id, None)
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
    """Derived Gate-3 state for one episode.

    The episode reads as approved ONLY when its outline and scene details are
    approved and its script passed review (or was overridden). ``story.approval.json``
    is kept purely as a notes/audit mirror — it is never the source of truth, so
    the displayed state can never disagree with the gates the pipeline actually
    reads (plan.json / scene_details.json / the script's ``_review``).
    """
    from .planner import read_episode_plan, read_scene_details
    from .storyboard import _latest_script
    show = Show(show_id)
    ep = _episode_num(episode)
    mirror: dict[str, Any] = {}
    p = story_approval_path(show_id, episode)
    if p.exists():
        try:
            mirror = json.loads(p.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            mirror = {}
    plan = read_episode_plan(show, ep)
    details = read_scene_details(show, ep)
    pstat = (plan.get("status") or "").strip()
    dstat = (details.get("status") or "").strip()
    script, _ = _latest_script(show, ep)
    review = (script or {}).get("_review") or {}
    if "rejected" in (pstat, dstat):
        status = "rejected"
    elif pstat != "approved" or dstat != "approved" or not script:
        status = "pending"
    elif review.get("passed") is False:
        status = "pending"
    else:
        status = "approved"
    return {"status": status, "notes": mirror.get("notes", ""),
            "decided_at": mirror.get("decided_at")}


def _set_story(show_id: str, episode: int | str, status: str, notes: str) -> None:
    p = story_approval_path(show_id, episode)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "status": status,
        "notes": notes or "",
        "decided_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2), encoding="utf-8")


def approve_story(show_id: str, episode: int | str, notes: str = "") -> list[str]:
    """Episode-level sign-off.

    Approves the underlying gates the derived story state represents (outline +
    scene details) when they exist, so clicking "Approve" actually advances the
    pipeline instead of writing a cosmetic flag the pipeline never reads. The
    script-review gate stays separate (use approve_script to override it).
    """
    from .planner import approve_plan, approve_scene_details, read_episode_plan, read_scene_details
    show = Show(show_id)
    ep = _episode_num(episode)
    msgs: list[str] = []
    plan = read_episode_plan(show, ep)
    if plan and (plan.get("status") or "") != "approved":
        try:
            # A wall "Approve" is an explicit human sign-off, so it overrides a
            # reviewer-rejected outline (mirrors the dashboard override button).
            approve_plan(show, ep, override=True)
            msgs.append(f"{_episode_label(ep)} outline approved")
        except Exception as exc:
            msgs.append(f"{_episode_label(ep)} outline not approved: {exc}")
    details = read_scene_details(show, ep)
    if details and (details.get("status") or "") != "approved":
        approve_scene_details(show, ep)
        msgs.append(f"{_episode_label(ep)} scene details approved")
    _set_story(show_id, episode, "approved", notes)
    _emit(show, SCRIPT_APPROVED, {"episode": str(episode), "manual": True, "notes": notes})
    if not msgs:
        msgs.append(f"{_episode_label(ep)} approved")
    return msgs


def approve_script(show_id: str, episode: int | str, notes: str = "") -> list[str]:
    """Override a failed script review so the pipeline can proceed to storyboard.

    Clears the ``_review`` failure state on the latest script revision, allowing
    the reconciler to advance past the script-review gate.
    """
    from .storyboard import _latest_script
    show = Show(show_id)
    raw = str(episode).strip().upper()
    if raw.startswith("EP"):
        raw = raw[2:]
    ep = int(raw)
    # _latest_script returns (script, path) — read BOTH so the override is written
    # back to the exact file it came from.
    script, path = _latest_script(show, ep)
    if not script or path is None:
        return [f"{_episode_label(ep)}: no script found"]
    review = script.get("_review") or {}
    if review.get("passed") is not False:
        return [f"{_episode_label(ep)}: script review already passed"]
    # Overwrite the script with the review marked as passed (human override).
    review["passed"] = True
    review["override"] = True
    review["override_notes"] = notes
    script["_review"] = review
    from . import epstate
    epstate.save_script(path, script)
    return [f"{_episode_label(ep)} script review overridden — pipeline will proceed"]


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


# ---------------------------------------------------------------------------
# Gate 4 — per-shot approve/reject (rendered clips)
# ---------------------------------------------------------------------------
# Per-shot approval state lives in runs/EP##/shots.approval.json:
#   { "<shot_id>": {"status": "approved"|"rejected"|"pending", "notes": "",
#                   "revised_prompt": "…", "retake_count": N, "decided_at": "…"} }
#
# A REJECT deletes the shot's clip and stores a revised render prompt (LLM
# rewrite of the current prompt from the rejection notes — the established
# feedback loop, never raw append). The reconciler sees video_count < shot_count
# and re-renders ONLY that shot with the stored revised prompt + a fresh seed.
# Successive rejects further refine the SAME stored prompt (mirrors the
# costume/object reject loop).


def shot_approval_path(show: "Show | str", episode: int | str) -> Path:
    s = show if isinstance(show, Show) else Show(show)
    return s.dir / "runs" / _episode_label(episode) / "shots.approval.json"


def shot_approvals(show: "Show | str", episode: int | str) -> dict[str, Any]:
    p = shot_approval_path(show, episode)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _save_shot_approvals(show: "Show | str", episode: int | str,
                         data: dict[str, Any]) -> None:
    p = shot_approval_path(show, episode)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def shot_state(show: "Show | str", episode: int | str, shot_id: str) -> dict[str, Any]:
    return shot_approvals(show, episode).get(shot_id, {"status": "pending"})


def approve_shot(show: "Show | str", episode: int | str, shot_id: str,
                 notes: str = "") -> list[str]:
    data = shot_approvals(show, episode)
    entry = data.setdefault(shot_id, {})
    entry["status"] = "approved"
    entry["notes"] = notes or ""
    entry["decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save_shot_approvals(show, episode, data)
    return [f"{shot_id} approved"]


def reject_shot(show: "Show | str", episode: int | str, shot_id: str,
                notes: str = "no notes", llm=None) -> list[str]:
    """Reject ONE rendered shot and re-render it from the rejection feedback.

    Records the rejection, LLM-revises the shot's stored render prompt from the
    notes (persisting the revised prompt so successive rejects refine it), bumps
    the retake counter, and deletes the clip so the reconciler re-renders ONLY
    this shot with the revised prompt + a fresh seed. Returns user-facing
    messages.

    ``llm`` may be injected (tests); when None a client is built for the
    configured LLM and the revision is best-effort (never a dead end).
    """
    from .qc import clear_shot_qc
    s = show if isinstance(show, Show) else Show(show)
    show_id = s.show_id
    ep = _episode_num(episode)
    label = _episode_label(ep)
    sid = shot_id.strip()
    runs = s.dir / "runs" / label

    # Current prompt: last revised one (persisted) else the prepared/compiled one.
    data = shot_approvals(s, ep)
    entry = data.setdefault(sid, {})
    current = entry.get("revised_prompt") or _current_shot_prompt(s, ep, sid)
    revised = current
    if notes.strip():
        from .clients.lmstudio import LMStudioClient
        from .prompts import revise_shot_prompt
        cfg = get_config()
        try:
            llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=60)
            model = (cfg.get("llm", "roles", {}).get("showrunner")
                     or cfg.get("llm", "model"))
            parsed = llm.chat_json([
                {"role": "system", "content": (
                    "You revise video-generation prompts for a show production "
                    "from the director's rejection notes.")},
                {"role": "user", "content": revise_shot_prompt(current, notes)},
            ], model=model, role="showrunner", temperature=0.4, max_tokens=1600, retries=1)
            candidate = (parsed.get("prompt") or "").strip()
            if len(candidate) > 40:
                revised = candidate
        except Exception:
            pass  # keep the current prompt; still re-render from it

    entry["status"] = "rejected"
    entry["notes"] = notes
    entry["revised_prompt"] = revised
    entry["retake_count"] = int(entry.get("retake_count", 0) or 0) + 1
    entry["decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save_shot_approvals(s, ep, data)

    # Delete the clip so the reconciler re-renders this shot.
    clip = runs / "video" / f"{sid}.mp4"
    if clip.exists():
        try:
            clip.unlink()
        except Exception:
            pass
    clear_shot_qc(s, ep, sid)
    return [f"{sid} rejected — re-rendering from your notes (retake "
            f"{entry['retake_count']}) in the background…"]


def _current_shot_prompt(show: Show, episode: int, sid: str) -> str:
    """The prompt the last render used for this shot: revised from shots.approval
    json first, then the prepared prompts.json, else the compiled base."""
    from .render import _latest_script, _load_prepared_prompts
    script = _latest_script(show, episode)
    if not script:
        return ""
    for sc in script.get("scenes", []):
        for sh in sc.get("shots", []):
            if sh.get("id") != sid:
                continue
            from .storyboard import _shot_refs
            names, _ = _shot_refs(show, sh)
            prepared = _load_prepared_prompts(show, episode)
            entry = prepared.get(sid)
            if entry and isinstance(entry, dict) and entry.get("rewritten"):
                return entry["rewritten"]
            from .render import compile_shot_prompt
            return compile_shot_prompt(show, script, sc, sh, names)
    return ""
