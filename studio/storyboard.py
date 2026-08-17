"""Episode storyboard: shot keyframes + recurring-object reference images.

Renders a krea2 keyframe for every shot of an episode (the storyboard panel)
plus one reference image per recurring object so those objects stay consistent
across shots. Images land in runs/EP##/storyboard/ and runs/EP##/objects/.
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from .bootstrap import ACTIVITY
from .config import get_config
from .show import Show

# Background jobs: show_id -> {"state", "done", "total", "detail"}
STORYBOARD_JOBS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _latest_script(show: Show, episode: int) -> tuple[dict[str, Any] | None, Path | None]:
    d = show.dir / "runs" / f"EP{episode:02d}"
    scripts = sorted(d.glob("script.r*.json"), key=lambda p: p.stat().st_mtime)
    if not scripts:
        return None, None
    return json.loads(scripts[-1].read_text(encoding="utf-8")), scripts[-1]


def _shot_characters(shot: dict[str, Any]) -> list[str]:
    refs = shot.get("references", {}) or {}
    chars = list(refs.get("characters", []) or [])
    for dlg in shot.get("dialogue", []) or []:
        if dlg.get("char") and dlg.get("char") not in chars:
            chars.append(dlg.get("char"))
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
        rj = rd / "refs.json"
        if not rj.exists():
            continue
        try:
            prior = json.loads(rj.read_text(encoding="utf-8"))
        except Exception:
            continue
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
                # ("Chitinous Marauder Pods (x6)") resolve to the unit's ref.
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
    """Names + ref paths for EVERY character on screen, honouring the shot's
    structured costume/variant declarations (references.costumes)."""
    names = _shot_characters(shot)
    ref_map = _char_ref_map(show)
    costumes = ((shot.get("references") or {}).get("costumes") or {}) or {}
    prose = " ".join([shot.get("action", ""), shot.get("camera", ""),
                      shot.get("soundscape", "")])
    for m in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", prose):
        n = m.group(0).strip()
        if n in ref_map and n not in names:
            names.append(n)
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


def _keyframe_prompt(shot: dict[str, Any], scene: dict[str, Any], names: list[str],
                     appearances: dict[str, str] | None = None) -> str:
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
    return ("Anime cinematic keyframe: "
            f"{action} {camera}. Setting: {setting}. On screen: {chars}.{desc} "
            "Cinematic composition, dynamic framing, consistent series art style, "
            "high quality. ABSOLUTELY NO text, no letters, no words, no dialogue "
            "bubbles, no captions, no subtitles, no watermark, no logos.").rstrip(" .") + "."


def _char_appearances(show: Show) -> dict[str, str]:
    """name -> appearance_canonical for every character."""
    out: dict[str, str] = {}
    for cid in show.list_characters():
        c = show.read_character(cid)
        if c.get("name"):
            out[c["name"]] = (c.get("appearance_canonical") or "").replace("\n", " ").strip()
    return out


# Camera-direction / film-technique phrases that the object heuristic must never
# treat as recurring objects. A capitalized phrase whose ANY token is one of
# these is a framing/lens direction, not a prop.
_CAMERA_TOKENS = {
    "close", "closeup", "close-up", "cu", "ec", "ecu", "extreme", "medium",
    "wide", "shot", "angle", "low", "high", "dutch", "overhead", "aerial",
    "top", "eye", "view", "level", "shoulder", "ots", "reverse", "tracking",
    "dolly", "pan", "tilt", "zoom", "rack", "focus", "insert", "inserts",
    "two", "three", "group", "establishing", "establish", "pov", "over",
    "profile", "tight", "full", "cowboy", "chest", "waist", "knee", "head",
    "behind", "above", "below", "from", "side", "frontal", "rear", "crane",
    "handheld", "stationary", "push", "pull", "walk", "run", "circle",
    "depth", "field", "shallow", "bokeh", "blur", "focal", "perspective",
    "vignette", "lens", "aperture", "exposure", "lighting", "luminance",
    "silhouette", "backlit", "backlight", "glare",
}


def _is_camera_phrase(phrase: str) -> bool:
    """True when a capitalized phrase looks like a camera direction."""
    toks = set(re.split(r"\s+", phrase.lower()))
    if toks & _CAMERA_TOKENS:
        return True
    # Common two-part framings like "Over The Shoulder", "Low Angle CU".
    low = phrase.lower()
    for frag in ("over the shoulder", "over-the-shoulder", "low angle", "high angle",
                 "eye level", "top down", "aerial view", "medium wide", "tight two",
                 "extreme close", "depth of field", "shallow focus", "shallow depth",
                 "rack focus", "focus pull", "lens flare"):
        if frag in low:
            return True
    return False


_OBJECT_STOPWORDS = {"the", "a", "an", "of", "and", "in", "on", "with"}

# Generic scenery / debris words. A picked item containing ANY of these is not a
# key recurring item (a weapon, vehicle, plot device or set piece) — it's
# environment filler, no matter what the LLM says. The local 12B showrunner
# reliably ignores a long exclusion list, so this deterministic net does the job.
_JUNK_OBJECT_TOKENS = (
    "debris", "rubble", "dust", "chunks", "chunk", "gravel", "dirt", "mud",
    "sand", "ash", "rock", "rocks", "stone", "stones", "brick", "bricks",
    "concrete", "rebar", "girder", "girders", "shard", "shards", "fragment",
    "fragments", "wreck", "wreckage", "ruins", "ruin", "pile", "heap", "ground",
    "floor", "wall", "walls", "ceiling", "road", "street", "pavement", "glass",
    "scaffold", "scaffolding", "shrapnel", "trash", "garbage", "rubbish",
    "remains", "remnants", "pulverized", "pulverised",
    # structural / transport infrastructure is scenery, not a prop: supports,
    # beams, pillars, viaducts, bridges, barriers — and "collapsed X" is wreckage.
    "viaduct", "overpass", "bridge", "support", "supports", "beam", "beams",
    "strut", "struts", "pillar", "pillars", "column", "columns", "pylon",
    "pylons", "railing", "railings", "barricade", "barricades", "barrier",
    "barriers", "trestle", "collapsed", "collapse",
    # fixed location fixtures / architecture are part of the LOCATION's ref, not
    # a recurring prop: fountains, statues, monuments, plazas, arches, towers.
    "fountain", "fountains", "statue", "statues", "monument", "monuments",
    "plaza", "plazas", "square", "squares", "arch", "arches", "tower",
    "towers", "spire", "spires", "obelisk", "obelisk", "colonnade", "colonnades",
    "gazebo", "gazebos", "bandstand", "clock", "clock tower", "water feature",
    "basin", "fountainhead", "memorial", "memorials", "plinth", "plinth",
    "pedestal", "pedestals",
)


def _is_junk_object(name: str) -> bool:
    low = name.lower()
    return any(tok in low for tok in _JUNK_OBJECT_TOKENS)


def _object_tokens(name: str) -> set[str]:
    return {w for w in re.split(r"\W+", name.lower())
            if w and w not in _OBJECT_STOPWORDS}


def _dedup_object_names(names: list[str]) -> list[str]:
    """Drop near-duplicate item names so one object never spawns two refs.

    Two names are near-duplicates when one contains the other, OR they share
    2+ significant tokens that cover most of the shorter name ("Ferrocrete
    Ground Debris" vs "Ferrocrete Pulverized Chunks Ground"). The shorter,
    cleaner name wins.
    """
    names = [n.strip() for n in names if n and n.strip()]
    keep: list[str] = []
    for n in names:
        low = n.lower()
        toks = _object_tokens(n)
        dup = False
        for other in names:
            if other == n:
                continue
            o_low = other.lower()
            if low in o_low or o_low in low:
                # One name contains the other — keep the shorter.
                if len(low) >= len(o_low):
                    dup = True
                break
            otoks = _object_tokens(other)
            shared = toks & otoks
            if len(shared) >= 2 and len(shared) >= min(len(toks), len(otoks)) - 1:
                if len(other) < len(n):
                    dup = True
                    break
        if not dup:
            keep.append(n)
    return keep


def _llm_recurring_objects(show: Show, episode: int, script: dict[str, Any],
                           cfg=None, llm=None) -> list[str]:
    """KEY recurring items picked by the showrunner from the WHOLE script.

    The model reads the episode story + every shot's action/camera and returns
    only items that matter for continuity — signature weapons, vehicles, plot
    devices, distinctive set pieces. Generic scenery (debris, ground, dust) is
    explicitly excluded. There is NO regex fallback: on failure we return [] —
    a bogus object ref is worse than none.
    """
    from . import prompts
    from .clients.lmstudio import LMStudioClient
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
    model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
    cast = [c.get("name") for c in (show.read_character(cid)
                                    for cid in show.list_characters())
            if c.get("name")]
    locations = set()
    for sc in script.get("scenes", []):
        if sc.get("location"):
            locations.add(str(sc["location"]))
        for shot in sc.get("shots", []):
            sref = (shot.get("references") or {}).get("scene")
            if sref:
                locations.add(str(sref))
    episode_summary = (script.get("summary") or "")
    if not episode_summary.strip():
        from .planner import read_episode_plan
        plan = read_episode_plan(show, episode)
        episode_summary = (plan.get("plot") or plan.get("summary") or "")
    ACTIVITY.setdefault(show.show_id, {})["detail"] = "Picking key recurring items…"
    system = prompts.showrunner_system(
        cfg["show_profile"], (show.read_bible() or {}).get("content_policy", "mature"),
        cfg["show_profile"].get("baseline", "ranma-1-2"))
    try:
        out = llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompts.recurring_objects_prompt(
                 script, cast, sorted(locations), episode_summary)}],
            model=model, temperature=0.2, max_tokens=2048)
    except Exception as exc:
        log.warning("object extraction LLM failed for %s EP%02d: %s",
                    show.show_id, episode, exc)
        return []
    names = [str(n).strip() for n in (out.get("objects") or []) if str(n).strip()]
    # Safety net: never let a camera phrase, a cast/location name, or generic
    # scenery (debris, rubble, dust…) through.
    known = set(cast) | {k for k in locations if k}
    names = [n for n in names
             if n not in known and not _is_camera_phrase(n)
             and not _is_junk_object(n)
             and not any(k.lower() in n.lower() or n.lower() in k.lower()
                         for k in known if k)]
    return _dedup_object_names(names)[:6]


def _shot_object_refs(show: Show, episode: int, shot: dict[str, Any]) -> list[str]:
    """Reference-image paths for recurring objects present in this shot.

    Matches the same capitalized multi-word heuristic against the shot's
    action/camera text and returns the ref for any object whose ref image
    exists under runs/EP##/objects/<slug>.png.
    """
    od = show.dir / "runs" / f"EP{episode:02d}" / "objects"
    if not od.exists():
        return []
    text = f"{shot.get('action', '')} {shot.get('camera', '')}"
    # Objects are matched by filename slug only, so camera phrases and character
    # fragments can only resolve to an actual ref file if one exists; the slug
    # itself is the authority. Camera phrases are still skipped (no ref file is
    # generated for them anyway).
    out: list[str] = []
    for m in re.finditer(r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+\b", text):
        if _is_camera_phrase(m.group(0)):
            continue
        slug = re.sub(r"[^a-z0-9-]+", "-", m.group(0).lower()).strip("-")
        p = od / f"{slug}.png"
        if p.exists() and str(p) not in out:
            out.append(str(p))
    return out


def _char_ref_images(show: Show, names: list[str]) -> list[str]:
    """Approved reference-image paths for the given character names.

    Names are matched via their unit key, so a script that says
    "Chitinous Marauder Pods (x6)" resolves to the ref of the single-unit sheet
    "Chitinous Marauder Pod".
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
        scripts = sorted(d.glob("script.r*.json"), key=lambda p: p.stat().st_mtime)
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
    # Resolve the base ref image (identity anchor for the edit).
    base_ref = None
    rj = rd / "refs.json"
    if rj.exists():
        try:
            prior = json.loads(rj.read_text(encoding="utf-8"))
            base_file = (prior.get("variants") or {}).get("base")
            if base_file and (rd / base_file).exists():
                base_ref = str(rd / base_file)
        except Exception:
            prior = None
    if not prompt:
        if mode == "edit":
            # Build the edit prompt from an LLM costume DESCRIPTION (what changes
            # from the base ref), not the bare label — the label alone makes every
            # costume come out identical. Driven by personality + use case, never
            # the character's current look (which poisons the description).
            from .bootstrap import BootstrapChain
            personality = ""
            situation = f"{name} wears '{label}' in combat"
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
                label, name, personality, situation)
            if description:
                prompt = f"Replace the character's clothes to: {description}"
            else:
                prompt = f"Replace the character's clothes to: {label}"
        else:
            prompt = (f"Full-body anime character reference in the costume '{label}': "
                      f"{canon}".rstrip(" .")
                      + ". Plain studio background, clean lineart, consistent character "
                        "design, high quality, front view, neutral standing pose.")
    if mode == "edit":
        if seed is None:
            seed = _fresh_costume_seed(rd, label)
        ok = _render_costume_via_qwen_edit(show, base_ref, name, prompt, label,
                                           str(out), cfg, seed=seed)
    else:
        if seed is None:
            seed = _fresh_costume_seed(rd, label)
        ok = _render_costume_via_krea2(show, name, prompt, label, str(out), cfg,
                                       seed=seed)
    if not ok:
        return False
    try:
        prior = json.loads(rj.read_text(encoding="utf-8"))
    except Exception:
        prior = {"status": "real", "refs": [out.name]}
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
    rj.write_text(json.dumps(prior), encoding="utf-8")
    return True


def _render_costume_via_krea2(show: Show, name: str, prompt: str, label: str,
                              out_path: str, cfg, seed: int = 0) -> bool:
    """Fresh krea2 text-to-image render (no identity anchor)."""
    from .remote.ops import ServiceOps
    from .comfy_workflows import generate_keyframe, load_workflow
    from .gpu_manager import ServiceType, get_gpu_manager
    ops = ServiceOps(cfg)
    client, stop = ops._krea2_client()
    try:
        # krea2 runs on its own instance (no sage-attention); serialize with H3
        # via the GPU manager's exclusive COMFYUI lock so they never share VRAM.
        with get_gpu_manager(cfg).acquire(ServiceType.COMFYUI):
            generate_keyframe(client, load_workflow(cfg.workflows_dir / "image_keyframe.json"),
                              prompt, seed, out_path, aspect_ratio="3:4")
        return True
    except Exception as exc:
        ACTIVITY.setdefault(show.show_id, {})["detail"] = (
            f"Costume regen {label} failed: {exc}")
        return False
    finally:
        if stop is not None:
            stop()


def _render_costume_via_qwen_edit(show: Show, base_ref: str | None, name: str,
                                  prompt: str, label: str, out_path: str,
                                  cfg, seed: int = 0) -> bool:
    """Render a costume via Qwen-Image-Edit (v16 safetensors API workflow).

    The base ref is uploaded to the krea2 ComfyUI input dir and used as the edit
    source; the model edits the costume while preserving identity. ``seed`` is
    the Qwen-Image-Edit sampler seed (0 = randomize). Returns True when an image
    lands at out_path.

    The instance is picked per-render by :meth:`ServiceOps._krea2_client` — the
    primary when it answers /system_stats, otherwise a transient local launch or
    the Beast3 fallback. A wedged local instance (job stuck in queue_running,
    executor hung) therefore degrades to the fallback instead of stalling the
    whole costume pass; the hard wall-clock timeout in the wait bounds each
    attempt so a wedged job fails fast and the next variant / reconcile pass
    tries again on a healthy instance.

    The whole render is gated on the shared instance's queue draining: Qwen-
    Image-Edit (19 GB) cannot share the 16 GB GPU with an H3 render, and a job
    submitted mid-render silently produces a black frame. Waiting for the queue
    to empty first ensures H3's models are evicted before Qwen loads.
    """
    from .remote.ops import ServiceOps
    from .qwen_edit import adapt_qwen_edit_workflow, load_qwen_edit_workflow
    import time

    cfg = cfg or get_config()
    ops = ServiceOps(cfg)
    client, stop = ops._krea2_client()
    try:
        ref_fn = None
        if base_ref:
            try:
                ref_fn = client.upload_image(base_ref)
            except Exception:
                ref_fn = None
        if not ref_fn:
            # No base image: fall back to fresh krea2 generation.
            return _render_costume_via_krea2(show, name, prompt, label, out_path, cfg)
        # Qwen-Image-Edit runs on the krea2 instance (NO --use-sage-attention:
        # that flag breaks Qwen's attention and yields a black frame). H3 runs on
        # a SEPARATE instance that REQUIRES sage-attention. Both share one 16 GB
        # GPU, so the GPU manager's exclusive COMFYUI lock serializes them —
        # Qwen waits for any H3 render to finish, and vice versa.
        from .gpu_manager import ServiceType, get_gpu_manager
        with get_gpu_manager(cfg).acquire(ServiceType.COMFYUI):
            return _qwen_edit_render(show, client, cfg, prompt, ref_fn, label, out_path, seed)
    finally:
        if stop:
            stop()


def _qwen_edit_render(show: Show, client, cfg, prompt: str, ref_fn: str, label: str,
                      out_path: str, seed: int) -> bool:
    """Run one Qwen-Image-Edit render (GPU already owned exclusively)."""
    from .qwen_edit import adapt_qwen_edit_workflow, load_qwen_edit_workflow
    sid = show.show_id
    try:
        wf = load_qwen_edit_workflow(cfg.root)
        wf = adapt_qwen_edit_workflow(wf, prompt, ref_fn, seed=seed, steps=8, cfg=4.0)
        pid = client.submit(wf)
        # Hard wall-clock cap: a wedged Qwen-Image-Edit job (19 GB model on a
        # shared 16 GB GPU) that hangs in queue_running must not block the whole
        # storyboard forever. On expiry the wait interrupts the instance and
        # raises; the variant is marked failed here and retried on the next
        # reconcile pass.
        entry = client.wait(pid, timeout_s=900, poll_interval=5, hard_timeout_s=1200)
        status = (entry.get("status") or {}).get("status_str", "")
        if status != "success":
            ACTIVITY.setdefault(sid, {})["detail"] = (
                f"Qwen costume edit {label} status: {status}")
            return False
        img = None
        for _nid, output in (entry.get("outputs") or {}).items():
            for i in output.get("images", []):
                img = i
                break
            if img:
                break
        if not img:
            ACTIVITY.setdefault(sid, {})["detail"] = (
                f"Qwen costume edit {label}: no output image")
            return False
        client.download(img["filename"], img.get("subfolder", ""),
                        img.get("type", "output"), out_path)
        return True
    except Exception as exc:
        ACTIVITY.setdefault(sid, {})["detail"] = (
            f"Qwen costume edit {label} failed: {exc}")
        log.warning("qwen costume edit failed for %s: %s", label, exc, exc_info=True)
        return False


def generate_shot_keyframes(show: Show, episode: int, cfg=None, progress=None,
                            scene_ids: list[str] | None = None) -> int:
    """Generate a 1-second H3 preview video for every shot.

    The preview runs the SAME ref2va workflow and character refs as the final
    render (`length` snapped to H3's minimum grid ~22 frames \u2248 1s), so frame 0
    \u2014 and the whole clip \u2014 shows exactly how the video will start. Outputs
    `runs/EP##/storyboard/<sid>.mp4`.

    `scene_ids` scopes generation to specific scenes (e.g. ["s01"]).
    """
    from .render import (_render_client, _shot_character_ref, _voice_sample_path,
                         compile_shot_prompt, _shot_ref_paths)
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
                    sid = shot.get("id", f"sh{done}")
                    out = show.dir / "runs" / f"EP{episode:02d}" / "storyboard" / f"{sid}.mp4"
                    if out.exists():
                        done += 1
                        if progress:
                            progress(done, total, sid)
                        continue
                    names, _ = _shot_refs(show, shot)
                    # Character refs first (identity anchors), then objects/
                    # keyframe refs \u2014 same ordering the final render uses.
                    image_filenames: list[str] = []
                    for name in names:
                        ref = _shot_character_ref(show, name)
                        if not ref:
                            continue
                        try:
                            image_filenames.append(client.upload_image(ref))
                        except Exception:
                            continue
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
                            if not vp or name not in names:
                                continue
                            try:
                                audio_filenames.append(client.upload_audio(vp))
                            except Exception:
                                continue
                            audio_refs.append({"char": name,
                                               "token": f"<Audio {len(audio_refs) + 1}>"})
                    prompt = compile_shot_prompt(show, script, sc, shot, names,
                                                 audio_refs=audio_refs or None)
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
        prompt = _object_prompts(show, episode).get(slug) or _base_object_prompt(name)
        try:
            ServiceOps(cfg).generate_image(prompt, seed=0, aspect_ratio="1:1",
                                           out_path=str(out))
            _save_object_prompt(show, episode, slug, prompt)
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
    current = _object_prompts(show, episode).get(slug) or _base_object_prompt(name)
    prompt = current
    if notes.strip():
        from . import prompts
        model = cfg.get("llm", "roles", {}).get("showrunner") or cfg.get("llm", "model")
        try:
            llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=240)
            text = llm.chat([
                {"role": "system", "content": prompts.showrunner_system(
                    cfg["show_profile"], (show.read_bible() or {}).get("content_policy", "mature"),
                    cfg["show_profile"].get("baseline", "ranma-1-2"))},
                {"role": "user", "content": prompts.revise_object_prompt(current, notes)},
            ], model=model, temperature=0.4, max_tokens=900)
            revised = text[text.find("{"): text.rfind("}") + 1] or ""
            import json as _json
            parsed = _json.loads(revised)
            candidate = (parsed.get("prompt") or "").strip()
            if candidate:
                prompt = candidate
        except Exception:
            pass  # keep the current prompt; still regenerate from it directly
    try:
        ServiceOps(cfg).generate_image(prompt, seed=0, aspect_ratio="1:1",
                                       out_path=str(out))
        _save_object_prompt(show, episode, slug, prompt)
    except Exception as exc:
        ACTIVITY.setdefault(show.show_id, {})["detail"] = f"Object ref {name} failed: {exc}"
        return False
    return out.exists()


def _pending_ref_names(show: Show) -> list[str]:
    """Character names whose base reference image is not yet approved (pending
    or not generated). The storyboard previews must wait for these — a preview
    rendered without the approved ref would show the wrong character."""
    st = show.bootstrap_state()
    out: list[str] = []
    for ch in st.get("characters", []):
        refs = (ch.get("refs") or "").strip()
        if refs != "approved":
            out.append(ch.get("name", "?"))
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
    return show.dir / "runs" / f"EP{episode:02d}" / "objects"


def _approved_costume_labels(show: Show, cid: str) -> set[str]:
    """Costume-variant labels already approved for one character."""
    rj = show.character_refs_dir(cid) / "refs.json"
    if not rj.exists():
        return set()
    try:
        data = json.loads(rj.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return set(data.get("approved") or [])


def _approved_object_slugs(show: Show, episode: int) -> set[str]:
    ap = _objects_dir(show, episode) / "approvals.json"
    if not ap.exists():
        return set()
    try:
        return set((json.loads(ap.read_text(encoding="utf-8")) or {}).get("approved") or [])
    except Exception:
        return set()


def _object_prompts(show: Show, episode: int) -> dict[str, str]:
    """Persisted generation prompt per recurring object slug, across iterations.

    Mirrors how costume variants store their prompt in refs.json: the feedback
    loop keeps the last (LLM-revised) prompt so successive rejects FURTHER refine
    it instead of starting over from the base template every time.
    """
    p = _objects_dir(show, episode) / "prompts.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_object_prompt(show: Show, episode: int, slug: str, prompt: str) -> None:
    od = _objects_dir(show, episode)
    od.mkdir(parents=True, exist_ok=True)
    prompts = _object_prompts(show, episode)
    prompts[slug] = prompt
    (od / "prompts.json").write_text(json.dumps(prompts, ensure_ascii=False),
                                     encoding="utf-8")


def _base_object_prompt(name: str) -> str:
    """The default first-iteration prompt for a recurring object."""
    return (f"Anime reference image of the recurring object: {name}. "
            "THE OBJECT ALONE, isolated on a plain studio background, consistent "
            "series art style, high quality, no text, no watermark, NO PEOPLE, "
            "NO HUMANS, NO CHARACTERS, NO HANDS, NO FOOT, NO FACES, NO FIGURES "
            "carrying or touching it.")


def mark_costume_approved(show: Show, cid: str, label: str) -> None:
    """Record one costume variant as approved (refs.json). Idempotent."""
    rj = show.character_refs_dir(cid) / "refs.json"
    if not rj.exists():
        raise ValueError(f"character {cid} has no refs.json")
    data = json.loads(rj.read_text(encoding="utf-8"))
    approved = set(data.get("approved") or [])
    approved.add(label)
    data["approved"] = sorted(approved)
    rj.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def delete_costume(show: Show, cid: str, label: str) -> None:
    """Delete one costume variant: drop its ref image + registry entries.

    Removes the variant from refs.json (variants/prompts/approved), deletes the
    rendered ref PNG, and re-renders nothing. The base outfit is protected — you
    cannot delete a character's canonical base look. Idempotent: deleting a
    variant that doesn't exist is a no-op.
    """
    rd = show.character_refs_dir(cid)
    rj = rd / "refs.json"
    if not rj.exists():
        return
    data = json.loads(rj.read_text(encoding="utf-8"))
    variants = data.get("variants") or {}
    if label == "base" or label not in variants:
        if label == "base":
            raise ValueError("cannot delete a character's base outfit")
        return
    img = variants.pop(label, None)
    (data.get("prompts") or {}).pop(label, None)
    data["approved"] = [a for a in (data.get("approved") or []) if a != label]
    rj.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if img:
        f = rd / img
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass


def mark_object_approved(show: Show, episode: int, slug: str) -> None:
    """Record one recurring-object ref as approved. Idempotent."""
    od = _objects_dir(show, episode)
    od.mkdir(parents=True, exist_ok=True)
    ap = od / "approvals.json"
    approved = _approved_object_slugs(show, episode)
    approved.add(slug)
    ap.write_text(json.dumps({"approved": sorted(approved)}, ensure_ascii=False),
                  encoding="utf-8")


def unapprove_object(show: Show, episode: int, slug: str) -> None:
    """Remove an object ref from the approved set (after a regen/reject)."""
    od = _objects_dir(show, episode)
    ap = od / "approvals.json"
    if not ap.exists():
        return
    try:
        data = json.loads(ap.read_text(encoding="utf-8"))
    except Exception:
        return
    data["approved"] = [s for s in (data.get("approved") or []) if s != slug]
    ap.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


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


def build_storyboard(show: Show, episode: int, cfg=None) -> None:
    """Generate shot keyframes + object refs in a background thread with progress."""
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
            # Phase 2: 1-second H3 preview per shot (same workflow/refs as the
            # final render, so the preview shows exactly how the video starts).
            generate_shot_keyframes(show, episode, cfg=cfg, progress=prog)
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
