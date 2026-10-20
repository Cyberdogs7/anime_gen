"""Episode storyboard: shot keyframes + recurring-object reference images.

Renders a krea2 keyframe for every shot of an episode (the storyboard panel)
plus one reference image per recurring object so those objects stay consistent
across shots. Images land in runs/EP##/storyboard/ and runs/EP##/objects/.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from . import fsutil, objects, prompts, refs
from .bootstrap import ACTIVITY
from .config import get_config
from .refs import NUDE_LABEL, ensure_nude_ref, identity_canonical as _identity_canonical
from .show import Show

# Background jobs: show_id -> {"state", "done", "total", "detail"}
STORYBOARD_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _latest_script(show: Show, episode: int) -> tuple[dict[str, Any] | None, Path | None]:
    d = show.dir / "runs" / f"EP{episode:02d}"
    scripts = fsutil.existing_by_mtime(d.glob("script.r*.json"))
    if not scripts:
        return None, None
    return json.loads(scripts[-1].read_text(encoding="utf-8")), scripts[-1]


def _name_in_text(name: str, text: str) -> bool:
    """True when the character is named in the shot's action/camera/beat."""
    toks = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t]
    if not toks:
        return False
    if re.search(r"\b" + re.escape(" ".join(toks)) + r"\b", text):
        return True
    return bool(re.search(r"\b" + re.escape(toks[0]) + r"\b", text))


def _shot_characters(shot: dict[str, Any]) -> list[str]:
    """Names of the characters PHYSICALLY ON SCREEN in this shot.

    The declared cast (``references.characters``) is trusted, but the references
    pass sometimes lists a speaker who is only heard off-screen on a prop/monitor
    INSERT. H3 ref-mode couples <Subject N> to <Picture N> to <Audio N>, so
    attaching a ref RENDERS the character - which is how an insert of a pressure
    dial ends up with Ren standing over it. So: a declared character who speaks
    ONLY off-screen (``on_camera: false``), is not described in the action/camera,
    and is not shown on a display is NOT on screen and gets no ref. Display-only
    (``via_display``) names never get a physical ref.
    """
    refs = shot.get("references", {}) or {}
    declared = list(refs.get("characters", []) or [])
    dlg = shot.get("dialogue", []) or []
    on_cam = {d.get("char") for d in dlg if d.get("char") and d.get("on_camera") is not False}
    off_only = {d.get("char") for d in dlg if d.get("char") and d.get("on_camera") is False}
    via = set(refs.get("via_display") or [])
    text = " ".join(str(shot.get(k) or "") for k in ("action", "camera", "beat")).lower()
    chars: list[str] = []
    for name in declared:
        if name in on_cam:
            chars.append(name)
            continue
        if name in via:
            continue  # visible only through a screen/broadcast, never physically
        if name in off_only and not _name_in_text(name, text):
            continue  # off-screen speaker on an insert - not in frame
        chars.append(name)
    for dlg_entry in dlg:
        c = dlg_entry.get("char")
        if c and c not in chars and c not in via and dlg_entry.get("on_camera") is not False:
            chars.append(c)
    # "Declared" is not "framed": on a shot whose CAMERA names its subjects (a
    # two-shot on Ren and Mira), drop the extras the references pass also listed
    # for the room - background/edging characters get no ref and cannot be
    # rendered. A camera that stages a GROUP keeps its whole declared cast; if the
    # camera names nobody (and isn't a group shot), fall back to the action.
    frame_text = str(shot.get("camera") or "").lower()
    group_shot = bool(re.search(
        r"\b(all (three|four|five|six|seven)|group|ensemble|everyone|crew|team)\b",
        frame_text))
    if not group_shot:
        named = [c for c in chars if _name_in_text(c, frame_text)]
        if not named:
            named = [c for c in chars if _name_in_text(c, text)]
        if named:
            chars = [c for c in chars if c in named or c in on_cam]
    return chars


def _char_ref_map(show: Show) -> dict[str, dict[str, str]]:
    """name -> {"base": path, <costume label>: path, ...} from each character's refs.

    refs.json carries named appearance variants: {"status": "real",
    "variants": {"base": "base.png", "mech frame": "mech.png"}}. Legacy list form
    is tolerated (base = first entry, "mech"-named entries treated as variants).
    """
    out: dict[str, dict[str, str]] = {}
    for cid in show.list_characters():
        c = show.read_character(cid)
        name = c.get("name")
        if not name:
            continue
        rd = show.character_refs_dir(cid)
        prior = refs.load_refs(show, cid)
        if prior.get("status") != "real":
            continue
        variants = prior.get("variants")
        if isinstance(variants, dict):
            entry: dict[str, str] = {}
            for label, r in variants.items():
                p = rd / r
                if p.exists():
                    entry[label] = str(p)
            # Some refs.json files carry only the legacy "refs" list (no
            # variants dict, or an empty one): backfill "base" from it so a
            # character's base reference is never invisible to the keyframe
            # IPAdapter pass.
            if not entry:
                for r in prior.get("refs", []) or []:
                    p = rd / r
                    if p.exists():
                        entry["base"] = str(p)
                        break
            if entry:
                out[name] = entry
                # Alias the normalized unit key so group-count script names
                # ("<unit> Pods (x6)") resolve to the unit's ref.
                from .casting import _unit_key
                out.setdefault(_unit_key(name), entry)
            continue
        # legacy list form
        entry = {}
        for r in prior.get("refs", []) or []:
            p = rd / r
            if not p.exists():
                continue
            label = "base" if "mech" not in r else (r.split("_mech")[0] if "_mech" in r else r)
            entry.setdefault(label, str(p))
        if entry:
            out[name] = entry
    return out


def _shot_refs(show: Show, shot: dict[str, Any]):
    """Names + ref paths for the characters ON SCREEN in this shot.

    On-screen membership comes from the shot's DECLARED cast
    (``references.characters``) plus its speakers - never from scanning the prose.
    A regex scan over action/camera/soundscape drags in off-screen names as
    spurious subjects, which then get a BASE ref and a base-outfit description in
    the render prompt, corrupting shots those characters are not even in.
    """
    names = _shot_characters(shot)
    ref_map = _char_ref_map(show)
    costumes = ((shot.get("references") or {}).get("costumes") or {}) or {}
    from .casting import _unit_key
    refs: list[str] = []
    for n in names:
        entry = ref_map.get(n) or ref_map.get(_unit_key(n))
        if not entry:
            continue
        label = costumes.get(n, "base")
        pick = entry.get(label) or entry.get("base")
        if pick:
            refs.append(pick)
    return names, refs


def _shot_ref_weights(shot: dict[str, Any], names: list[str],
                      n_char_refs: int, n_obj_refs: int, has_prev_kf: bool) -> list[float]:
    """Per-ref IPAdapter dominance weights aligned to the refs list order.

    Order in the refs list is: [character refs..., object refs..., prev keyframe?].
    Weights: the speaker (if on camera) or the first character dominates (~0.95),
    later characters taper, objects pull lightly (~0.55), and the shot-line
    continuity keyframe is weakest (~0.45) so it nudges composition without
    overriding character identity.
    """
    weights: list[float] = []
    speakers = [d.get("char") for d in (shot.get("dialogue") or []) if d.get("on_camera")]
    primary = speakers[0] if speakers else (names[0] if names else "")
    for i, name in enumerate(names):
        if i >= n_char_refs:
            break
        if name == primary:
            weights.append(0.95)
        elif i == 0:
            weights.append(0.9)
        else:
            weights.append(max(0.7, 0.9 - 0.1 * i))
    weights += [0.55] * n_obj_refs
    if has_prev_kf:
        weights.append(0.45)
    return weights


def _show_style(show: Show) -> str:
    """The show's visual style guide (from the bible) for image/video prompts.

    Every ref/keyframe prompt must carry this so the whole series renders in the
    SAME look — without it the model falls back to its own default anime style.
    """
    try:
        return ((show.read_bible() or {}).get("style_guide") or "").strip().rstrip(".")
    except Exception:
        return ""


def _ensure_style(prompt: str, style: str) -> str:
    """Append the show style guide to a prompt unless it already carries it.

    Prompts persisted in refs.json / objects/prompts.json before the style guide
    was threaded through would otherwise keep rendering off-style forever — this
    guarantees the style at render time regardless of what was stored.
    """
    style = (style or "").strip().rstrip(".")
    if not style or style.lower() in (prompt or "").lower():
        return prompt
    return (prompt or "").rstrip(" .") + f" {style}."


def _style_anchor_path(show: Show) -> Path:
    """The show's house-style anchor image, used as an IPAdapter style reference."""
    return show.dir / "style_anchor.png"


def ensure_style_anchor(show: Show, cfg=None) -> Path | None:
    """Generate (once) the show's house-style anchor image, or return the existing one.

    A single key visual that embodies the bible style guide. Every ref render
    then runs an IPAdapter pass against it, so characters, costumes, props and
    scenes converge on one look instead of each picking its own style.
    """
    p = _style_anchor_path(show)
    if p.exists():
        return p
    try:
        from .config import get_config
        from .remote.ops import ServiceOps
        cfg = cfg or get_config()
        style = _show_style(show)
        prompt = ("Anime key visual, the series' house art style"
                  + (f": {style}" if style else "") + ". "
                  "A cinematic wide shot of the show's signature setting with a few "
                  "small original background figures, dramatic lighting, showcasing the "
                  "show's linework, cel shading and color palette, high detail.")
        res = ServiceOps(cfg).generate_image(prompt, seed=0, aspect_ratio="16:9",
                                             out_path=str(p))
        if res.get("ok") and p.exists():
            log.info("style anchor generated: %s", p)
            return p
        log.warning("style anchor generation failed: %s", res.get("detail"))
    except Exception:
        log.warning("style anchor generation failed", exc_info=True)
    return None


def _style_anchor_arg(show: Show) -> str:
    """style_ref path string for a render, or '' when the anchor is off/absent.

    Gated by ``comfy.style_anchor`` because the SDXL ip-adapter has no effect on
    the krea2 model — enabling it only wastes an IPAdapter pass per render.
    """
    from .config import get_config
    if not get_config().get("comfy", "style_anchor", False):
        return ""
    p = _style_anchor_path(show)
    return str(p) if p.exists() else ""


def _keyframe_prompt(shot: dict[str, Any], scene: dict[str, Any], names: list[str],
                     appearances: dict[str, str] | None = None,
                     style_guide: str = "") -> str:
    setting = (scene.get("summary") or scene.get("location") or "").strip()
    action = (shot.get("action") or "").strip()
    camera = (shot.get("camera") or "").strip()
    chars = ", ".join(names) if names else "the characters in this scene"
    # The krea2 turbo model is text-conditioned only (no image-reference
    # identity path locally), so the character's appearance_canonical MUST be
    # in the prompt or every shot renders a generic face/body.
    desc = ""
    if appearances and names:
        parts = []
        for n in names:
            a = (appearances.get(n) or "").strip()
            if a:
                parts.append(f"{n}: {a}")
        if parts:
            desc = "\nCharacter appearance (draw the characters EXACTLY as described):\n" + \
                   "\n".join(parts)
    style = (style_guide or "").strip().rstrip(".")
    # NO "no ..." clauses here: this prompt feeds image_keyframe.json, whose
    # negative conditioning is zeroed (ConditioningZeroOut), so every banned noun
    # becomes a positive token and the model draws exactly what was forbidden —
    # the same trap documented on prompts.character_ref_prompt (a "no duplication"
    # tail made refs render front+back turnarounds). State framing positively.
    return ("Cinematic anime keyframe: "
            f"{action} {camera}. Setting: {setting}. On screen: {chars}.{desc} "
            "Dynamic cinematic composition, consistent series art style"
            + (f" {style}." if style else "")).rstrip(" .") + "."


def _char_appearances(show: Show) -> dict[str, str]:
    """name -> appearance_canonical for every character."""
    out: dict[str, str] = {}
    for cid in show.list_characters():
        c = show.read_character(cid)
        if c.get("name"):
            out[c["name"]] = (c.get("appearance_canonical") or "").replace("\n", " ").strip()
    return out


def _llm_recurring_objects(show: Show, episode: int, script: dict[str, Any],
                           cfg=None, llm=None) -> list[str]:
    """Recurring objects for this episode, pulled from the episode outline.

    Objects are defined in the outline's ``objects`` list during episode
    planning — not extracted from script text or bible plotlines.
    """
    from .planner import read_episode_plan
    plan = read_episode_plan(show, episode)
    names: list[str] = []
    for obj in (plan.get("objects") or []):
        if obj and obj not in names:
            names.append(obj)
    return names[:6]


def _shot_object_refs(show: Show, episode: int, shot: dict[str, Any]) -> list[str]:
    """Reference-image paths for the recurring objects THIS shot actually shows.

    Objects are defined in the episode outline (plan.json ``objects`` list) and
    generated during the ref pass. A shot gets an object's ref only when the
    shot's own text (action/camera/beat) mentions that object — so a prop is not
    injected into shots that don't show it. Only plan-defined objects with a
    generated ref image count; the shot text never invents a new object.
    """
    od = show.dir / "runs" / f"EP{episode:02d}" / "objects"
    if not od.exists():
        return []
    # Collect object names from the plan.
    from .planner import read_episode_plan
    plan = read_episode_plan(show, episode)
    plan_objs = plan.get("objects") or []
    if not plan_objs:
        return []
    text = " ".join(str(shot.get(k, "")) for k in
                    ("action", "camera", "beat", "soundscape", "location")).lower()
    out: list[str] = []
    for name in plan_objs:
        slug = re.sub(r"[^a-z0-9-]+", "-", str(name).lower()).strip("-")
        if not slug:
            continue
        p = od / f"{slug}.png"
        # On screen only when the shot text names it (same plain-substring
        # convention as _object_context) and the ref image exists.
        if p.exists() and str(p) not in out and str(name).lower() in text:
            out.append(str(p))
    return out


def _char_ref_images(show: Show, names: list[str]) -> list[str]:
    """Approved reference-image paths for the given character names.

    Names are matched via their unit key, so a script that says
    "<unit> Pods (x6)" resolves to the ref of the single-unit sheet "<unit> Pod".
    """
    from .casting import _unit_key
    wanted = {_unit_key(n) for n in names}
    out: list[str] = []
    for cid in show.list_characters():
        c = show.read_character(cid)
        if not c.get("name") or _unit_key(c.get("name")) not in wanted:
            continue
        rd = show.character_refs_dir(cid)
        rj = rd / "refs.json"
        if rj.exists():
            try:
                prior = json.loads(rj.read_text(encoding="utf-8"))
            except Exception:
                prior = {}
            if prior.get("status") == "real":
                refs = prior.get("refs", [])
                if refs:
                    p = rd / refs[0]
                    if p.exists():
                        out.append(str(p))
    return out


def _script_costume_pairs(script: dict[str, Any]) -> dict[str, set[str]]:
    """character name -> set of costume labels they actually wear in the script."""
    pairs: dict[str, set[str]] = {}
    for sc in script.get("scenes", []):
        for shot in sc.get("shots", []):
            for name, label in (((shot.get("references") or {}).get("costumes") or {}) or {}).items():
                if label and str(label).strip().lower() != "base":
                    pairs.setdefault(name, set()).add(str(label).strip())
    return pairs


def costume_usage(show: Show, cid: str) -> dict[str, list[str]]:
    """Generated costume-variant label -> episodes that use it, for one character.

    Scans every episode script's ``references.costumes`` and maps each declared
    label to the EP### it appears in. Labels are normalised so equivalent casing/
    spacing collapse onto the same generated variant (best-effort fuzzy match).
    """
    from .planner import read_episode_plan
    import os
    runs = show.dir / "runs"
    usage: dict[str, set[str]] = {}
    if not runs.exists():
        return {k: sorted(v) for k, v in usage.items()}
    for d in sorted(runs.iterdir()):
        if not d.is_dir() or not d.name.startswith("EP"):
            continue
        scripts = fsutil.existing_by_mtime(d.glob("script.r*.json"))
        if not scripts:
            continue
        try:
            script = json.loads(scripts[-1].read_text(encoding="utf-8"))
        except Exception:
            continue
        for sc in script.get("scenes", []):
            for shot in sc.get("shots", []):
                for name, label in (((shot.get("references") or {}).get("costumes") or {}) or {}).items():
                    if not label or str(label).strip().lower() == "base":
                        continue
                    label = str(label).strip()
                    usage.setdefault(label, set()).add(d.name)
    return {k: sorted(v) for k, v in usage.items()}


def costume_variant_payload(show: Show, cid: str, name: str) -> list[dict[str, Any]]:
    """Review data for one character's costume variants.

    Each entry: {label, image (filename), prompt (generation prompt), episodes}.
    ``episodes`` lists every EP### whose script references this variant.
    """
    rd = show.character_refs_dir(cid)
    rj = rd / "refs.json"
    if not rj.exists():
        return []
    try:
        data = json.loads(rj.read_text(encoding="utf-8"))
    except Exception:
        return []
    variants = data.get("variants") or {}
    prompts = data.get("prompts") or {}
    usage = costume_usage(show, cid)
    approved = _approved_costume_labels(show, cid)
    rejected = refs.rejected_labels(show, cid)
    out: list[dict[str, Any]] = []
    for label, f in variants.items():
        if label == "base" or not (rd / f).exists():
            continue
        out.append({
            "label": label,
            "image": f,
            "prompt": prompts.get(label, ""),
            "episodes": usage.get(label, []),
            "approved": label in approved,
            "rejected": label in rejected,
        })
    return out


def ensure_variant_refs(show: Show, script: dict[str, Any], cfg=None,
                        progress=None) -> int:
    """Generate a ref image for each costume variant a character actually wears.

    Generic: every (character, costume-label) pair the script declares gets its own
    reference image, derived from the character's base appearance plus the label.
    ``progress(done, total, label)`` is called per variant when provided.
    """
    from .remote.ops import ServiceOps
    from .comfy_workflows import generate_keyframe, load_workflow
    cfg = cfg or get_config()
    wf_path = cfg.workflows_dir / "image_keyframe.json"
    pairs = _script_costume_pairs(script)
    if not pairs:
        return 0
    ref_map = _char_ref_map(show)
    jobs: list[tuple[str, str, str, str]] = []  # (cid, name, label, base_canon)
    for cid in show.list_characters():
        c = show.read_character(cid)
        name = c.get("name")
        if not name or name not in ref_map:
            continue
        for label in pairs.get(name, ()):
            if label in ref_map[name]:
                continue
            # Hard guard: a transient label (wet/bloody/posed/framing) is the
            # character's existing outfit in a situational state, and a base-
            # outfit label ("Base Armor Suit") is the character's default form —
            # neither is a new costume. Skip them even if a reconcile pass
            # failed to drop them.
            from .planner import is_transient_costume_label, is_base_outfit_label
            if is_transient_costume_label(label) or is_base_outfit_label(label):
                continue
            jobs.append((cid, name, label, (c.get("appearance_canonical") or "")))
    if not jobs:
        return 0
    created = 0
    ops = ServiceOps(cfg)
    client, stop = ops._krea2_client()
    total = len(jobs)
    try:
        for i, (cid, name, label, canon) in enumerate(jobs, start=1):
            if progress:
                progress(i, total, f"costume ref {label} for {name}")
            ACTIVITY[show.show_id] = {"detail": f"Costume ref '{label}' for {name} (Krea 2)…",
                                      "ts": time.time()}
            if generate_costume_variant(show, cid, name, label, canon, cfg=cfg):
                created += 1
    finally:
        if stop:
            stop()
    return created


def _fresh_costume_seed(rd: Path, label: str) -> int:
    """Roll a Qwen-Image-Edit seed guaranteed to differ from the last one used
    for this costume label (read from refs.json), so a regen never repeats."""
    last = None
    rj = rd / "refs.json"
    if rj.exists():
        try:
            last = (json.loads(rj.read_text(encoding="utf-8"))
                    .get("seeds", {}).get(label))
        except Exception:
            last = None
    while True:
        s = random.randrange(1, 2**63)
        if s != last:
            return s


def generate_costume_variant(show: Show, cid: str, name: str, label: str,
                             canon: str = "", cfg=None, prompt: str | None = None,
                             mode: str = "edit", seed: int | None = None) -> bool:
    """Render ONE costume variant ref for a character and register it in refs.json.

    Idempotent: overwrites the existing variant file + refs.json entry (variant
    image + its generation ``prompt``, stored under ``prompts`` for review).
    Returns True when an image landed. Used by the batch pass and the per-variant
    reject.

    The Qwen edit seed is always fresh: when ``seed`` is omitted the last-used
    seed for this label (from refs.json) is looked up and a new random seed
    guaranteed to differ from it is rolled, so a reject/regen never repeats a
    seed. The used seed is persisted under ``seeds`` for review.

    ``mode``:
      - 'edit' (default): Qwen-Image-Edit — base ref image + costume-change prompt,
        identity preserved.
      - 'regen': fresh krea2 text-to-image from the costume label (no identity
        anchor) — a different take for when the edit path isn't what's wanted.
    """
    from .remote.ops import ServiceOps
    cfg = cfg or get_config()
    rd = show.character_refs_dir(cid)
    rd.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    out = rd / f"{cid}_{slug}_01.png"
    # "nude" is an appearance state, not a designed costume: it is the Krea 2
    # identity root (never describe_costume, which would invent a "nude outfit").
    if label.strip().lower() == NUDE_LABEL:
        return ensure_nude_ref(show, cid, name, cfg, force=True) is not None
    # Edit source: the nude identity root when present (garmentless, so nothing
    # carries over), else the clothed base. All refs.json access goes through refs.
    prior = refs.load_refs(show, cid)
    base_file = (prior.get("variants") or {}).get("base")
    base_ref = str(rd / base_file) if base_file and (rd / base_file).exists() else None
    # Nude-first: NO outfit is generated (in ANY mode, and before spending an LLM
    # description) until the nude identity root is approved — every outfit is an
    # edit of the nude.
    variants = prior.get("variants") or {}
    use_nude = refs.wants_nude_first(show, cid) or NUDE_LABEL in variants
    if use_nude and NUDE_LABEL not in (prior.get("approved") or []):
        ACTIVITY.setdefault(show.show_id, {})["detail"] = (
            f"'{label}' for {name}: waiting for nude-ref approval")
        return False
    if not prompt:
        if mode == "edit":
            # Build the edit prompt from an LLM costume DESCRIPTION (what changes
            # from the base ref), not the bare label — the label alone makes every
            # costume come out identical. Driven by personality + the label alone
            # (never a fabricated use case, which biased every outfit into combat
            # gear) and never the character's current look (which poisons it).
            from .bootstrap import BootstrapChain
            personality = ""
            try:
                c = show.read_character(cid)
                pers = c.get("personality") or []
                if isinstance(pers, list):
                    personality = ", ".join(str(x) for x in pers)
                else:
                    personality = str(pers or "")
            except Exception:
                pass
            description = BootstrapChain(show).describe_costume(
                label, name, personality)
            prompt = prompts.costume_edit_prompt(description or label)
        else:
            prompt = prompts.costume_ref_prompt(name, label, canon,
                                                _show_style(show), show.ref_medium())
    if mode == "edit":
        # Source the edit from the garmentless construction ref when available:
        # with no garments in the source, the edit cannot carry over the base
        # outfit's shoes/hats/belts. Falls back to the dressed base.
        # Use the nude as the edit source only when the character is nude-first
        # (recurring person) or already has a nude ref (an explicit nude scene);
        # otherwise edit from their clothed base.
        src = (ensure_nude_ref(show, cid, name, cfg) if use_nude else None) or base_ref
        if seed is None:
            seed = _fresh_costume_seed(rd, label)
        ok = refs._render_costume_via_qwen_edit(show, src, name, prompt, label,
                                                str(out), cfg, seed=seed)
    else:
        if seed is None:
            seed = _fresh_costume_seed(rd, label)
        ok = refs._render_costume_via_krea2(show, name, prompt, label, str(out), cfg,
                                            seed=seed)
    if not ok:
        return False
    prior = refs.load_refs(show, cid) or {"status": "real", "refs": [out.name]}
    variants = prior.setdefault("variants", {})
    if "base" not in variants and prior.get("refs"):
        base = next((r for r in prior["refs"] if "mech" not in r),
                    prior["refs"][0])
        variants["base"] = base
    variants[label] = out.name
    prior.setdefault("prompts", {})[label] = prompt
    prior.setdefault("modes", {})[label] = mode
    if seed is not None:
        prior.setdefault("seeds", {})[label] = seed
    # A freshly generated (or regenerated) variant is NOT approved: it must be
    # human-approved before any video uses it. Drop it from the approved set so
    # a regen after a reject re-opens the gate.
    prior["approved"] = [l for l in (prior.get("approved") or []) if l != label]
    prior["status"] = "real"
    refs.save_refs(show, cid, prior)
    return True


def generate_shot_keyframes(show: Show, episode: int, cfg=None, progress=None,
                            scene_ids: list[str] | None = None,
                            should_stop=None) -> int:
    """Generate a 1-second H3 preview video for every shot.

    The preview runs the SAME ref2va workflow and character refs as the final
    render (`length` snapped to H3's minimum grid ~22 frames \u2248 1s), so frame 0
    \u2014 and the whole clip \u2014 shows exactly how the video will start. Outputs
    `runs/EP##/storyboard/<sid>.mp4`.

    `scene_ids` scopes generation to specific scenes (e.g. ["s01"]).
    """
    from .render import (_render_client, _shot_character_ref, _voice_sample_path,
                         compile_shot_prompt, _shot_ref_paths)
    from .scene_anchor import anchor_path, cached_continuity_brief
    from .h3 import build_h3_ref2va_workflow, run_h3_shot
    from .compile.durations import snap_duration
    from .gpu_manager import ServiceType, get_gpu_manager

    script, _ = _latest_script(show, episode)
    if not script:
        return 0
    cfg = cfg or get_config()
    client = _render_client(cfg)
    scenes = script.get("scenes", [])
    if scene_ids:
        scenes = [sc for sc in scenes if sc.get("id") in set(scene_ids)]
    shots = [s for sc in scenes for s in sc.get("shots", [])]
    total = len(shots)
    done = 0
    h3_cfg = cfg.get("comfy", "h3", {})
    width = int(h3_cfg.get("width", 864) or 864)
    height = int(h3_cfg.get("height", 480) or 480)
    # H3's minimum grid value: 22 frames (~0.9s). Cheap, but the workflow is
    # identical to the real render so the preview matches the video start.
    _k, preview_frames, _secs = snap_duration(1.0)
    preview_frames = 22 if preview_frames > 22 else preview_frames
    # H3 requires --use-sage-attention; krea2/qwen CANNOT run with it. The GPU
    # manager's exclusive COMFYUI lock serializes the H3 instance (8188) against
    # the krea2 instance (8190) so they never contend for the one 16 GB GPU.
    try:
        with get_gpu_manager(cfg).acquire(ServiceType.COMFYUI):
            for sc in scenes:
                for shot in sc.get("shots", []):
                    if should_stop and should_stop():
                        return done  # cooperative pause: stop before the next shot
                    sid = shot.get("id", f"sh{done}")
                    out = show.dir / "runs" / f"EP{episode:02d}" / "storyboard" / f"{sid}.mp4"
                    if out.exists():
                        done += 1
                        if progress:
                            progress(done, total, sid)
                        continue
                    names, _ = _shot_refs(show, shot)
                    costumes = ((shot.get("references") or {}).get("costumes") or {}) or {}
                    # Character refs first (identity anchors), then objects/
                    # keyframe refs \u2014 same ordering the final render uses.
                    image_filenames: list[str] = []
                    for name in names:
                        ref = _shot_character_ref(show, name, costume=costumes.get(name, ""))
                        if not ref:
                            continue
                        try:
                            image_filenames.append(client.upload_image(ref))
                        except Exception:
                            continue
                    # Scene grounding anchor: the SAME set image for every shot of
                    # the scene. On the first shot it is the registry location ref
                    # (if the plan's location resolves); from shot 2 on it is the
                    # last frame of the scene's first preview, so later previews
                    # inherit the established set instead of inventing one.
                    scene_ref_pic: int | None = None
                    try:
                        anchor = anchor_path(show, episode, sc, shot=shot, cfg=cfg)
                        if anchor:
                            fname = client.upload_image(str(anchor))
                            if fname not in image_filenames:
                                scene_ref_pic = len(image_filenames) + 1
                                image_filenames.append(fname)
                    except Exception:
                        scene_ref_pic = None
                    for p in _shot_ref_paths(show, episode, sc, shot):
                        try:
                            fname = client.upload_image(p)
                            if fname not in image_filenames:
                                image_filenames.append(fname)
                        except Exception:
                            pass
                    audio_filenames: list[str] = []
                    audio_refs: list[dict[str, str]] = []
                    if image_filenames:
                        speakers = [d.get("char") for d in (shot.get("dialogue") or [])
                                    if d.get("line")]
                        for name in dict.fromkeys(speakers):
                            if not name or len(audio_filenames) >= 3:
                                break
                            vp = _voice_sample_path(show, name)
                            # Off-screen speakers still get their VOICE ref (only
                            # their PICTURE is withheld) - so do not require the
                            # name to be a visual subject here.
                            if not vp:
                                continue
                            try:
                                audio_filenames.append(client.upload_audio(vp))
                            except Exception:
                                continue
                            audio_refs.append({"char": name,
                                               "token": f"<Audio {len(audio_refs) + 1}>"})
                    prompt = compile_shot_prompt(show, script, sc, shot, names,
                                                 audio_refs=audio_refs or None,
                                                 scene_ref_pic=scene_ref_pic,
                                                 scene_ref_desc=cached_continuity_brief(show, episode, sc, shot=shot))
                    wf = build_h3_ref2va_workflow(
                        prompt, 1.0, 0, cfg=cfg,
                        ref_image_filenames=image_filenames or None,
                        ref_audio_filenames=audio_filenames or None,
                        width=width, height=height,
                        steps=int(h3_cfg.get("steps", 8) or 8),
                        sampler_name=h3_cfg.get("sampler") or "res_multistep",
                        scheduler=h3_cfg.get("scheduler") or "simple",
                        use_spectrum=bool(h3_cfg.get("spectrum", False)),
                        use_first_block_cache=bool(h3_cfg.get("first_block_cache", False)),
                    )
                    # Force the 1s preview length regardless of the shot duration.
                    for nid, node in wf.items():
                        if node.get("class_type") == "MiniMaxH3ReferenceToVideo" and \
                           isinstance(node.get("inputs"), dict):
                            node["inputs"]["length"] = preview_frames
                    if progress:
                        progress(done, total, sid)
                    try:
                        run_h3_shot(client, wf, out)
                    except Exception as exc:
                        ACTIVITY.setdefault(show.show_id, {})["detail"] = (
                            f"Storyboard preview {sid} failed: {exc}")
                    finally:
                        try:
                            client.free_memory()
                        except Exception:
                            pass
                    done += 1
                    if progress:
                        progress(done, total, sid)
    finally:
        try:
            client.free_memory()
        except Exception:
            pass
    return done


def generate_object_refs(show: Show, episode: int, cfg=None, progress=None,
                         objs: list[str] | None = None) -> int:
    """One reference image per recurring object in the episode.

    ``objs`` may be precomputed by the caller (LLM extraction, run before the
    krea2 GPU batch so the showrunner model is still loaded). When omitted, the
    LLM extraction runs here — never a regex.
    """
    from .remote import ServiceOps
    script, _ = _latest_script(show, episode)
    if not script:
        return 0
    objs = objs if objs is not None else _llm_recurring_objects(show, episode, script, cfg=cfg)
    for i, name in enumerate(objs, start=1):
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
        if not slug:
            continue
        out = show.dir / "runs" / f"EP{episode:02d}" / "objects" / f"{slug}.png"
        if out.exists():
            continue
        # Reuse a previously LLM-revised prompt so a regenerated object keeps the
        # accumulated corrections rather than resetting to the base template.
        stored = _object_prompts(show, episode).get(slug)
        if stored and not _is_stale_object_prompt(stored):
            prompt = _ensure_style(stored, _show_style(show))
        else:
            desc = _describe_object(show, episode, name, cfg)
            prompt = _base_object_prompt(name, _show_style(show), desc, show.ref_medium())
        try:
            ServiceOps(cfg).generate_image(prompt, seed=0, aspect_ratio="1:1",
                                           out_path=str(out),
                                           style_ref=_style_anchor_arg(show))
            _save_object_prompt(show, episode, slug, prompt)
            # A freshly (re)generated object image needs re-approval + re-review —
            # otherwise a stale approvals.json / vcheck pass would clear the gate
            # for an image the human never saw.
            unapprove_object(show, episode, slug)
            from .vcheck import clear_object_vcheck
            clear_object_vcheck(show, episode, slug)
        except Exception as exc:
            ACTIVITY.setdefault(show.show_id, {})["detail"] = f"Object ref {name} failed: {exc}"
        if progress:
            progress(i, len(objs), name)
    return len(objs)


def regenerate_object_ref(show: Show, episode: int, slug: str, notes: str = "",
                          cfg=None, llm=None) -> bool:
    """Feedback-driven regeneration of ONE recurring-object ref.

    Works like the costume/char-ref feedback loop: the current generation prompt
    (the last LLM-revised one, or the base template on first iteration) is
    rewritten by the showrunner from the rejection notes, the revised prompt is
    persisted so further rejects keep refining it, then the object is re-rendered.
    Returns True when an image landed at runs/EP##/objects/<slug>.png.
    """
    from .remote import ServiceOps
    from .clients.lmstudio import LMStudioClient
    cfg = cfg or get_config()
    od = _objects_dir(show, episode)
    od.mkdir(parents=True, exist_ok=True)
    out = od / f"{slug}.png"
    if out.exists():
        try:
            out.unlink()
        except Exception:
            pass
    name = slug.replace("-", " ").title()
    style = _show_style(show)
    stored = _object_prompts(show, episode).get(slug)
    if stored and not _is_stale_object_prompt(stored):
        current = _ensure_style(stored, style)
    else:
        desc = _describe_object(show, episode, name, cfg)
        current = _base_object_prompt(name, style, desc, show.ref_medium())
    prompt = current
    if notes.strip():
        from . import prompts
        model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
        try:
            llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
            text = llm.chat([
                {"role": "system", "content": prompts.showrunner_system(
                    show.style_profile(cfg), (show.read_bible() or {}).get("content_policy", "mature"),
                    show.style_profile(cfg).get("baseline", ""))},
                {"role": "user", "content": prompts.revise_object_prompt(current, notes)},
            ], model=model, role="showrunner", temperature=0.4, max_tokens=900)
            revised = text[text.find("{"): text.rfind("}") + 1] or ""
            import json as _json
            parsed = _json.loads(revised)
            candidate = (parsed.get("prompt") or "").strip()
            if candidate:
                prompt = _ensure_style(candidate, style)
        except Exception:
            pass  # keep the current prompt; still regenerate from it directly
    try:
        ServiceOps(cfg).generate_image(prompt, seed=0, aspect_ratio="1:1",
                                       out_path=str(out),
                                       style_ref=_style_anchor_arg(show))
        _save_object_prompt(show, episode, slug, prompt)
        # A regenerated object image needs re-approval: drop it from the approved
        # set and clear the VL check so it is re-reviewed.
        unapprove_object(show, episode, slug)
        from .vcheck import clear_object_vcheck
        clear_object_vcheck(show, episode, slug)
    except Exception as exc:
        ACTIVITY.setdefault(show.show_id, {})["detail"] = f"Object ref {name} failed: {exc}"
        return False
    return out.exists()


def _pending_ref_names(show: Show) -> list[str]:
    """Character names whose base reference image is not yet approved.

    Covers both bootstrap characters (approval stored in bootstrap.json; ``refs``
    != "approved") and episode-supporting characters (enemy/group units that
    never get a bootstrap roster entry — their approval lives in refs.json). The
    storyboard previews must wait for these: a preview rendered without the
    approved ref would show the wrong character.
    """
    st = show.bootstrap_state()
    cid_by_name: dict[str, str] = {}
    for cid in show.list_characters():
        c = show.read_character(cid)
        if c.get("name"):
            cid_by_name[c["name"]] = cid
    out: list[str] = []
    in_bootstrap: set[str] = set()
    for ch in st.get("characters", []):
        name = ch.get("name", "?")
        in_bootstrap.add(name)
        if (ch.get("refs") or "").strip() == "approved":
            continue
        # Approval is recorded in refs.json (base/nude/variants) — accept that, so a
        # bootstrap entry left at "pending"/"" doesn't nag after the refs are approved.
        cid = cid_by_name.get(name)
        if cid and (is_base_ref_approved(show, cid)
                    or refs.load_refs(show, cid).get("approved")):
            continue
        out.append(name)
    # Episode-supporting characters that DO have a generated base ref must be
    # gated too — otherwise H3 renders with an identity anchor the human never
    # signed off on (the base-ref approval is recorded in refs.json for these).
    ref_map = _char_ref_map(show)
    for cid in show.list_characters():
        c = show.read_character(cid)
        name = c.get("name")
        if not name or name in in_bootstrap or name not in ref_map:
            continue
        if is_base_ref_approved(show, cid):
            continue
        out.append(name)
    return out


# ---------------------------------------------------------------------------
# Costume-variant + recurring-object ref approval (gates before videos).
#
# A costume variant (refs.json "variants") and a recurring-object ref
# (runs/EP##/objects/<slug>.png) are both *asset gates*: they are generated
# during the storyboard ref pass but MUST be approved by the human before any
# video (storyboard preview or final render) uses them. The shot-writer invents
# labels per shot and object heuristics are noisy, so an unapproved ref is not
# a reliable identity anchor — gating the videos on approval is what keeps
# H3's ref2va from being steered by a ref the human never signed off on.
#
# Approval state:
#   costumes  -> characters/<id>/refs/refs.json["approved"]  (list of labels)
#   objects   -> runs/EP##/objects/approvals.json            {"approved": [slug]}
# ---------------------------------------------------------------------------


def _objects_dir(show: Show, episode: int) -> Path:
    return objects.objects_dir(show, episode)


def _approved_costume_labels(show: Show, cid: str) -> set[str]:
    """Costume-variant labels already approved for one character."""
    return set(refs.load_refs(show, cid).get("approved") or [])


def mark_base_ref_approved(show: Show, cid: str) -> bool:
    """Record a character's BASE reference as approved in refs.json.

    The base ref is normally approved through the Gate-0 character/refs gate
    (bootstrap.json ``characters[].refs``). Episode-supporting characters
    (enemy/group units) never get a bootstrap roster entry, so their base
    approval is recorded here — the same ``approved`` list costume variants use.
    No-op (``False``) when the character has no refs.json.
    """
    data = refs.load_refs(show, cid)
    if not data:
        return False
    approved = set(data.get("approved") or [])
    approved.add("base")
    data["approved"] = sorted(approved)
    rej = dict(data.get("rejected") or {})
    rej.pop("base", None)
    if rej:
        data["rejected"] = rej
    else:
        data.pop("rejected", None)
    refs.save_refs(show, cid, data)
    return True


def unapprove_base_ref(show: Show, cid: str) -> None:
    """Drop a character's base ref from the approved list (after regen/reject)."""
    data = refs.load_refs(show, cid)
    if not data:
        return
    data["approved"] = [a for a in (data.get("approved") or []) if a != "base"]
    refs.save_refs(show, cid, data)


def is_base_ref_approved(show: Show, cid: str) -> bool:
    """True when a character's BASE reference is approved.

    The decision can live in either store and they must never disagree:
      * ``bootstrap.json`` ``characters[].refs == "approved"`` (the Gate-0 gate,
        written by the character/refs approval), and
      * ``refs.json["approved"]`` containing ``"base"`` (episode-supporting
        characters that have no bootstrap roster entry).
    """
    if "base" in _approved_costume_labels(show, cid):
        return True
    name = show.read_character(cid).get("name", "")
    if not name:
        return False
    for ch in show.bootstrap_state().get("characters", []):
        if ch.get("name") == name:
            return (ch.get("refs") or "").strip() == "approved"
    return False


def _approved_object_slugs(show: Show, episode: int) -> set[str]:
    return objects.approved_slugs(show, episode)


def _object_prompts(show: Show, episode: int) -> dict[str, str]:
    """Persisted generation prompt per recurring object slug, across iterations.

    Mirrors how costume variants store their prompt in refs.json: the feedback
    loop keeps the last (LLM-revised) prompt so successive rejects FURTHER refine
    it instead of starting over from the base template every time.
    """
    return objects.prompts(show, episode)


def _save_object_prompt(show: Show, episode: int, slug: str, prompt: str) -> None:
    objects.save_prompt(show, episode, slug, prompt)


def _is_stale_object_prompt(prompt: str) -> bool:
    """True for the old negation-based object prompt (it renders people/hands).

    The Krea 2 turbo workflow zeroes its negative conditioning, so a "NO PEOPLE /
    NO HANDS" clause is a POSITIVE trigger. Prompts written that way must be
    rebuilt, not reused.
    """
    low = (prompt or "").lower()
    return "no people" in low or "no hands" in low or "no figures" in low


def _object_context(show: Show, episode: int, name: str) -> str:
    """Story context for a recurring object: scenes that mention it + the plot.

    The plot/synopsis is always included so the describer can tell WHAT the
    object is (its kind and scale) instead of guessing from a terse name.
    """
    from .planner import read_episode_plan
    plan = read_episode_plan(show, episode) or {}
    low = (name or "").lower()
    bits: list[str] = []
    for sc in (plan.get("scenes") or []):
        text = " ".join([str(sc.get("summary", ""))]
                        + [str(b) for b in (sc.get("beats") or [])])
        if low and low in text.lower():
            s = str(sc.get("summary", "")).strip()
            if s:
                bits.append(s)
    scene_ctx = " | ".join(bits[:3])
    story = " ".join(str(plan.get(k) or "")
                     for k in ("plot", "synopsis")).strip()[:700]
    parts = [p for p in (scene_ctx, story) if p]
    return " || ".join(parts)


def _describe_object(show: Show, episode: int, name: str, cfg=None) -> str:
    """LLM visual description of a recurring object (best effort; '' on error)."""
    try:
        from .bootstrap import BootstrapChain
        ctx = _object_context(show, episode, name)
        return BootstrapChain(show, cfg=cfg).describe_object(name, ctx)
    except Exception:
        log.warning("object description failed for %s", name, exc_info=True)
        return ""


def _base_object_prompt(name: str, style_guide: str = "",
                        description: str = "", medium: str = "") -> str:
    """Positive-only Krea 2 prompt for ONE recurring object.

    The krea2 turbo workflow has NO negation handling (its negative conditioning
    is zeroed out), so any "no people / no hands / no text" clause acts as a
    POSITIVE trigger and the model draws exactly that. Single-subject isolation
    is therefore expressed only as positive facts, the same discipline as
    ``prompts.character_ref_prompt``.
    """
    desc = (description or "").strip().rstrip(".")
    lead = f"A single prop, {name}"
    if desc:
        lead = f"{lead}: {desc}"
    return (
        f"{lead}. The prop is the sole subject, centered and fully in frame, "
        "alone on a seamless plain studio backdrop. "
        + prompts.ref_style_clause(style_guide, medium))


def mark_costume_approved(show: Show, cid: str, label: str) -> None:
    """Record one costume variant as approved (refs.json). Idempotent."""
    data = refs.load_refs(show, cid)
    if not data:
        raise ValueError(f"character {cid} has no refs.json")
    approved = set(data.get("approved") or [])
    approved.add(label)
    data["approved"] = sorted(approved)
    rej = dict(data.get("rejected") or {})
    rej.pop(label, None)
    if rej:
        data["rejected"] = rej
    else:
        data.pop("rejected", None)
    refs.save_refs(show, cid, data)


def delete_costume(show: Show, cid: str, label: str) -> None:
    """Delete one costume variant: drop its ref image + registry entries.

    Removes the variant from refs.json (variants/prompts/approved), deletes the
    rendered ref PNG, and re-renders nothing. The base outfit is protected — you
    cannot delete a character's canonical base look. Idempotent: deleting a
    variant that doesn't exist is a no-op.
    """
    rd = show.character_refs_dir(cid)
    data = refs.load_refs(show, cid)
    if not data:
        return
    variants = data.get("variants") or {}
    if label == "base" or label not in variants:
        if label == "base":
            raise ValueError("cannot delete a character's base outfit")
        return
    img = variants.pop(label, None)
    (data.get("prompts") or {}).pop(label, None)
    data["approved"] = [a for a in (data.get("approved") or []) if a != label]
    refs.save_refs(show, cid, data)
    if img:
        f = rd / img
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass


def mark_object_approved(show: Show, episode: int, slug: str) -> None:
    """Record one recurring-object ref as approved. Idempotent."""
    objects.approve(show, episode, slug)


def unapprove_object(show: Show, episode: int, slug: str) -> None:
    """Remove an object ref from the approved set (after a regen/reject)."""
    objects.unapprove(show, episode, slug)


def _pending_costume_refs(show: Show, script: dict[str, Any]) -> list[str]:
    """Costume-variant labels the episode actually wears that aren't approved.

    Mirrors the same (character, label) pairs ``ensure_variant_refs`` would
    generate — but only for variants that already have a generated ref file,
    so we never block on a label that simply hasn't been generated yet (the
    ref pass generates it first, then this gate applies).
    """
    ref_map = _char_ref_map(show)
    pairs = _script_costume_pairs(script)
    pending: list[str] = []
    for cid in show.list_characters():
        c = show.read_character(cid)
        name = c.get("name")
        if not name or name not in pairs:
            continue
        approved = _approved_costume_labels(show, cid)
        for label in sorted(pairs.get(name, ())):
            # base is the character's canonical form — approved with the ref bank.
            if label.lower() == "base":
                continue
            if label in approved:
                continue
            # only a label with a real generated ref file is gateable
            entry = ref_map.get(name, {})
            if label in entry:
                pending.append(f"{name}: {label}")
    return pending


def _pending_object_refs(show: Show, episode: int) -> list[str]:
    """Recurring-object refs (runs/EP##/objects/*.png) that exist but aren't approved."""
    od = _objects_dir(show, episode)
    if not od.exists():
        return []
    approved = _approved_object_slugs(show, episode)
    pending: list[str] = []
    for p in sorted(od.glob("*.png")):
        if p.stem not in approved:
            pending.append(p.stem.replace("-", " ").title())
    return pending


def pending_ref_approvals(show: Show, episode: int,
                          script: dict[str, Any] | None = None) -> list[str]:
    """Human-readable list of every ref the videos must wait on: character base
    refs, costume variants this episode wears, and this episode's object refs."""
    pending: list[str] = []
    for name in _pending_ref_names(show):
        pending.append(f"character ref: {name}")
    if script is None:
        script, _ = _latest_script(show, episode)
    if script:
        for item in _pending_costume_refs(show, script):
            pending.append(f"costume: {item}")
    for item in _pending_object_refs(show, episode):
        pending.append(f"object: {item}")
    return pending


def auto_approve_refs(show: Show, episode: int,
                      script: dict[str, Any] | None = None, cfg=None) -> None:
    """Approve every pending costume/object ref when the approval config is in
    auto mode (master switch or per-gate ``auto``), so a hands-free run never
    stalls on these asset gates. Character base refs are approved by the
    bootstrap chain, not here."""
    cfg = cfg or get_config()
    master = bool(cfg.get("approval", "global", {}).get("auto_approve", False))
    if not (master or cfg.get("approval", "gates", {}).get("costume") == "auto"):
        return
    if script is None:
        script, _ = _latest_script(show, episode)
    if script:
        ref_map = _char_ref_map(show)
        pairs = _script_costume_pairs(script)
        for cid in show.list_characters():
            c = show.read_character(cid)
            name = c.get("name")
            if not name or name not in pairs:
                continue
            approved = _approved_costume_labels(show, cid)
            for label in pairs.get(name, ()):
                if label.lower() == "base" or label in approved:
                    continue
                if label in ref_map.get(name, {}):
                    try:
                        mark_costume_approved(show, cid, label)
                    except Exception:
                        pass
    # Episode-supporting base refs (no bootstrap roster entry) are gated by
    # _pending_ref_names but approved by neither the bootstrap chain nor the
    # costume loop — approve them here in auto mode so a hands-free run does not
    # stall on the base-ref gate.
    if master or cfg.get("approval", "gates", {}).get("character") == "auto":
        bootstrap_names = {ch.get("name") for ch in show.bootstrap_state().get("characters", [])}
        for cid in show.list_characters():
            c = show.read_character(cid)
            name = c.get("name")
            if not name or name in bootstrap_names:
                continue
            try:
                mark_base_ref_approved(show, cid)
            except Exception:
                pass
    # Objects: master switch OR the object gate is auto.
    if master or cfg.get("approval", "gates", {}).get("object") == "auto":
        for slug in _pending_object_slugs(show, episode):
            try:
                mark_object_approved(show, episode, slug)
            except Exception:
                pass


def _pending_object_slugs(show: Show, episode: int) -> list[str]:
    od = _objects_dir(show, episode)
    if not od.exists():
        return []
    approved = _approved_object_slugs(show, episode)
    return [p.stem for p in sorted(od.glob("*.png")) if p.stem not in approved]


def storyboard_blockers(show: Show, episode: int) -> list[str]:
    """Reasons a storyboard/render build must not run yet (empty = clear to build).

    A build renders expensive H3 clips, so it may only run once the episode
    OUTLINE and the SCENE DETAILS are both approved AND a script exists. Without
    this guard a manual or leftover trigger happily renders against an unapproved
    (or stale, plan-less) outline.
    """
    from .planner import read_episode_plan, read_scene_details
    blockers: list[str] = []
    if (read_episode_plan(show, episode).get("status") or "") != "approved":
        blockers.append("episode outline not approved")
    if (read_scene_details(show, episode).get("status") or "") != "approved":
        blockers.append("scene details not approved")
    script, _ = _latest_script(show, episode)
    if not script:
        blockers.append("no script")
    else:
        # The writers'-room review gate is enforced by the reconciler; the manual
        # storyboard/render path must honour it too, else a rejected script can be
        # turned into finished video.
        review = script.get("_review") or {}
        if review.get("passed") is False:
            blockers.append("script failed review (approve as override or revise it)")
    return blockers


def build_storyboard(show: Show, episode: int, cfg=None) -> None:
    """Generate shot keyframes + object refs in a background thread with progress."""
    blockers = storyboard_blockers(show, episode)
    if blockers:
        STORYBOARD_JOBS[show.show_id] = {
            "state": "blocked", "done": 0, "total": 0, "ts": time.time(),
            "detail": "Cannot build storyboard: " + "; ".join(blockers)}
        log.info("storyboard blocked for %s EP%02d: %s",
                 show.show_id, episode, "; ".join(blockers))
        return
    # Re-entrancy guard: the self-healing reconciler and the dashboard's manual
    # flows (approve scenes / regenerate shots) both call build_storyboard. Two
    # threads sharing the single STORYBOARD_JOBS dict clobber each other's
    # progress AND both contend for the exclusive GPU lock — the second one
    # busy-waits forever. If a live worker already owns this show's job, skip.
    existing = STORYBOARD_JOBS.get(show.show_id)
    if existing:
        thread = existing.get("_thread")
        if thread and thread.is_alive() \
                and existing.get("state") in ("running", "waiting"):
            log.info("storyboard already running for %s — skipping duplicate build",
                     show.show_id)
            return
    # Track the worker thread so a crashed thread can't leave a stale "running"
    # job behind that blocks the reconciler forever.
    STORYBOARD_JOBS[show.show_id] = {"state": "running", "done": 0, "total": 0,
                                     "detail": "Starting…", "ts": time.time()}

    def _run():
        job = STORYBOARD_JOBS.get(show.show_id) or {}
        job["state"] = "running"
        job["detail"] = "Preparing storyboard…"
        job["ts"] = time.time()

        def prog(done, total, label):
            job["done"], job["total"] = done, total
            if "keyframe" in str(label).lower() or "preview" in str(label).lower() \
                    or total == job.get("shot_total"):
                job["detail"] = f"Generating storyboard previews {done}/{total}: {label}"
            else:
                job["detail"] = f"Reference images {done}/{total}: {label}"
            job["ts"] = time.time()
            ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}

        try:
            from .remote.ops import ServiceOps
            # Phase 1: krea2 reference passes (character/costume/object refs).
            # The costume-label reconcile AND the recurring-object pick are PURE
            # LLM and must run BEFORE the krea2 GPU batch: taking the exclusive
            # COMFYUI lock evicts the LLM from VRAM, and calling the LLM while
            # holding it self-deadlocks the GPU manager (a nested acquire waits
            # on its own hold).
            script, _ = _latest_script(show, episode)
            objs: list[str] = []
            if script:
                from .planner import reconcile_costume_labels
                script = reconcile_costume_labels(show, episode, script, cfg=cfg)
                objs = _llm_recurring_objects(show, episode, script, cfg=cfg)
            with ServiceOps(cfg).krea2_batch():
                from .casting import create_missing_character_refs
                created = create_missing_character_refs(show, episode, cfg=cfg)
                if created:
                    job["detail"] = f"Ref pass: new characters {', '.join(created)}"
                    job["ts"] = time.time()
                    ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}
                # Location refs for every environment the outline/shots introduced
                # (new places AND per-shot sets like a cockpit or the apron). This
                # is the correct GPU phase for them: the krea2 batch already holds
                # the COMFYUI lock, which has evicted the local LLM — generating
                # them any earlier (while the LLM is resident) would OOM it.
                from .scenes import generate_missing_refs
                scn = generate_missing_refs(show, cfg=cfg)
                if scn:
                    job["detail"] = f"Location refs: {scn} environments"
                    job["ts"] = time.time()
                    ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}
                vn = ensure_variant_refs(show, script or {}, cfg=cfg, progress=prog)
                if vn:
                    job["detail"] = f"Costume refs: {vn} variants"
                    job["ts"] = time.time()
                    ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}
                objn = generate_object_refs(show, episode, cfg=cfg, progress=prog, objs=objs)
                if objn:
                    job["detail"] = f"Object refs: {objn} recurring props"
                    job["ts"] = time.time()
                    ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}
                # Object VL check: verify no humans / text in generated object
                # images. Runs on CPU (VLM via LM Studio) inside the krea2
                # batch context so the GPU is available if the VLM needs it.
                # Fails auto-regenerate the object with prompt refinement.
                if objn:
                    from .vcheck import run_object_vcheck_batch
                    def _obj_vprog(d, t, label):
                        job["detail"] = f"Object VL check {d}/{t}: {label}"
                        job["ts"] = time.time()
                        ACTIVITY[show.show_id] = {"detail": job["detail"],
                                                   "ts": time.time()}
                    try:
                        vl_actions = run_object_vcheck_batch(
                            show, episode, cfg=cfg, on_progress=_obj_vprog)
                        if vl_actions:
                            job["detail"] = f"Object VL check: {len(vl_actions)} issues"
                            job["ts"] = time.time()
                    except Exception as exc:
                        log.warning("[storyboard] object vcheck failed: %s", exc,
                                    exc_info=True)
            total_shots = len([s for sc in (script or {}).get("scenes", [])
                               for s in sc.get("shots", [])])
            job["total"] = total_shots
            # Gate the preview phase on EVERY ref the videos use: character
            # base refs, this episode's costume variants, and its recurring
            # objects. A preview/render steered by an unapproved costume or
            # object ref would bake the wrong identity into the clip, so the
            # job parks in "waiting" until the human approves them. The
            # reconciler re-kicks this job once the refs gate is cleared.
            # In auto mode the refs are approved here instead, so nothing waits.
            auto_approve_refs(show, episode, script or {}, cfg=cfg)
            pending = pending_ref_approvals(show, episode, script or {})
            if pending:
                job["state"] = "waiting"
                job["detail"] = ("Waiting for ref approval: "
                                 + ", ".join(pending))
                job["ts"] = time.time()
                ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}
                return
            if storyboard_status(show.show_id).get("state") == "stopped":
                return  # cooperative pause between the ref phase and the previews
            # Phase 2: 1-second H3 preview per shot (same workflow/refs as the
            # final render, so the preview shows exactly how the video starts).
            generate_shot_keyframes(
                show, episode, cfg=cfg, progress=prog,
                should_stop=lambda: storyboard_status(show.show_id).get("state") == "stopped")
            job["detail"] = "Consistency review (vision)…"
            job["ts"] = time.time()
            ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}
            from .consistency import run_consistency_check
            # Keep the job's progress stamp fresh during the (potentially long)
            # vision review so the reconciler's stale-job watchdog can't mistake
            # a legitimate review for a hung worker.
            job["report"] = run_consistency_check(
                show, episode, cfg=cfg,
                on_progress=lambda: job.__setitem__("ts", time.time()))
            # AUTO preview visual check: the same VLM as the post-render check,
            # run on the freshly-generated 1s previews so a wrong character or
            # action is flagged BEFORE the expensive full render. Never
            # auto-rejects — it stores a verdict the dashboard surfaces. Keeps
            # the job's ts fresh so the reconciler's stale-job watchdog can't
            # mistake this legitimate vision pass for a hung worker.
            try:
                pv_dir = show.dir / "runs" / f"EP{episode:02d}" / "storyboard"
                if (storyboard_status(show.show_id).get("state") != "stopped"
                        and pv_dir.exists() and any(pv_dir.glob("*.mp4"))):
                    job["detail"] = "Preview visual check (vision)…"
                    job["ts"] = time.time()
                    ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}
                    from .vcheck import run_preview_vcheck
                    run_preview_vcheck(
                        show, episode, cfg=cfg,
                        on_progress=lambda d, t, label: job.__setitem__(
                            "ts", time.time()))
            except Exception as exc:
                log.warning("[storyboard] preview vcheck failed: %s", exc,
                            exc_info=True)
            job["state"] = "done"
            job["detail"] = "Storyboard + consistency complete"
        except Exception as exc:
            job["state"] = "failed"
            job["detail"] = f"Storyboard failed: {exc}"
            log.warning("storyboard failed for %s EP%02d: %s",
                        show.show_id, episode, exc, exc_info=True)
        finally:
            job["ts"] = time.time()
            ACTIVITY.pop(show.show_id, None)

    t = threading.Thread(target=_run, daemon=True, name=f"storyboard-{show.show_id}")
    t.start()
    STORYBOARD_JOBS[show.show_id]["_thread"] = t


def stop_storyboard(show_id: str) -> None:
    """Mark the storyboard job as stopped so the reconciler won't re-kick it."""
    job = STORYBOARD_JOBS.get(show_id)
    if job:
        job["state"] = "stopped"
        job["detail"] = "Stopped by user"


def storyboard_status(show_id: str) -> dict[str, Any]:
    job = STORYBOARD_JOBS.get(show_id)
    if not job:
        return {"state": "idle", "done": 0, "total": 0, "detail": ""}
    out = dict(job)
    thread = out.pop("_thread", None)
    out["alive"] = bool(thread and thread.is_alive())
    return out
