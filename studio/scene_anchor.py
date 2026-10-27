"""Per-scene visual grounding for shot prompts.

A shot must not invent its own set. Each episode scene gets ONE anchor image
that every shot in that scene is grounded on:

  1. the show's registry location ref for the scene
     (``scenes/<loc>/refs/<loc>_ref_01.png``) when the plan's free-text
     ``location`` resolves to a registered location, else
  2. the last frame of the scene's FIRST storyboard preview (the actual
     rendered set), extracted once to ``runs/EP##/scenes/<sid>_anchor.png``.

The same anchor is (a) passed to H3 as a ``<Picture N>`` scene reference for
every shot of the scene, and (b) summarised once per scene by the vision model
into a short continuity brief that the shot writer carries into every pass, so
all shots share one layout, lighting direction and fixed props.

Everything here is best-effort: a missing ffmpeg, VLM, registry or preview
degrades to "no anchor" rather than failing the pipeline.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from . import scenes
from .config import get_config


def _plan_location_id(show, episode: int, scene_id: str) -> str:
    """The canonical ``location_id`` for a scene, from the episode's plan.json."""
    if not scene_id:
        return ""
    try:
        p = show.dir / "runs" / f"EP{episode:02d}" / "plan.json"
        if not p.exists():
            return ""
        plan = json.loads(p.read_text(encoding="utf-8")) or {}
        for sc in plan.get("scenes", []) or []:
            if isinstance(sc, dict) and sc.get("id") == scene_id:
                return str(sc.get("location_id") or "").strip()
    except Exception:
        pass
    return ""


def resolve_location_ref(show, scene: dict[str, Any], episode: int | None = None,
                         shot: dict[str, Any] | None = None) -> Path | None:
    """The registry location ref for a shot (or scene), or None when unresolved.

    A shot may sit in a specific environment of its own (``references.location_id``
    / ``references.scene``) that differs from the scene's broader place — that
    environment's ref wins. Resolution order (most specific first):

    1. the SHOT's ``references.location_id``;
    2. the shot's ``references.scene`` (free text, via the registry matcher);
    3. the scene's ``location_id``;
    4. the episode plan's ``location_id`` for this scene id;
    5. a confident match of the scene's free-text ``location``.
    """
    try:
        refs = (shot or {}).get("references") or {}
        # Shot-specific environment first (most specific).
        shot_lid = str(refs.get("location_id") or "").strip()
        if not shot_lid and refs.get("scene"):
            shot_lid = scenes.resolve_slug(show, refs.get("scene")) or ""
        if shot_lid:
            ref = scenes.environment_ref(show, shot_lid)
            if ref:
                return ref
        # Then the scene-level place. The SPECIFIC free-text set wins over a
        # broader `location_id` / plan id (a "pit monitor wall" is not the hangar).
        scene_lids: list[str] = []
        res = scenes.resolve_slug(show, scene.get("location") or "")
        if res:
            scene_lids.append(res)
        scene_lids.append(str(scene.get("location_id") or "").strip())
        if episode is not None:
            scene_lids.append(_plan_location_id(show, episode, str(scene.get("id") or "")))
        for lid in scene_lids:
            if lid:
                ref = scenes.environment_ref(show, lid)
                if ref:
                    return ref
        return None
    except Exception:
        return None


def _extract_last_frame(video: Path, out: Path) -> bool:
    """Write the video's final frame to ``out`` (png). Best-effort."""
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = get_config().ffmpeg_bin()
        subprocess.run(
            [ffmpeg, "-y", "-sseof", "-0.05", "-i", str(video),
             "-frames:v", "1", "-q:v", "2", str(out)],
            capture_output=True, timeout=120)
        return out.exists() and out.stat().st_size > 0
    except Exception:
        return False


def anchor_path(show, episode: int, scene: dict[str, Any],
                shot: dict[str, Any] | None = None, cfg=None) -> Path | None:
    """The grounding image for one shot (or scene), or None when none exists.

    Precedence: the registry ref for the shot's own environment (a specific set
    can differ from the scene's broader place), else the scene's place ref, else
    the scene's cached first-preview frame (``runs/EP##/scenes/<sid>_anchor.png``).
    """
    sid = scene.get("id") or ""
    if not sid:
        return None
    ref = resolve_location_ref(show, scene, episode, shot=shot)
    if ref:
        return ref
    extracted = show.dir / "runs" / f"EP{episode:02d}" / "scenes" / f"{sid}_anchor.png"
    if extracted.exists():
        return extracted
    shots = scene.get("shots") or []
    first = (shots[0].get("id") if shots else "") or ""
    if not first:
        return None
    preview = show.dir / "runs" / f"EP{episode:02d}" / "storyboard" / f"{first}.mp4"
    if not preview.exists():
        return None
    return extracted if _extract_last_frame(preview, extracted) else None


# Prompt-version tag: bump when CONTINUITY_PROMPT changes so stale briefs are not
# reused (the version is part of the cache filename).
_BRIEF_VERSION = "2"


def _env_id(show, scene: dict[str, Any], shot: dict[str, Any] | None = None) -> str:
    """The registry environment id a shot (or scene) sits in.

    A shot's own ``references.location_id`` / ``references.scene`` wins over the
    scene's place, so a specific set gets its own brief. Falls back to the scene
    id when nothing resolves (still gives each scene a stable cache key).
    """
    try:
        refs = (shot or {}).get("references") or {}
        lid = str(refs.get("location_id") or "").strip()
        if not lid and refs.get("scene"):
            lid = scenes.resolve_slug(show, refs["scene"]) or ""
        if not lid:
            # Prefer the SPECIFIC free-text location name over a broader
            # `location_id` so a "pit monitor wall" gets its own set, not the
            # hangar's brief.
            lid = scenes.resolve_slug(show, scene.get("location") or "") or ""
        if not lid:
            lid = str(scene.get("location_id") or "").strip()
        if not lid and scene.get("name"):
            lid = scenes.resolve_slug(show, scene.get("name") or "") or ""
        return lid or str(scene.get("id") or "")
    except Exception:
        return str(scene.get("id") or "")


def _brief_cache(show, episode: int, key: str) -> Path:
    return (show.dir / "runs" / f"EP{episode:02d}" / "scenes"
            / f"{key}.continuity.v{_BRIEF_VERSION}.txt")


CONTINUITY_PROMPT = (
    "You are the continuity supervisor for a show episode. This image is the "
    "reference for one SET (a single location). The shot takes place at: \"{spot}\" "
    "within \"{place}\".\n"
    "Extract ONLY the fixed physical facts of THIS set that must stay consistent "
    "between shots: the architecture and built materials, the fixed props and their "
    "spatial relationships, and the lighting direction, colour and time of day.\n"
    "Do NOT describe camera framing, shot size, an 'establishing' or wide composition, "
    "or where things sit in the image \u2014 every shot chooses its own camera. Do NOT "
    "name or describe any character, and do NOT mention art style or action.\n"
    "Write 50-90 words of plain declarative English, no lists, no headings."
)


def continuity_brief(show, episode: int, scene: dict[str, Any],
                     anchor: Path | str | None = None, shot: dict[str, Any] | None = None,
                     llm=None, cfg=None) -> str:
    """A short, cached environment brief for ONE set (vision model, once).

    Keyed by the resolved environment id (per shot when given), so a cockpit and
    the apron get their own briefs. Returns "" when there is no anchor or the
    model is unavailable; never raises.
    """
    env = _env_id(show, scene, shot)
    if not env:
        return ""
    cache = _brief_cache(show, episode, env)
    cached = cached_continuity_brief(show, episode, scene, shot=shot)
    if cached:
        return cached
    if anchor is None:
        anchor = anchor_path(show, episode, scene, shot=shot, cfg=cfg)
    if not anchor or not Path(anchor).exists():
        return ""
    place = scenes.location_name(show, env)
    refs = (shot or {}).get("references") or {}
    spot = str(refs.get("scene") or scene.get("location") or "").strip()
    prompt = CONTINUITY_PROMPT.format(spot=spot or place or "this set",
                                      place=place or spot or "this set")
    try:
        from .clients.lmstudio import LMStudioClient
        from .consistency import _data_uri, _downscale
        cfg = cfg or get_config()
        if llm is None:
            llm = LMStudioClient(cfg.get("llm", "base_url"), timeout=180)
        model = (cfg.get("llm", "roles", {}) or {}).get("describer") or cfg.get("llm", "model")
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _data_uri(_downscale(Path(anchor)))}},
        ]
        text = llm.chat([{"role": "user", "content": content}], model=model,
                        temperature=0.2, max_tokens=400, role="describer")
        brief = (text or "").strip()
    except Exception:
        return ""
    if brief:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(brief, encoding="utf-8")
        except Exception:
            pass
    return brief


def cached_continuity_brief(show, episode: int, scene: dict[str, Any],
                            shot: dict[str, Any] | None = None) -> str:
    """Read the persisted environment brief without any model call (render-safe)."""
    env = _env_id(show, scene, shot)
    if not env:
        return ""
    cache = _brief_cache(show, episode, env)
    try:
        if cache.exists():
            return cache.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""
