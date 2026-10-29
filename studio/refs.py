"""Single owner of character reference images.

EVERY path that creates or reads a character's refs goes through here — the
bootstrap Gate-0 cast, the episode ref pass, character tweaks, and growth — so
there is exactly ONE ref pipeline and ONE place that touches refs.json.

Pipeline (NUDE FIRST):
  1. body description        -> nude identity root (Krea 2)        the FIRST image
  2. nude + default outfit   -> clothed canonical (Qwen edit)      refs[0] / the base
  3. nude + costume label    -> costume variant (Qwen edit)

The nude is the identity root and the construction source for every outfit. It is
a selectable, approvable appearance variant but NEVER the base ref, so a shot that
declares no appearance still anchors the clothed canonical.

Nothing else in the codebase should render a character ref or read/write
refs.json directly.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from pathlib import Path
from typing import Any

from . import prompts
from .config import get_config
from .show import Show

NUDE_LABEL = "nude"


# ---------------------------------------------------------------------------
# State — the ONLY place refs.json is read/written.
# ---------------------------------------------------------------------------
# State — the ONLY place refs.json is read/written.
# ---------------------------------------------------------------------------
def load_refs(show: Show, cid: str) -> dict[str, Any]:
    p = show.character_refs_dir(cid) / "refs.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_refs(show: Show, cid: str, data: dict[str, Any]) -> None:
    rd = show.character_refs_dir(cid)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "refs.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def rejected_labels(show: Show, cid: str) -> dict[str, str]:
    """Variant labels marked REJECTED (awaiting rerender) -> rejection notes."""
    return dict((load_refs(show, cid) or {}).get("rejected") or {})


def mark_rejected(show: Show, cid: str, label: str, notes: str = "") -> None:
    """Mark one variant REJECTED (awaiting rerender): unapprove + record notes."""
    data = load_refs(show, cid)
    if not data:
        return
    data["approved"] = [a for a in (data.get("approved") or []) if a != label]
    rej = dict(data.get("rejected") or {})
    rej[label] = notes or ""
    data["rejected"] = rej
    save_refs(show, cid, data)


def clear_rejected(show: Show, cid: str, label: str) -> None:
    """Clear a variant's REJECTED marker (cleared on re-render / approval)."""
    data = load_refs(show, cid)
    if not data:
        return
    rej = dict(data.get("rejected") or {})
    if label in rej:
        rej.pop(label, None)
        if rej:
            data["rejected"] = rej
        else:
            data.pop("rejected", None)
        save_refs(show, cid, data)


def mark_stub(show: Show, cid: str, note: str) -> None:
    """Record a failure stub WITHOUT clobbering an existing real/nude ref.

    A bootstrap/growth render failure must never overwrite refs that another pass
    already produced (that would erase a rendered nude and its registration)."""
    data = load_refs(show, cid)
    if data.get("status") == "real" or data.get("variants"):
        return
    data["status"] = "stub"
    data["note"] = note
    save_refs(show, cid, data)


def is_person(char: dict[str, Any]) -> bool:
    """Whether the subject is a PERSON (gets the nude-first treatment) rather than
    a non-person subject (mech / machine / creature / vehicle / group).

    Read from the character sheet's ``is_person`` field, set by the character
    writer from the request's own content (or LLM-backfilled once). When the field
    is ABSENT the answer is unknown, so it defaults to **False** (a non-person only
    gets a plain clothed ref; the flag is written to the sheet on the next
    generation, so recurrences become correct).
    """
    return bool(char.get("is_person", False))


def is_recurring_cast(show: Show, char: dict[str, Any]) -> bool:
    """True when the character is part of the show's RECURRING cast (not an
    episode one-off): the bootstrap roster, the overall-plotline cast, or the
    bible's cast list."""
    name = (char.get("name") or "").strip().lower()
    if not name:
        return False
    roster = {str(c.get("name") or "").strip().lower()
              for c in (show.bootstrap_state().get("characters") or [])}
    bible = show.read_bible() or {}
    op = [str(n).strip().lower()
          for n in ((bible.get("overall_plotline") or {}).get("characters") or [])]
    cast = [str(c.get("name") or "").strip().lower()
            for c in (bible.get("cast") or []) if isinstance(c, dict)]
    return name in roster or name in op or name in cast


def wants_nude_first(show: Show, cid: str, char: dict[str, Any] | None = None) -> bool:
    """Full nude-first treatment ONLY for recurring, person characters. Supporting,
    background and non-person subjects get a plain clothed ref (a nude is still
    available on demand if a shot declares one)."""
    char = char if char is not None else show.read_character(cid)
    return is_recurring_cast(show, char) and is_person(char)


# ---------------------------------------------------------------------------
# Body description (identity root input)
# ---------------------------------------------------------------------------
def identity_canonical(char: dict[str, Any]) -> str:
    """The character's garmentless BODY description (identity root).

    Reads the structured field only. Legacy sheets are split once by the LLM
    (``_backfill_identity``) instead of parsing ``appearance_canonical``.
    """
    return (char.get("identity_canonical") or "").strip()


def _style(show: Show) -> str:
    return show.render_style()


def _style_anchor_arg(show: Show) -> str:
    """style_ref path for a render, or '' when the anchor is off/absent."""
    if not get_config().get("comfy", "style_anchor", False):
        return ""
    p = show.dir / "style_anchor.png"
    return str(p) if p.exists() else ""


# ---------------------------------------------------------------------------
# Low-level render primitives — the ONLY ref render entry points.
# ---------------------------------------------------------------------------
def _render_costume_via_krea2(show: Show, name: str, prompt: str, label: str,
                              out_path: str, cfg, seed: int = 0) -> bool:
    """Fresh krea2 text-to-image render (no identity anchor)."""
    from .bootstrap import ACTIVITY
    from .remote.ops import ServiceOps
    from .comfy_workflows import generate_keyframe, load_workflow
    from .gpu_manager import ServiceType, get_gpu_manager
    ops = ServiceOps(cfg)
    client, stop = ops._krea2_client()
    try:
        # krea2 runs on its own instance (no sage-attention); serialize with H3 via
        # the GPU manager's exclusive COMFYUI lock so they never share VRAM.
        with get_gpu_manager(cfg).acquire(ServiceType.COMFYUI):
            generate_keyframe(client, load_workflow(cfg.workflows_dir / "image_keyframe.json"),
                              prompt, seed, out_path, aspect_ratio="3:4",
                                style_ref=_style_anchor_arg(show) or None)
        return True
    except Exception as exc:
        ACTIVITY.setdefault(show.show_id, {})["detail"] = f"Krea2 ref {label} failed: {exc}"
        # Loud, not silent: callers stub the ref with this message, so the
        # card names the ComfyUI cause instead of "ref generation failed".
        raise RuntimeError(f"Krea2 {label} render failed: {exc}") from exc
    finally:
        if stop is not None:
            stop()


def _render_costume_via_qwen_edit(show: Show, base_ref: str | None, name: str,
                                  prompt: str, label: str, out_path: str,
                                  cfg, seed: int = 0) -> bool:
    """Render a costume/outfit via Qwen-Image-Edit from a base ref (identity
    preserved). Falls back to a fresh krea2 render when there is no base image."""
    from .remote.ops import ServiceOps
    from .gpu_manager import ServiceType, get_gpu_manager
    cfg = cfg or get_config()
    ops = ServiceOps(cfg)
    client, stop = ops._krea2_client()
    try:
        if not base_ref:
            # No base image at all -> a fresh render is the only option.
            return _render_costume_via_krea2(show, name, prompt, label, out_path, cfg)
        ref_fn = None
        try:
            ref_fn = client.upload_image(base_ref)
        except Exception as exc:
            from .bootstrap import ACTIVITY
            ACTIVITY.setdefault(show.show_id, {})["detail"] = (
                f"Qwen edit {label}: source upload failed ({exc})")
        if not ref_fn:
            # A base image WAS expected: do NOT silently fall back to a fresh
            # (different-style) render — surface the failure instead.
            import logging
            logging.getLogger(__name__).warning(
                "Qwen edit %s: could not upload edit source %s — not falling back",
                label, base_ref)
            raise RuntimeError(
                f"Qwen edit {label}: could not upload edit source; not falling back")
        with get_gpu_manager(cfg).acquire(ServiceType.COMFYUI):
            return _qwen_edit_render(show, client, cfg, prompt, ref_fn, label, out_path, seed)
    finally:
        if stop:
            stop()


def _qwen_edit_render(show: Show, client, cfg, prompt: str, ref_fn: str, label: str,
                      out_path: str, seed: int) -> bool:
    """Run one Qwen-Image-Edit render (GPU already owned exclusively)."""
    from .bootstrap import ACTIVITY
    from .qwen_edit import adapt_qwen_edit_workflow, load_qwen_edit_workflow
    sid = show.show_id
    try:
        wf = load_qwen_edit_workflow(cfg.root)
        wf = adapt_qwen_edit_workflow(wf, prompt, ref_fn, seed=seed, steps=8, cfg=4.0)
        pid = client.submit(wf)
        # Hard wall-clock cap: a wedged Qwen job must not block the whole pass.
        entry = client.wait(pid, timeout_s=900, poll_interval=5, hard_timeout_s=1200)
        status = (entry.get("status") or {}).get("status_str", "")
        if status != "success":
            ACTIVITY.setdefault(sid, {})["detail"] = f"Qwen edit {label} status: {status}"
            raise RuntimeError(f"Qwen edit {label} failed with status: {status or 'unknown'}")
        img = None
        for _nid, output in (entry.get("outputs") or {}).items():
            for i in output.get("images", []):
                img = i
                break
            if img:
                break
        if not img:
            ACTIVITY.setdefault(sid, {})["detail"] = f"Qwen edit {label}: no output image"
            raise RuntimeError(f"Qwen edit {label} produced no output image")
        client.download(img["filename"], img.get("subfolder", ""),
                        img.get("type", "output"), out_path)
        return True
    except Exception as exc:
        import logging
        ACTIVITY.setdefault(sid, {})["detail"] = f"Qwen edit {label} failed: {exc}"
        logging.getLogger(__name__).warning("qwen edit failed for %s: %s", label, exc,
                                            exc_info=True)
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Qwen edit {label} failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Step 1 — the nude identity root (Krea 2)
# ---------------------------------------------------------------------------
def ensure_nude_ref(show: Show, cid: str, name: str, cfg=None,
                    force: bool = False, llm=None, model: str = "",
                    notes: str = "") -> str | None:
    """Garmentless "nude" identity root, rendered by Krea 2 from the body
    description — the FIRST image of the character. Cached by a hash of the body
    text (staleness = a body tweak). Registers ``variants["nude"]`` (unapproved);
    never the base ref. Returns the image path or None.
    """
    cfg = cfg or get_config()
    rd = show.character_refs_dir(cid)
    prior = load_refs(show, cid)
    char = show.read_character(cid)
    identity = identity_canonical(char)
    if not identity:
        return None
    ident_hash = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
    nude_fn = (prior.get("variants") or {}).get(NUDE_LABEL)
    if (not force and nude_fn and (rd / nude_fn).exists()
            and prior.get("nude_identity") == ident_hash):
        return str(rd / nude_fn)
    out = rd / f"{cid}_{NUDE_LABEL}.png"
    prompt = prompts.nude_ref_prompt(name, identity, _style(show), show.ref_medium(),
                                     gender=char.get("gender") or "")
    if notes.strip():
        prompt = prompt.rstrip() + " Director's revision: " + notes.strip()

    def _render() -> bool:
        return _render_costume_via_krea2(show, name, prompt, NUDE_LABEL, str(out),
                                         cfg, seed=random.randrange(1, 2**63))

    if llm is not None and model:
        # VLM single-figure gate: retry with fresh seeds until one figure lands.
        from .consistency import render_until_single
        gate = render_until_single(_render, out, llm=llm, model=model,
                                   label=f"nude root {name}")
        if not gate["ok"]:
            return None
    elif not _render():
        return None
    if not out.exists():
        return None
    prior.setdefault("variants", {})[NUDE_LABEL] = out.name
    prior.setdefault("prompts", {})[NUDE_LABEL] = prompt
    prior.setdefault("modes", {})[NUDE_LABEL] = "krea2"
    prior["nude_identity"] = ident_hash
    # A fresh/regenerated nude needs approval before a shot anchors on it.
    prior["approved"] = [l for l in (prior.get("approved") or []) if l != NUDE_LABEL]
    prior["status"] = "real"
    save_refs(show, cid, prior)
    return str(out)


def _drop_derived(show: Show, cid: str) -> None:
    """Remove every outfit derived from a character's nude (the canonical base +
    costumes) plus their approvals — used when the nude is unapproved, so nothing
    renders against an unapproved body and no stale base lingers."""
    rd = show.character_refs_dir(cid)
    data = load_refs(show, cid)
    variants = data.get("variants") or {}
    for label in [l for l in list(variants) if l != NUDE_LABEL]:
        f = variants.pop(label, None)
        if f:
            try:
                (rd / f).unlink()
            except Exception:
                pass
        for k in ("prompts", "seeds", "modes"):
            (data.get(k) or {}).pop(label, None)
    data["variants"] = variants
    data["approved"] = [l for l in (data.get("approved") or []) if l == NUDE_LABEL]
    save_refs(show, cid, data)


# ---------------------------------------------------------------------------
# Body revision (character tweaks)
# ---------------------------------------------------------------------------
def _revise_identity(llm, model: str, name: str, current: str, notes: str,
                     bible: dict[str, Any] | None = None, style: str = "") -> str:
    """LLM rewrites ONLY the BODY (identity_canonical) from rejection feedback."""
    world = (f"Series bible (world, tone, style): {json.dumps(bible, ensure_ascii=False)}\n"
             if bible else "")
    try:
        data = llm.chat_json([
            {"role": "user", "content":
             f"Revise this character's BODY-ONLY identity description to address the "
             f"director's feedback (body, gender, face, hair, build — no clothing). "
             f"Keep everything the feedback did not ask to change.\n"
             f"CHARACTER: {name}\n"
             f"{world}"
             + (f"Visual style guide: {style}\n" if style else "")
             + f"CURRENT BODY:\n{current or '(none)'}\n"
             f"DIRECTOR'S FEEDBACK:\n{notes}\n"
             + prompts.identity_canonical_rules() + "\n"
             'Reply with ONLY JSON: {"identity_canonical": "..."}'},
        ], model=model, role="showrunner", temperature=0.6, max_tokens=1000)
        return (data.get("identity_canonical") or "").strip()
    except Exception:
        return ""


def _backfill_identity(show: Show, char: dict[str, Any], llm, cfg) -> dict[str, str]:
    """LLM-SPLIT an existing appearance_canonical into body + outfit for sheets that
    predate the unbundled fields. Returns a dict of the parts it produced."""
    if llm is None:
        return {}
    name = char.get("name", "") or ""
    canon = (char.get("appearance_canonical") or "").strip()
    if not canon:
        return {}
    model = (cfg.get("llm", "roles", {}).get("showrunner")
             or cfg.get("llm", "model") or "")
    try:
        data = llm.chat_json([{"role": "user",
                               "content": prompts.identity_backfill_prompt(name, canon)}],
                             model=model, role="showrunner", temperature=0.4,
                             max_tokens=800)
        out: dict[str, str] = {}
        if (data.get("identity_canonical") or "").strip():
            out["identity_canonical"] = data["identity_canonical"].strip()
        if (data.get("wardrobe_canonical") or "").strip():
            out["wardrobe_canonical"] = data["wardrobe_canonical"].strip()
        gender = str(data.get("gender") or "").strip().lower()
        if gender in ("female", "male", "nonbinary", "none"):
            out["gender"] = gender
        return out
    except Exception:
        return {}


def _recombine(char: dict[str, Any]) -> None:
    """Rebuild appearance_canonical (the full look) from the unbundled body +
    outfit, so legacy readers (plain refs, H3 shot prompts) never drift."""
    ident = (char.get("identity_canonical") or "").strip().rstrip(".")
    ward = (char.get("wardrobe_canonical") or "").strip().rstrip(".")
    if ident and ward:
        char["appearance_canonical"] = f"{ident}. Wears {ward}."
    elif ident:
        char["appearance_canonical"] = f"{ident}."


def wardrobe(char: dict[str, Any]) -> str:
    """The canonical OUTFIT (garments only). Falls back to the full look when the
    sheet predates the body/outfit split."""
    w = (char.get("wardrobe_canonical") or "").strip()
    return w or (char.get("appearance_canonical") or "").strip()


def _revise_wardrobe(llm, model: str, name: str, current: str, notes: str,
                     bible: dict[str, Any] | None = None, style: str = "") -> str:
    """LLM rewrites ONLY the outfit (wardrobe_canonical) from rejection feedback."""
    world = (f"Series bible: {json.dumps(bible, ensure_ascii=False)}\n" if bible else "")
    try:
        data = llm.chat_json([
            {"role": "user", "content":
             f"Revise this character's DEFAULT OUTFIT description to address the "
             f"director's feedback. Keep everything the feedback did not ask to change.\n"
             f"CHARACTER: {name}\n{world}"
             + (f"Visual style guide: {style}\n" if style else "")
             + f"CURRENT OUTFIT:\n{current or '(none)'}\n"
             f"DIRECTOR'S FEEDBACK:\n{notes}\n"
             + prompts.wardrobe_canonical_rules() + "\n"
             'Reply with ONLY JSON: {"wardrobe_canonical": "..."}'},
        ], model=model, role="showrunner", temperature=0.6, max_tokens=800)
        return (data.get("wardrobe_canonical") or "").strip()
    except Exception:
        return ""


def _backfill_is_person(show: Show, char: dict[str, Any], llm, cfg) -> bool | None:
    """LLM-decide whether the subject is a person, for sheets that lack the field."""
    if llm is None:
        return None
    name = char.get("name", "") or ""
    canon = (char.get("appearance_canonical") or "").strip()
    if not canon:
        return None
    model = (cfg.get("llm", "roles", {}).get("showrunner")
             or cfg.get("llm", "model") or "")
    try:
        data = llm.chat_json([{"role": "user",
                               "content": prompts.is_person_backfill_prompt(name, canon)}],
                             model=model, role="showrunner", temperature=0.0,
                             max_tokens=64)
        v = data.get("is_person")
        return bool(v) if isinstance(v, bool) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The one pipeline
# ---------------------------------------------------------------------------
def _generate_plain_ref(show: Show, cid: str, char: dict[str, Any], name: str,
                        cfg, llm, notes: str) -> bool:
    """Clothed canonical ref only — for supporting / background / non-person
    subjects. No nude identity root, no edit-from-nude."""
    refs_dir = show.character_refs_dir(cid)
    refs_dir.mkdir(parents=True, exist_ok=True)
    # A tweak revises the canonical LOOK (there is no body description here).
    if notes.strip() and llm is not None:
        try:
            from .casting import _revise_appearance
            model = (cfg.get("llm", "roles", {}).get("showrunner")
                     or cfg.get("llm", "model"))
            bible = show.read_bible() or {}
            revised = _revise_appearance(llm, model, name,
                                         (char.get("appearance_canonical") or "").strip(),
                                         notes, bible=bible,
                                         style=(bible.get("style_guide") or "").strip())
            if revised:
                char["appearance_canonical"] = revised
                show.write_character(char)
        except Exception:
            pass
    identity = (char.get("identity_canonical") or "").strip()
    if not identity:
        identity = (char.get("appearance_canonical") or "").strip()
    if not (char.get("gender") or "").strip() and char.get("is_person", True):
        parts = _backfill_identity(show, char, llm, cfg)
        if isinstance(parts, dict) and parts.get("gender"):
            char["gender"] = parts["gender"]
            show.write_character(char)
    prompt = prompts.character_ref_prompt(
        name, identity, (char.get("wardrobe_canonical") or "") if char.get("is_person") else "",
        _style(show), show.ref_medium(), gender=char.get("gender") or "",
        is_person=bool(char.get("is_person", True)))
    out = refs_dir / f"{cid}_ref_01.png"

    def _render() -> bool:
        return _render_costume_via_krea2(show, name, prompt, "base", str(out), cfg,
                                         seed=random.randrange(1, 2**63))

    vmodel = (cfg.get("llm", "roles", {}).get("describer")
              or cfg.get("llm", "model") or "")
    if llm is not None and vmodel:
        from .consistency import render_until_single
        gate = render_until_single(_render, out, llm=llm, model=vmodel, label=f"ref {name}")
        if not gate["ok"]:
            return False
    elif not _render():
        return False
    if not out.exists():
        return False
    prior = load_refs(show, cid)
    variants = dict(prior.get("variants") or {})
    variants["base"] = out.name
    prior.update({"status": "real", "refs": [out.name], "variants": variants})
    prior.setdefault("modes", {})["base"] = "krea2"
    prior.setdefault("prompts", {})["base"] = prompt
    save_refs(show, cid, prior)
    return True


def generate_refs(show: Show, cid: str, char: dict[str, Any] | None = None,
                  cfg=None, llm=None, notes: str = "", force: bool = False,
                  nude_first: bool | None = None) -> bool:
    """Build/refresh a character's refs — the ONLY ref pipeline.

    RECURRING, PERSON characters get the full NUDE-FIRST treatment: nude identity
    root (Krea 2) -> clothed canonical = default outfit ADDED onto the nude (Qwen
    edit) -> register ``base`` + ``nude``. Supporting / background / non-person
    subjects get a plain clothed ref (no nude root); a nude is still generated on
    demand if a shot later declares one. ``nude_first=None`` decides from the
    character.
    """
    cfg = cfg or get_config()
    char = char if char is not None else show.read_character(cid)
    name = char.get("name", "") or cid
    # Decide person-hood from the sheet field; backfill it once for a legacy sheet
    # that predates the field (no keyword guessing).
    if "is_person" not in char:
        val = _backfill_is_person(show, char, llm, cfg)
        if val is not None:
            char["is_person"] = val
            show.write_character(char)
    if nude_first is None:
        nude_first = wants_nude_first(show, cid, char)
    if not nude_first:
        return _generate_plain_ref(show, cid, char, name, cfg, llm, notes)

    # Ensure the unbundled BODY + OUTFIT + gender exist (older sheets predate
    # the fields). Fill only what's MISSING so a user-edited body isn't
    # clobbered; recombine the full look so legacy readers stay consistent.
    if (not (char.get("identity_canonical") or "").strip()
            or not (char.get("wardrobe_canonical") or "").strip()
            or not (char.get("gender") or "").strip()) and not notes.strip():
        parts = _backfill_identity(show, char, llm, cfg)
        if parts:
            changed = False
            if not (char.get("identity_canonical") or "").strip() and parts.get("identity_canonical"):
                char["identity_canonical"] = parts["identity_canonical"]
                changed = True
            if not (char.get("wardrobe_canonical") or "").strip() and parts.get("wardrobe_canonical"):
                char["wardrobe_canonical"] = parts["wardrobe_canonical"]
                changed = True
            if not (char.get("gender") or "").strip() and parts.get("gender"):
                char["gender"] = parts["gender"]
                changed = True
            if changed:
                _recombine(char)
                show.write_character(char)

    # Optional: revise the BODY and/or OUTFIT from director feedback.
    if notes.strip():
        try:
            from .clients.lmstudio import LMStudioClient
            llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
            model = (cfg.get("llm", "roles", {}).get("showrunner")
                     or cfg.get("llm", "model"))
            bible = show.read_bible() or {}
            style = (bible.get("style_guide") or "").strip()
            revised = _revise_identity(llm, model, name, identity_canonical(char),
                                       notes, bible=bible, style=style)
            if revised:
                char["identity_canonical"] = revised
            revised_w = _revise_wardrobe(llm, model, name, wardrobe(char),
                                         notes, bible=bible, style=style)
            if revised_w:
                char["wardrobe_canonical"] = revised_w
            if revised or revised_w:
                _recombine(char)
                show.write_character(char)
        except Exception:
            pass

    # 1. Nude identity root (Krea 2) — the FIRST image (VLM single-figure gated).
    vmodel = (cfg.get("llm", "roles", {}).get("describer")
              or cfg.get("llm", "model") or "")
    nude = ensure_nude_ref(show, cid, name, cfg, force=force, llm=llm, model=vmodel)
    if not nude:
        return False
    # 2. WAIT for the human to approve the nude identity root before spending the
    #    outfit render. An unapproved nude also invalidates any base/costume derived
    #    from a previous nude, so drop those instead of leaving a stale base behind.
    if NUDE_LABEL not in (load_refs(show, cid).get("approved") or []):
        _drop_derived(show, cid)
        return True

    # 2. Canonical (refs[0]) = the default outfit ADDED onto the nude.
    refs_dir = show.character_refs_dir(cid)
    refs_dir.mkdir(parents=True, exist_ok=True)
    out = refs_dir / f"{cid}_ref_01.png"
    outfit = wardrobe(char) or name
    if not _render_costume_via_qwen_edit(show, nude, name,
                                         prompts.costume_edit_prompt(outfit),
                                         "base", str(out), cfg, seed=0):
        return False
    if not out.exists():
        return False

    prior = load_refs(show, cid)
    variants = dict(prior.get("variants") or {})
    if notes.strip():
        # A body change invalidates costumes derived from the old body: drop them
        # (files + entries) so the ref pass regenerates them from the new nude.
        for label in list(variants):
            if label in ("base", NUDE_LABEL):
                continue
            try:
                (refs_dir / variants[label]).unlink()
            except Exception:
                pass
            variants.pop(label, None)
            for k in ("prompts", "seeds", "modes"):
                (prior.get(k) or {}).pop(label, None)
    variants["base"] = out.name
    variants[NUDE_LABEL] = Path(nude).name
    prior.update({"status": "real", "refs": [out.name], "variants": variants})
    prior.setdefault("modes", {})["base"] = "edit"
    prior.setdefault("prompts", {})["base"] = prompts.costume_edit_prompt(outfit)
    save_refs(show, cid, prior)
    return True


# ---------------------------------------------------------------------------
# Resolver — the ONE way to look up a character's refs by name.
# ---------------------------------------------------------------------------
def ensure_ref(show: Show, char: dict[str, Any], cfg=None, llm=None,
               report: Callable[[str], None] | None = None,
               force: bool = False, notes: str = "") -> str:
    """Ensure ONE character has refs — the SINGLE generator used by both the
    bootstrap chain and the startup pass. Returns ``"real"`` when a ref landed
    (a nude awaiting approval counts), else ``"stub"``. Idempotent unless
    ``force`` (reject/regenerate) is set, which re-renders; ``notes`` carries
    director feedback into the revision."""
    import logging
    cfg = cfg or get_config()
    cid = char.get("id", "") or "char"
    rd = show.character_refs_dir(cid)
    rd.mkdir(parents=True, exist_ok=True)
    data = load_refs(show, cid)
    base = (data.get("variants") or {}).get("base") or next(
        iter(data.get("refs") or []), None)
    has_base = bool(base and (rd / base).exists())
    has_nude = bool((data.get("variants") or {}).get(NUDE_LABEL))
    # Nude-first characters are NOT done just because a (legacy) base exists: the
    # nude identity root must exist too. An absent is_person flag is unknown, so
    # attempt the backfill/nude path rather than assuming a plain ref is enough.
    needs_nude = ("is_person" not in char) or wants_nude_first(show, cid, char)
    if not force and data.get("status") == "real" and (
            has_nude or (has_base and not needs_nude)):
        return "real"
    krea2 = cfg.get("env", "comfyui", {}).get("krea2", {}) or {}
    if not krea2.get("url"):
        mark_stub(show, cid, "no krea2 url configured (env.yaml comfyui.krea2.url)")
        return "stub"
    name = char.get("name", "") or "character"
    if report:
        report(f"Generating {name}'s refs — nude root, then outfit…")
    try:
        if not generate_refs(show, cid, char, cfg=cfg, llm=llm, force=force,
                              notes=notes):
            raise RuntimeError("ref generation failed")
        return "real"
    except Exception as exc:
        logging.getLogger(__name__).warning("character ref generation failed: %s", exc)
        # Record WHY it stubbed: the note surfaces in the card's Missing /
        # failed refs row, so the director sees the cause instead of a blank.
        mark_stub(show, cid, f"character ref generation failed: {exc}"[:300])
        return "stub"


def regenerate_nude(show: Show, cid: str, char: dict[str, Any], cfg=None, llm=None,
                    notes: str = "") -> bool:
    """(Re)generate a character's NUDE identity root ONLY — regardless of the
    nude-first classification. Backfills/revises the body + outfit from ``notes``
    first, then force-renders the nude. Used by the explicit "regenerate the nude"
    actions (which must never fall through to a plain base render)."""
    cfg = cfg or get_config()
    name = char.get("name", "") or cid
    model_run = (cfg.get("llm", "roles", {}).get("showrunner")
                 or cfg.get("llm", "model"))
    if notes.strip() and llm is not None:
        try:
            bible = show.read_bible() or {}
            style = (bible.get("style_guide") or "").strip()
            r = _revise_identity(llm, model_run, name, identity_canonical(char),
                                 notes, bible=bible, style=style)
            rw = _revise_wardrobe(llm, model_run, name, wardrobe(char), notes,
                                  bible=bible, style=style)
            if r:
                char["identity_canonical"] = r
            if rw:
                char["wardrobe_canonical"] = rw
            if r or rw:
                _recombine(char)
                show.write_character(char)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "nude revision skipped (%s); rendering from current sheet", exc)
    elif (not (char.get("identity_canonical") or "").strip()
        or not (char.get("wardrobe_canonical") or "").strip()):
        parts = _backfill_identity(show, char, llm, cfg)
        if parts:
            char.update(parts)
            _recombine(char)
            show.write_character(char)
    vmodel = (cfg.get("llm", "roles", {}).get("describer")
              or cfg.get("llm", "model") or "")
    ok = ensure_nude_ref(show, cid, name, cfg, force=True, llm=llm,
                         model=vmodel) is not None
    if ok:
        # The nude changed: any base/costume derived from the OLD nude is stale.
        _drop_derived(show, cid)
    return ok


def regenerate_base(show: Show, cid: str, char: dict[str, Any], cfg=None, llm=None,
                    notes: str = "") -> bool:
    """Regenerate a character's clothed CANONICAL (base), honoring ``notes``.

    Revises the OUTFIT (wardrobe) from ``notes`` and re-renders ``base`` as an
    edit of the approved nude (which is kept) — unless the notes target the
    BODY itself (gender, face, build). A changed body invalidates the old nude
    root, so the nude is re-rolled first and the base re-derived from it.
    Returns False when there is no approved nude to derive from (caller should
    re-roll the nude).
    """
    cfg = cfg or get_config()
    data = load_refs(show, cid)
    if NUDE_LABEL not in (data.get("approved") or []):
        return False
    nude = (data.get("variants") or {}).get(NUDE_LABEL)
    if not nude or not (show.character_refs_dir(cid) / nude).exists():
        return False
    name = char.get("name", "") or cid
    if notes.strip() and llm is not None:
        try:
            model = (cfg.get("llm", "roles", {}).get("showrunner")
                     or cfg.get("llm", "model"))
            bible = show.read_bible() or {}
            style = (bible.get("style_guide") or "").strip()
            current_ident = identity_canonical(char)
            r = _revise_identity(llm, model, name, current_ident, notes,
                                 bible=bible, style=style)
            if r and r.strip() != current_ident.strip():
                # Body changed: the approved nude no longer matches. Re-roll
                # the root (drops stale derived variants + their approvals)
                # and derive the base from the fresh nude below.
                char["identity_canonical"] = r
                _recombine(char)
                show.write_character(char)
                data["approved"] = [a for a in (data.get("approved") or [])
                                    if a != NUDE_LABEL]
                save_refs(show, cid, data)
                vmodel = (cfg.get("llm", "roles", {}).get("describer")
                          or cfg.get("llm", "model") or "")
                fresh = ensure_nude_ref(show, cid, name, cfg, force=True,
                                        llm=llm, model=vmodel)
                if not fresh:
                    return False
                data = load_refs(show, cid)
                nude = (data.get("variants") or {}).get(NUDE_LABEL)
                if not nude:
                    return False
            rw = _revise_wardrobe(llm, model, name, wardrobe(char), notes, bible=bible,
                                  style=style)
            if rw:
                char["wardrobe_canonical"] = rw
                _recombine(char)
                show.write_character(char)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "base revision skipped (%s); rendering from current sheet", exc)
    refs_dir = show.character_refs_dir(cid)
    out = refs_dir / f"{cid}_ref_01.png"
    outfit = wardrobe(char) or name
    prompt = prompts.costume_edit_prompt(outfit)
    if notes.strip() and llm is None:
        prompt = prompt.rstrip() + " Director's revision: " + notes.strip()
    if not _render_costume_via_qwen_edit(show, str(refs_dir / nude), name,
                                         prompt, "base", str(out), cfg, seed=0):
        return False
    data.setdefault("variants", {})["base"] = out.name
    data.setdefault("modes", {})["base"] = "edit"
    data.setdefault("prompts", {})["base"] = prompt
    save_refs(show, cid, data)
    return True


def rebuild_missing(show: Show, cfg=None, llm=None) -> list[str]:
    """Ensure every character has refs — the startup / UI pass. Loops the single
    generator (``ensure_ref``); returns the names that were (re)built."""
    cfg = cfg or get_config()
    done: list[str] = []
    for cid in show.list_characters():
        ch = show.read_character(cid)
        data = load_refs(show, cid)
        base = (data.get("variants") or {}).get("base") or next(
            iter(data.get("refs") or []), None)
        has_base = (data.get("status") == "real" and base
                    and (show.character_refs_dir(cid) / base).exists())
        has_nude = bool((data.get("variants") or {}).get(NUDE_LABEL))
        needs_nude = ("is_person" not in ch) or wants_nude_first(show, cid, ch)
        if has_base and (has_nude or not needs_nude):
            continue  # nude-first characters also need the nude root
        if ensure_ref(show, ch, cfg=cfg, llm=llm) == "real":
            done.append(ch.get("name") or cid)
    return done


def ref_map(show: Show) -> dict[str, dict[str, str]]:
    """name -> {appearance label: path} for every character with refs."""
    out: dict[str, dict[str, str]] = {}
    for cid in show.list_characters():
        c = show.read_character(cid)
        name = c.get("name")
        if not name:
            continue
        rd = show.character_refs_dir(cid)
        data = load_refs(show, cid)
        if data.get("status") != "real":
            continue
        entry: dict[str, str] = {}
        variants = data.get("variants")
        if isinstance(variants, dict):
            for label, r in variants.items():
                if r and (rd / r).exists():
                    entry[label] = str(rd / r)
        if "base" not in entry and data.get("refs"):
            first = data["refs"][0]
            if first and (rd / first).exists():
                entry["base"] = str(rd / first)
        if entry:
            out[name] = entry
    return out
