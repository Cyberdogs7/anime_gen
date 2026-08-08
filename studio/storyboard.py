"""Episode storyboard: shot keyframes + recurring-object reference images.

Renders a krea2 keyframe for every shot of an episode (the storyboard panel)
plus one reference image per recurring object so those objects stay consistent
across shots. Images land in runs/EP##/storyboard/ and runs/EP##/objects/.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

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
            if entry:
                out[name] = entry
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
    refs: list[str] = []
    for n in names:
        entry = ref_map.get(n)
        if not entry:
            continue
        label = costumes.get(n, "base")
        pick = entry.get(label) or entry.get("base")
        if pick:
            refs.append(pick)
    return names, refs


def _keyframe_prompt(shot: dict[str, Any], scene: dict[str, Any], names: list[str]) -> str:
    setting = (scene.get("summary") or scene.get("location") or "").strip()
    action = (shot.get("action") or "").strip()
    camera = (shot.get("camera") or "").strip()
    chars = ", ".join(names) if names else "the characters in this scene"
    return ("Anime cinematic keyframe: "
            f"{action} {camera}. Setting: {setting}. On screen: {chars}. "
            "Cinematic composition, dynamic framing, consistent series art style, "
            "high quality. ABSOLUTELY NO text, no letters, no words, no dialogue "
            "bubbles, no captions, no subtitles, no watermark, no logos.").rstrip(" .") + "."


def _recurring_objects(script: dict[str, Any]) -> list[str]:
    """Capitalized multi-word nouns appearing across 2+ shots (rough heuristic)."""
    shots = [s for sc in script.get("scenes", []) for s in sc.get("shots", [])]
    texts = [f"{s.get('action', '')} {s.get('camera', '')}" for s in shots]
    seen_in: dict[str, set[int]] = {}
    for idx, t in enumerate(texts):
        for m in re.finditer(r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+\b", t):
            name = m.group(0)
            seen_in.setdefault(name, set()).add(idx)
    return [n for n, shots_i in seen_in.items() if len(shots_i) >= 2]


def _char_ref_images(show: Show, names: list[str]) -> list[str]:
    """Approved reference-image paths for the given character names."""
    out: list[str] = []
    for cid in show.list_characters():
        c = show.read_character(cid)
        if c.get("name") in names:
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


def ensure_variant_refs(show: Show, script: dict[str, Any], cfg=None) -> int:
    """Generate a ref image for each costume variant a character actually wears.

    Generic: every (character, costume-label) pair the script declares gets its own
    reference image, derived from the character's base appearance plus the label.
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
            jobs.append((cid, name, label, (c.get("appearance_canonical") or "")))
    if not jobs:
        return 0
    created = 0
    ops = ServiceOps(cfg)
    client, stop = ops._krea2_client()
    try:
        for cid, name, label, canon in jobs:
            ACTIVITY[show.show_id] = {"detail": f"Costume ref '{label}' for {name} (Krea 2)…",
                                      "ts": time.time()}
            rd = show.character_refs_dir(cid)
            slug = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
            out = rd / f"{cid}_{slug}_01.png"
            prompt = (f"Anime character reference of {name} in their '{label}' "
                      f"appearance: {canon}".rstrip(" .")
                      + ". Full body, front view, neutral standing pose, plain studio "
                        "background, clean lineart, consistent character design, high quality.")
            try:
                generate_keyframe(client, load_workflow(wf_path), prompt, 0, str(out),
                                  aspect_ratio="3:4")
                prior = json.loads((rd / "refs.json").read_text(encoding="utf-8"))
                variants = prior.setdefault("variants", {})
                if "base" not in variants and prior.get("refs"):
                    base = next((r for r in prior["refs"] if "mech" not in r),
                                prior["refs"][0])
                    variants["base"] = base
                variants[label] = out.name
                prior["status"] = "real"
                (rd / "refs.json").write_text(json.dumps(prior), encoding="utf-8")
                created += 1
            except Exception as exc:
                ACTIVITY.setdefault(show.show_id, {})["detail"] = (
                    f"Costume ref {name}/{label} failed: {exc}")
    finally:
        if stop:
            stop()
    return created


def generate_shot_keyframes(show: Show, episode: int, cfg=None, progress=None) -> int:
    """Generate a keyframe for every shot, using character refs for consistency."""
    from .remote import ServiceOps
    from .comfy_workflows import (generate_keyframe, generate_keyframe_with_ref,
                                  load_workflow)
    script, _ = _latest_script(show, episode)
    if not script:
        return 0
    ops = ServiceOps(cfg)
    wf_path = (cfg or get_config()).workflows_dir / "image_keyframe.json"
    client, stop = ops._krea2_client()
    scenes = script.get("scenes", [])
    shots = [s for sc in scenes for s in sc.get("shots", [])]
    total = len(shots)
    done = 0
    try:
        for sc in scenes:
            prev_kf: Path | None = None
            for shot in sc.get("shots", []):
                sid = shot.get("id", f"sh{done}")
                out = show.dir / "runs" / f"EP{episode:02d}" / "storyboard" / f"{sid}.png"
                if out.exists():
                    done += 1
                    prev_kf = out
                    if progress:
                        progress(done, total, sid)
                    continue
                names, refs = _shot_refs(show, shot)
                # Shot-line continuity: reuse the previous keyframe of the scene as an
                # extra reference so consecutive shots stay visually consistent.
                if prev_kf and prev_kf.exists():
                    refs = list(refs) + [str(prev_kf)]
                prompt = _keyframe_prompt(shot, sc, names)
                if progress:
                    progress(done, total, sid)
                try:
                    if refs and wf_path.exists():
                        generate_keyframe_with_ref(client, load_workflow(wf_path),
                                                   prompt, 0, refs, str(out),
                                                   aspect_ratio="16:9", weight=0.8)
                    elif wf_path.exists():
                        generate_keyframe(client, load_workflow(wf_path), prompt, 0,
                                          str(out), aspect_ratio="16:9")
                except Exception as exc:
                    ACTIVITY.setdefault(show.show_id, {})["detail"] = (
                        f"Storyboard shot {sid} failed: {exc}")
                done += 1
                prev_kf = out
                if progress:
                    progress(done, total, sid)
    finally:
        if stop:
            stop()
    return done


def generate_object_refs(show: Show, episode: int, cfg=None, progress=None) -> int:
    """One reference image per recurring object in the episode."""
    from .remote import ServiceOps
    script, _ = _latest_script(show, episode)
    if not script:
        return 0
    objs = _recurring_objects(script)
    for i, name in enumerate(objs, start=1):
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
        out = show.dir / "runs" / f"EP{episode:02d}" / "objects" / f"{slug}.png"
        if out.exists():
            continue
        prompt = (f"Anime reference image of the recurring object: {name}. "
                  "Isolated on a plain studio background, consistent series art style, "
                  "high quality, no text, no watermark.")
        try:
            ServiceOps(cfg).generate_image(prompt, seed=0, aspect_ratio="1:1",
                                           out_path=str(out))
        except Exception as exc:
            ACTIVITY.setdefault(show.show_id, {})["detail"] = f"Object ref {name} failed: {exc}"
        if progress:
            progress(i, len(objs), name)
    return len(objs)


def build_storyboard(show: Show, episode: int, cfg=None) -> None:
    """Generate shot keyframes + object refs in a background thread with progress."""
    def _run():
        job = {"state": "running", "done": 0, "total": 0, "detail": "Preparing storyboard…"}
        STORYBOARD_JOBS[show.show_id] = job

        def prog(done, total, label):
            job["done"], job["total"] = done, total
            job["detail"] = f"Generating storyboard {done}/{total}: {label}"
            ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}

        try:
            from .casting import create_missing_character_refs
            created = create_missing_character_refs(show, episode, cfg=cfg)
            if created:
                job["detail"] = f"Ref pass: new characters {', '.join(created)}"
                ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}
            script, _ = _latest_script(show, episode)
            vn = ensure_variant_refs(show, script or {}, cfg=cfg)
            if vn:
                job["detail"] = f"Costume refs: {vn} variants"
                ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}
            total_shots = len([s for sc in (script or {}).get("scenes", [])
                               for s in sc.get("shots", [])])
            job["total"] = total_shots
            generate_shot_keyframes(show, episode, cfg=cfg, progress=prog)
            job["detail"] = "Consistency review (vision)…"
            ACTIVITY[show.show_id] = {"detail": job["detail"], "ts": time.time()}
            from .consistency import run_consistency_check
            job["report"] = run_consistency_check(show, episode, cfg=cfg)
            job["state"] = "done"
            job["detail"] = "Storyboard + consistency complete"
        except Exception as exc:
            job["state"] = "failed"
            job["detail"] = f"Storyboard failed: {exc}"
        finally:
            ACTIVITY.pop(show.show_id, None)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def storyboard_status(show_id: str) -> dict[str, Any]:
    return dict(STORYBOARD_JOBS.get(show_id, {"state": "idle", "done": 0, "total": 0, "detail": ""}))
