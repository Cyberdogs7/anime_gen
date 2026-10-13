"""Local human approval & review dashboard (DESIGN.md §9.6).

A dependency-free HTTP server (stdlib only) that binds 127.0.0.1, serves a
single-page review surface, and exposes a small JSON API. Every approve/reject
action goes through :mod:`studio.approval`, so the pipeline sees the decision on
the approval bus stream exactly as if a consumer posted it.
"""
from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import approval, fsutil
from .config import get_config
from .proposals import propose_concepts
from .show import Show
from .shows import create_show, create_show_from_proposal, delete_show

_HTML = (Path(__file__).with_name("dashboard.html"))


def _read_json(path: Path | None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _resolve_show_file(show: Show, rel: str) -> Path | None:
    """Resolve a relative path under the show dir, rejecting traversal.

    Returns the resolved path if it is inside the show directory (or a direct
    subdirectory of it), else None. This gates the raw-file edit endpoint so the
    browser can only touch this show's saved-out data.
    """
    if not rel or rel.startswith("/") or ".." in rel.replace("\\", "/").split("/"):
        return None
    p = (show.dir / rel).resolve()
    base = show.dir.resolve()
    if p != base and base not in p.parents:
        return None
    return p


def _proposals_dir() -> Path:
    d = get_config().root / "cache" / "proposals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_proposal_set(guidance: str, proposals: list[dict[str, Any]]) -> str:
    """Persist a generated proposal set so a dismissed modal never loses it."""
    import datetime as _dt
    pid = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = {"id": pid, "created": pid, "guidance": guidance or "",
               "proposals": proposals}
    (_proposals_dir() / f"{pid}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    # Keep only the newest 20 sets.
    files = sorted(_proposals_dir().glob("*.json"))
    for stale in files[:-20]:
        try:
            stale.unlink()
        except OSError:
            pass
    return pid


def _list_proposal_sets() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted(_proposals_dir().glob("*.json"), reverse=True):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _read_show_file(show: Show, rel: str) -> dict[str, Any] | None:
    p = _resolve_show_file(show, rel)
    if p is None or not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return None
    return {"path": rel, "content": text, "format": p.suffix.lstrip(".")}


def _write_show_file(show: Show, rel: str, content: str) -> dict[str, Any]:
    p = _resolve_show_file(show, rel)
    if p is None:
        raise ValueError(f"invalid or outside-show path: {rel!r}")
    p.parent.mkdir(parents=True, exist_ok=True)
    # Validate JSON/YAML so a typo can't silently corrupt saved-out data.
    fmt = p.suffix.lstrip(".").lower()
    if fmt in ("json", "yaml", "yml"):
        try:
            if fmt == "json":
                json.loads(content)
            else:
                import yaml as _yaml
                _yaml.safe_load(content)
        except Exception as exc:
            raise ValueError(f"{rel} is not valid {fmt.upper()}: {exc}")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": rel, "bytes": len(content)}


def _read_yaml(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface, don't hide, a broken file
        logging.getLogger("dashboard").warning("could not read %s: %s", path, exc)
        return None


# config/*.yaml sections the Settings tab may read and write. Whitelisted so the
# endpoint can never be pointed at an arbitrary file path. Sections that carry
# SECRETS (llm: zen auth/key; remotes: node tokens) are deliberately EXCLUDED so
# they are never rendered in, or writable from, the browser.
_EDITABLE_CONFIG_SECTIONS: list[str] = [
    "reviewers", "pipeline", "growth", "show_profile", "approval", "qc",
    "env", "bus", "comfy",
]


def _config_path(section: str) -> Path:
    return get_config().root / "config" / f"{section}.yaml"


def _config_section_payload(section: str) -> dict[str, Any] | None:
    """The Settings payload for one section: raw file values, its YAML text, and
    the merged effective values the pipeline actually reads."""
    if section not in _EDITABLE_CONFIG_SECTIONS:
        return None
    path = _config_path(section)
    raw_text = ""
    if path.exists():
        try:
            raw_text = path.read_text(encoding="utf-8")
        except Exception:
            raw_text = ""
    file_vals = _read_yaml(path)
    if not isinstance(file_vals, dict):
        file_vals = {}
    return {"section": section, "path": str(path), "exists": path.exists(),
            "file": file_vals, "yaml": raw_text,
            "effective": get_config().get(section) or {}}


def _dump_config_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml as _yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")


def _yaml_scalar_text(v: Any) -> str:
    """Render a value as an inline YAML scalar/flow sequence (no document markers)."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    import yaml as _yaml
    s = _yaml.safe_dump(v, default_flow_style=True, allow_unicode=True).strip()
    if s.endswith("..."):           # PyYAML appends a document-end marker to scalars
        s = s[:-3].rstrip()
    return s.replace("\n", " ")


def _patch_yaml_text(text: str, patch: dict[str, Any]) -> str:
    """Apply a settings patch to YAML text at the LINE level.

    A full ``safe_dump`` rewrite would drop every comment in config/*.yaml, so
    instead only the changed keys are rewritten in place (keeping inline comments
    and everything else). New keys are appended. Nested mappings recurse; block
    sequences are collapsed to flow style (``[a, b]``).
    """
    import re as _re
    if not patch:
        return text
    lines = text.splitlines()

    def key_re(key: str, indent: int) -> "_re.Pattern[str]":
        return _re.compile(r"^ {" + str(indent) + r"}" + _re.escape(str(key)) + r"\s*:")

    def find(lines: list[str], key: str, indent: int) -> int:
        pat = key_re(key, indent)
        for i, ln in enumerate(lines):
            if pat.match(ln):
                return i
        return -1

    def apply(lines: list[str], patch: dict[str, Any], indent: int) -> None:
        for key, val in patch.items():
            idx = find(lines, key, indent)
            if isinstance(val, dict):
                if idx < 0:
                    lines.append(" " * indent + f"{key}:")
                    apply(lines, val, indent + 2)
                else:
                    apply(lines, val, indent + 2)
                continue
            text_val = _yaml_scalar_text(val)
            if idx >= 0:
                comment = ""
                m = _re.match(r"^.*?:(.*)$", lines[idx])
                rest = m.group(1) if m else ""
                if " #" in rest:
                    comment = "  #" + rest.split(" #", 1)[1]
                lines[idx] = f"{' ' * indent}{key}: {text_val}{comment}"
                # Drop the old block-sequence items this scalar replaced.
                j = idx + 1
                while j < len(lines) and lines[j].lstrip().startswith("- ") \
                        and (len(lines[j]) - len(lines[j].lstrip())) > indent:
                    del lines[j]
            else:
                lines.append(" " * indent + f"{key}: {text_val}")

    apply(lines, patch, 0)
    return "\n".join(lines) + ("\n" if lines else "")


def _write_config_section(section: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a settings patch to config/<section>.yaml, preserving comments, then
    reload the singleton so the running process picks up the change immediately."""
    from .config import _deep_merge, reload_config
    if section not in _EDITABLE_CONFIG_SECTIONS:
        raise ValueError(f"config section not editable: {section}")
    if not isinstance(patch, dict):
        raise ValueError("'values' must be a JSON object")
    path = _config_path(section)
    current = _read_yaml(path)
    if not isinstance(current, dict):
        current = {}
    expected = _deep_merge(current, patch)
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        new_text = _patch_yaml_text(text, patch)
        # The comment-preserving patch must yield EXACTLY the intended config;
        # otherwise fall back to a clean full rewrite below.
        import yaml as _yaml
        if (_yaml.safe_load(new_text) or {}) != expected:
            raise ValueError("patched config did not match intended values")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
    except Exception:
        # Fall back to a safe full rewrite (correct values; file comments dropped).
        _dump_config_yaml(path, expected)
    reload_config()
    return _config_section_payload(section) or {}


def _write_config_yaml(section: str, text: str) -> dict[str, Any]:
    """Replace config/<section>.yaml with validated YAML (must parse to a mapping)."""
    from .config import reload_config
    if section not in _EDITABLE_CONFIG_SECTIONS:
        raise ValueError(f"config section not editable: {section}")
    import yaml as _yaml
    try:
        data = _yaml.safe_load(text)
    except Exception as exc:
        raise ValueError(f"invalid YAML: {exc}")
    if not isinstance(data, dict):
        raise ValueError("section YAML must be a mapping (key: value)")
    _dump_config_yaml(_config_path(section), data)
    reload_config()
    return _config_section_payload(section) or {}


def _render_status(show_id: str) -> dict[str, Any]:
    from .render import render_status
    return render_status(show_id)


def show_payload(show_id: str) -> dict[str, Any]:
    show = Show(show_id)
    bs = show.bootstrap_state()
    chars = []
    for cs in bs.get("characters", []):
        name = cs.get("name", "")
        content: dict[str, Any] = {}
        for cid in show.list_characters():
            try:
                c = show.read_character(cid)
            except Exception:
                c = {}
            if c.get("name") == name:
                content = c
                break
        char_id = content.get("id", "")
        ref_images = []
        variants = []
        costumes = []
        refs_dir = show.character_refs_dir(char_id)
        if refs_dir.exists():
            ref_images = sorted(
                p.name for p in refs_dir.iterdir()
                if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))
            rj = refs_dir / "refs.json"
            if rj.exists():
                try:
                    data = json.loads(rj.read_text(encoding="utf-8"))
                    vmap = data.get("variants") or {}
                    for label, f in vmap.items():
                        if (refs_dir / f).exists():
                            variants.append({"label": label, "image": f})
                except Exception:
                    variants = []
            from .storyboard import costume_variant_payload
            costumes = costume_variant_payload(show, char_id, content.get("name", ""))
        voice_file = ""
        vp = show.dir / "assets" / "voice" / f"{char_id}_voice.wav"
        if vp.exists():
            voice_file = vp.name
        chars.append({**cs, "content": content, "ref_images": ref_images,
                      "variants": variants, "costumes": costumes,
                      "voice_file": voice_file})
    scenes: dict[str, Any] = {}
    for sid in show.list_scenes():
        data = _read_yaml(show.scenes_dir / f"{sid}.yaml")
        rd = show.scenes_dir / sid / "refs"
        data["ref_images"] = (sorted(p.name for p in rd.iterdir()
                                     if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))
                              if rd.exists() else [])
        scenes[sid] = data
    runs = show.dir / "runs"
    existing = sorted(int(d.name[2:]) for d in runs.iterdir()
                      if d.is_dir() and d.name.startswith("EP") and d.name[2:].isdigit()) \
        if runs.exists() else []
    # First missing episode number (fills gaps from deletions).
    next_ep = 1
    for n in existing:
        if n == next_ep:
            next_ep += 1
        else:
            break
    return {
        "show_id": show_id,
        "bootstrap": bs,
        "concept": _read_json(show.concept_path),
        "bible": _read_yaml(show.bible_path),
        "characters": chars,
        "scenes": scenes,
        "plotlines": _plotline_payload(show),
        "seasons": _seasons_payload(show),
        "next_episode": next_ep,
    }


def _seasons_payload(show: Show) -> list[dict[str, Any]]:
    """Summaries of every season plan for the UI (status + episode slots)."""
    out: list[dict[str, Any]] = []
    for sno in show.list_seasons():
        data = show.read_season(sno)
        eps = []
        for e in data.get("episodes", []) or []:
            if not isinstance(e, dict):
                continue
            eps.append({
                "episode": e.get("episode"),
                "title": e.get("title", ""),
                "episode_type": e.get("episode_type", "character"),
                "logline": e.get("logline", ""),
                "featured_plotlines": e.get("featured_plotlines", []) or [],
                "new_plotline": e.get("new_plotline"),
                "key_beats": e.get("key_beats", []) or [],
                "cliffhanger": e.get("cliffhanger", ""),
            })
        out.append({
            "season": data.get("season", sno),
            "title": data.get("title", ""),
            "status": data.get("status", "pending"),
            "num_episodes": data.get("num_episodes", len(eps)),
            "start_episode": data.get("start_episode", sno),
            "season_arc": data.get("season_arc", {}) or {},
            "episodes": eps,
        })
    return out


def _plotline_payload(show: Show) -> list[dict[str, Any]]:
    """Merge the bible's plotline seeds with the live arc/cooldown bookkeeping
    from continuity so the UI can show each plotline's episode allocation."""
    from .development import _plotline_state
    bible = show.read_bible()
    overall = bible.get("overall_plotline") or {}
    lines = _plotline_state(show, bible)
    out: list[dict[str, Any]] = []
    for p in lines:
        out.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "kind": p.get("kind", "character"),
            "status": p.get("status", "active"),
            "characters": p.get("characters", []) or [],
            "summary": p.get("summary", ""),
            "last_seen_episode": p.get("last_seen_episode", 0),
            "arc_remaining": int(p.get("arc_remaining", 0) or 0),
            "cooldown_remaining": int(p.get("cooldown_remaining", 0) or 0),
        })
    out.sort(key=lambda p: (p["id"] == overall.get("id"), p["name"] or ""))
    return out


def episodes_payload(show_id: str) -> list[dict[str, Any]]:
    show = Show(show_id)
    runs = show.dir / "runs"
    out = []
    if runs.exists():
        for d in sorted(runs.iterdir()):
            if d.is_dir() and d.name.startswith("EP"):
                out.append(episode_payload(show_id, d.name))
    return out


def episode_payload(show_id: str, ep: str) -> dict[str, Any]:
    show = Show(show_id)
    d = show.dir / "runs" / ep
    ep_num = int(ep.replace("EP", "") or 1)
    scripts = fsutil.existing_by_mtime(d.glob("script.r*.json"))
    latest = scripts[-1] if scripts else None
    script = _read_json(latest) if latest else None
    reviews: dict[str, Any] = {}
    rdir = d / "reviews"
    if rdir.exists():
        # Group review files by reviewer name (drop the ".rN" round suffix) so the
        # UI can show EVERY round's pass/fail history per reviewer, not just the
        # last file. e.g. structure.r1.json..structure.r4.json -> "structure" with
        # 4 round entries.
        import re as _re
        for f in rdir.glob("*.json"):
            m = _re.match(r"^(.*)\.(pr|r)(\d+)$", f.stem)
            reviewer = m.group(1) if m else f.stem
            round_no = int(m.group(3)) if m else 0
            is_prose = bool(m and m.group(2) == "pr")
            data = _read_json(f)
            entry = reviews.setdefault(reviewer, {"rounds": []})
            entry["rounds"].append({"round": round_no, **{
                k: data.get(k) for k in ("score", "pass", "summary", "notes", "maturity_score")
                if k in data}, **({"prose": True} if is_prose else {})})
        # Sort each reviewer's rounds ascending and expose latest + best.
        for reviewer, entry in reviews.items():
            entry["rounds"].sort(key=lambda r: r.get("round", 0))
            entry["latest"] = entry["rounds"][-1] if entry["rounds"] else {}
            entry["passed_any"] = any(r.get("pass") is True for r in entry["rounds"])
            entry["passed_latest"] = entry["latest"].get("pass") is True

    # Per-shot STT dialogue QC (garbled-audio detection), keyed by shot id.
    qc_shots: dict[str, Any] = {}
    qc_rollup: dict[str, Any] = {"pass": 0, "fail": 0, "skip": 0, "total": 0}
    qc_summary_path = d / "qc" / "summary.json"
    if qc_summary_path.exists():
        try:
            qc_data = json.loads(qc_summary_path.read_text(encoding="utf-8")) or {}
        except Exception:
            qc_data = {}
        for entry in qc_data.values():
            if not isinstance(entry, dict):
                continue
            sid = entry.get("shot_id", "")
            qc_shots[sid] = {"verdict": entry.get("verdict", "fail"),
                             "score": entry.get("score", 0.0),
                             "transcript": entry.get("transcript") or "",
                             "expected": entry.get("expected", []),
                             "issues": entry.get("issues", []),
                             "silent_shot": bool(entry.get("silent_shot"))}
            qc_rollup["total"] += 1
            v = entry.get("verdict", "fail")
            if v in qc_rollup:
                qc_rollup[v] += 1
            else:
                qc_rollup["fail"] += 1

    # Per-shot human approval (Shot gate): status + rejection notes/retake count.
    from .approval import shot_approvals
    shot_approval = shot_approvals(show, ep_num)

    # Per-shot post-render visual check (Qwen3-VL), keyed by shot id.
    vcheck_shots: dict[str, Any] = {}
    vcheck_rollup: dict[str, Any] = {"pass": 0, "fail": 0, "skip": 0, "total": 0}
    vcheck_summary_path = d / "vcheck" / "summary.json"
    if vcheck_summary_path.exists():
        try:
            vcheck_data = json.loads(vcheck_summary_path.read_text(encoding="utf-8")) or {}
        except Exception:
            vcheck_data = {}
        for entry in vcheck_data.values():
            if not isinstance(entry, dict):
                continue
            sid = entry.get("shot_id", "")
            vcheck_shots[sid] = {
                "verdict": entry.get("verdict", "skip"),
                "issues": entry.get("issues", []),
                "characters": entry.get("characters", []),
                "unknown_characters": entry.get("unknown_characters", []),
                "action_match": bool(entry.get("action_match")),
                "action_issue": entry.get("action_issue", ""),
                "text_defect": bool(entry.get("text_defect")),
            }
            vcheck_rollup["total"] += 1
            v = entry.get("verdict", "skip")
            if v in vcheck_rollup:
                vcheck_rollup[v] += 1
            else:
                vcheck_rollup["fail"] += 1

    scenes_out: list[dict[str, Any]] = []
    total_shots = 0
    dialogue_lines = 0
    hero_shots = 0

    def _shot_payload(shot: dict[str, Any]) -> dict[str, Any]:
        sid = shot.get("id", "")
        keyframe = d / "storyboard" / f"{sid}.png"
        preview = d / "storyboard" / f"{sid}.mp4"
        ap = shot_approval.get(sid, {})
        # Auto-rejected shots have their vcheck report cleared (so a retake gets
        # re-reviewed), which would erase the flag from the UI. Re-surface a
        # `vcheck` display entry from the stored rejection notes when the shot
        # was flagged by the post-render visual check, so the user can still see
        # WHAT failed and why while the retake re-renders.
        vcheck = vcheck_shots.get(sid, {})
        if not vcheck and ap.get("status") == "rejected":
            notes = (ap.get("notes") or "")
            if notes.startswith("Post-render visual check"):
                issues = [l.strip() for l in notes.split("Post-render visual check:")[-1].split("\n")
                          if l.strip()] or [notes]
                vcheck = {"verdict": "fail", "issues": issues, "retaking": True}
        return {
            "id": sid,
            "type": shot.get("type", ""),
            "importance": shot.get("importance", ""),
            "duration_s": shot.get("duration_s", 0),
            "camera": shot.get("camera", ""),
            "action": shot.get("action", ""),
            "dialogue": shot.get("dialogue", []) or [],
            "costumes": (shot.get("references") or {}).get("costumes", {}) or {},
            "keyframe": keyframe.name if keyframe.exists() else "",
            "preview": preview.name if preview.exists() else "",
            "qc": qc_shots.get(sid, {}),
            "vcheck": vcheck,
            "approval": {"status": ap.get("status", "pending"),
                         "notes": ap.get("notes", ""),
                         "retake_count": ap.get("retake_count", 0),
                         "revised": bool(ap.get("revised_prompt"))},
        }

    def _scene_payload(sc: dict[str, Any], shots: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "id": sc.get("id", ""),
            "location": sc.get("location", ""),
            "time_of_day": sc.get("time_of_day", ""),
            "summary": sc.get("summary", ""),
            "shots": [_shot_payload(s) for s in shots],
        }

    # While a script is being written, the assembled script only lands at the
    # END of the run — but each scene's shot checkpoint (scenes/<sid>_shots.json)
    # appears the moment that scene finishes. Populate the list from those
    # checkpoints so the user sees the shots appear scene-by-scene, and expose a
    # per-scene pass-progress map so the current scene's stage is visible.
    writing: dict[str, Any] = {}
    if script:
        for sc in (script or {}).get("scenes", []):
            scenes_out.append(_scene_payload(sc, sc.get("shots", [])))
    else:
        import re as _re
        from .planner import _SCENE_PASSES, read_episode_plan
        plan = read_episode_plan(show, ep_num)
        plan_scenes = [s for s in (plan.get("scenes") or [])
                       if isinstance(s, dict) and s.get("id")]
        sc_dir = d / "scenes"
        for ps in plan_scenes:
            sid = ps.get("id", "")
            shots: list[dict[str, Any]] = []
            ck = sc_dir / f"{sid}_shots.json"
            if ck.exists():
                try:
                    shots = json.loads(ck.read_text(encoding="utf-8")).get("shots") or []
                except Exception:
                    shots = []
            if shots:
                scenes_out.append(_scene_payload(ps, shots))
                writing[sid] = {"pass": "done", "shots": len(shots)}
                continue
            # Only report a scene as "writing" if actual shot work exists (a
            # partial pass checkpoint). A pending plan with no writing started
            # must NOT show a progress chip — the episode is blocked on approval,
            # not mid-write.
            best = -1
            try:
                for f in sc_dir.glob(f"{sid}.p*.json"):
                    m = _re.fullmatch(_re.escape(sid) + r"\.p(\d+)\.json", f.name)
                    if m:
                        best = max(best, int(m.group(1)))
            except Exception:
                pass
            if best >= 0:
                writing[sid] = {"pass": _SCENE_PASSES[best]["name"]
                                if 0 <= best < len(_SCENE_PASSES) else "",
                                "shots": 0}
    total_shots = sum(len(sc["shots"]) for sc in scenes_out)
    hero_shots = sum(1 for sc in scenes_out for s in sc["shots"]
                     if s.get("importance") == "hero")
    dialogue_lines = sum(len(s.get("dialogue") or []) for sc in scenes_out
                         for s in sc["shots"])

    objects: list[dict[str, Any]] = []
    od = d / "objects"
    if od.exists():
        from .storyboard import _approved_object_slugs
        from .vcheck import _object_vcheck_summary
        approved = _approved_object_slugs(show, ep_num)
        obj_vcheck = _object_vcheck_summary(show, ep_num)
        for f in sorted(od.glob("*.png")):
            vcheck_entry = obj_vcheck.get(f.stem, {})
            objects.append({"name": f.stem.replace("-", " ").title(),
                            "slug": f.stem, "image": f.name,
                            "approved": f.stem in approved,
                            "vcheck": vcheck_entry})

    videos: dict[str, str] = {}
    vd = d / "video"
    if vd.exists():
        for f in sorted(vd.glob("*.mp4")):
            videos[f.stem] = f.name

    # Episode cast: every character the script's cast lists, with their ref image.
    cast: list[dict[str, Any]] = []
    if script:
        from .storyboard import _approved_costume_labels
        char_by_name = {}
        for cid in show.list_characters():
            try:
                c = show.read_character(cid)
            except Exception:
                continue
            if c.get("name"):
                char_by_name[c["name"]] = (cid, c)
        for name in script.get("cast", []) or []:
            if name not in char_by_name:
                continue
            cid, c = char_by_name[name]
            ref_img = ""
            variants: list[dict[str, Any]] = []
            rd = show.character_refs_dir(cid)
            rj = rd / "refs.json"
            if rj.exists():
                try:
                    data = json.loads(rj.read_text(encoding="utf-8"))
                    variants_map = data.get("variants") or {}
                    approved = set(_approved_costume_labels(show, cid))
                    for label, f in variants_map.items():
                        if (rd / f).exists():
                            variants.append({"label": label, "image": f,
                                             "approved": label in approved})
                            if label == "base":
                                ref_img = f
                except Exception:
                    pass
            cast.append({"name": name, "id": cid,
                         "role": c.get("role", ""), "image": ref_img,
                         "variants": variants})

    from .storyboard import storyboard_status
    from .planner import read_episode_plan, read_scene_details
    from .episode_repair import is_episode_paused
    sb = storyboard_status(show_id)
    cjson = d / "consistency.json"
    consistency = _read_json(cjson) if cjson.exists() else (sb.get("report") or [])
    # Final verdict per shot = the last round's entry.
    final_cons: dict[str, Any] = {}
    for entry in consistency:
        if entry.get("shot"):
            final_cons[entry["shot"]] = entry
    consistency = list(final_cons.values())
    plan = read_episode_plan(show, ep_num)
    scene_details = read_scene_details(show, ep_num)
    from .storyboard import pending_ref_approvals
    # Full human-readable story artifacts (never truncated): the whole-episode
    # prose screenplay and the whole-episode dialogue script, when they exist.
    # Debugging needs the actual words, not counts.
    def _read_txt(name: str) -> str:
        try:
            p = d / name
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            return ""

    def _episode_files() -> list[dict[str, Any]]:
        """Every file under runs/EP## (recursive): the artifacts browser.

        Includes prompts/ (LLM inputs), reviews/, scenes/ checkpoints, scripts,
        storyboard media, QC reports — everything the pipeline wrote, so the
        director can open any step's input or output from one place.
        """
        out: list[dict[str, Any]] = []
        if not d.exists():
            return out
        try:
            for p in sorted(d.rglob("*")):
                if not p.is_file() or len(out) >= 2000:
                    continue
                try:
                    st = p.stat()
                    out.append({"path": f"runs/{ep}/" + str(p.relative_to(d)).replace("\\", "/"),
                                "bytes": st.st_size, "mtime": st.st_mtime})
                except Exception:
                    continue
        except Exception:
            pass
        return out
    return {
        "episode": ep,
        "script_file": latest.name if latest else None,
        "script": script,
        "episode_prose": _read_txt("episode_prose.txt"),
        "dialogue_txt": _read_txt("dialogue.txt"),
        "files": _episode_files(),
        "reviews": reviews,
        "story": approval.story_state(show_id, ep),
        "pending_refs": pending_ref_approvals(show, ep_num, script),
        "writing": writing,
        "summary": {
            "scenes": len(scenes_out),
            "shots": total_shots,
            "dialogue_lines": dialogue_lines,
            "hero_shots": hero_shots,
            "episode": (script or {}).get("summary", "") or "",
        },
        "scenes": scenes_out,
        "objects": objects,
        "cast": cast,
        "videos": videos,
        "render": _render_status(show_id),
        "storyboard": sb,
        "qc": qc_rollup,
        "vcheck": vcheck_rollup,
        "paused": is_episode_paused(show_id, ep_num),
        "consistency": consistency,
        "plan": plan,
        "scene_details": scene_details,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "StudioDashboard/1.0"

    # -- helpers -----------------------------------------------------------

    def _send(self, code: int, obj: Any) -> None:
        if isinstance(obj, (dict, list)):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            ctype = "application/json"
        else:
            body = str(obj).encode("utf-8")
            ctype = "text/plain"
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _path_parts(self) -> list[str]:
        return [p for p in urlparse(self.path).path.split("/") if p]

    def _serve_file(self, path: Path) -> None:
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".wav": "audio/wav", ".mp3": "audio/mpeg",
            ".mp4": "video/mp4",
        }.get(path.suffix.lower(), "application/octet-stream")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:
        parts = self._path_parts()
        if not parts:
            self._send_html()
            return
        if parts == ["api", "shows"]:
            self._send(200, {"shows": [
                {"id": s, "bootstrap": Show(s).bootstrap_state()} for s in get_config().list_shows()
            ]})
            return
        if len(parts) == 3 and parts[:2] == ["api", "show"]:
            # Self-healing: resume a stalled Gate-0 chain OR a stalled episode
            # pipeline in the background so nothing waits silently on a crash.
            from .bootstrap import reconcile_if_stalled
            from .episode_repair import reconcile_episodes_if_stalled
            reconcile_if_stalled(parts[2])
            reconcile_episodes_if_stalled(parts[2])
            self._send(200, show_payload(parts[2]))
            return
        if len(parts) >= 5 and parts[:2] == ["api", "show"] and parts[3] == "media":
            # /api/show/<id>/media/<relpath under the show dir> (ref images, voice samples)
            root = Show(parts[2]).dir.resolve()
            rel = "/".join(parts[4:])
            p = (root / rel).resolve()
            if (p != root and root in p.parents) and p.is_file():
                self._serve_file(p)
                return
            self._send(404, {"error": "media not found"})
            return
        if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "activity":
            from .bootstrap import (ACTIVITY, reconcile_if_stalled,
                                    is_bootstrap_reconciling)
            from .episode_repair import (reconcile_episodes_if_stalled,
                                         is_episode_reconciling)
            from .storyboard import storyboard_status
            from .render import render_status
            from .planner import read_episode_plan, read_scene_details
            reconcile_if_stalled(parts[2])
            reconcile_episodes_if_stalled(parts[2])
            a = ACTIVITY.get(parts[2])
            sb = storyboard_status(parts[2])
            rd = render_status(parts[2])
            # A show is only "active" while a REAL job thread is alive:
            #  - a storyboard job running,
            #  - a render job running,
            #  - a Gate-0 reconcile thread alive, or
            #  - an episode-reconcile thread alive.
            # ACTIVITY alone is NOT proof of work: any thread can write it, and a
            # finished/crashed job leaves a stale entry that would otherwise pin
            # the UI to "Generating…" forever. Only surface its detail while a
            # genuine job is in flight.
            job_alive = (
                sb.get("state") == "running"
                or rd.get("state") == "running"
                or is_bootstrap_reconciling(parts[2])
                or is_episode_reconciling(parts[2])
            )
            if job_alive:
                detail = (a or {}).get("detail") or sb.get("detail") or rd.get("detail") or "Working…"
                active = True
                blocked = False
                blocked_detail = ""
            else:
                detail = ""
                active = False
                if a:
                    ACTIVITY.pop(parts[2], None)   # clear orphaned detail
                # Blocked on a human gate? Check the latest episode's plan /
                # scene-details status. If pending and nothing is generating, the
                # pipeline is WAITING ON YOU — surface that as a distinct state so
                # the UI can show a red "waiting for approval" bar instead of
                # silence.
                try:
                    show_dir = Show(parts[2]).dir / "runs"
                    eps = sorted((int(d.name[2:]) for d in show_dir.iterdir()
                                  if d.is_dir() and d.name.startswith("EP")
                                  and d.name[2:].isdigit()),
                                 reverse=True) if show_dir.exists() else []
                    blocked = False
                    blocked_detail = ""
                    for ep in eps:
                        plan = read_episode_plan(Show(parts[2]), ep)
                        if (plan or {}).get("status") == "pending":
                            review = (plan or {}).get("plan_review") or {}
                            if review.get("passed") is False:
                                # The outline FAILED the structure review. It is
                                # not simply awaiting a yes/no — the reviewers
                                # rejected it and it needs revision (approve is a
                                # manual override, not a clean pass).
                                blocked = True
                                blocked_detail = (f"EP{ep:02d}: outline FAILED review "
                                                  f"({review.get('rounds', 1)} rounds, "
                                                  f"{len(review.get('notes') or [])} notes) — "
                                                  "revise it or approve as an override")
                            else:
                                blocked = True
                                blocked_detail = f"EP{ep:02d}: outline waiting for your approval"
                            break
                        details = read_scene_details(Show(parts[2]), ep)
                        if (details or {}).get("status") == "pending":
                            blocked = True
                            blocked_detail = f"EP{ep:02d}: scene details waiting for your approval"
                            break
                        # Script review gate: writers' room review failed and
                        # storyboard is blocked until a human overrides.
                        from .storyboard import _latest_script as _ls
                        script = _ls(Show(parts[2]), ep)
                        if script:
                            script_review = script.get("_review") or {}
                            if script_review.get("passed") is False:
                                blocked = True
                                blocked_detail = (f"EP{ep:02d}: script FAILED review "
                                                  f"({script_review.get('rounds', 1)} rounds, "
                                                  f"{len(script_review.get('notes') or [])} notes) — "
                                                  "revise it or approve as an override")
                                break
                except Exception:
                    blocked = False
                    blocked_detail = ""
            self._send(200, {"active": active, "detail": detail, "output": (a or {}).get("output", ""),
                             "blocked": blocked, "blocked_detail": blocked_detail,
                             "storyboard": sb, "render": rd})
            return
        if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "episodes":
            self._send(200, {"episodes": episodes_payload(parts[2])})
            return
        if parts == ["api", "proposals"]:
            self._send(200, {"sets": _list_proposal_sets()})
            return
        if parts == ["api", "llm-status"]:
            # Which LLM side serves generation right now — so a missing key in
            # the SERVER process can never silently route to local again. The
            # key is read from the opencode auth.json (never the environment).
            from .clients.lmstudio import LMStudioClient as _LLM
            from .config import resolve_zen_api_key
            cfg = get_config()
            z = cfg.get("llm", "zen", {}) or {}
            key_present = bool(resolve_zen_api_key(cfg))
            remote = _LLM(cfg.get("llm", "base_url"))._zen_settings() is not None
            if remote:
                reason = "ok"
            elif not z.get("enabled"):
                reason = "disabled in config (llm.zen.enabled=false)"
            elif not key_present:
                reason = ("no key in the opencode auth.json — sign in with "
                          "`opencode auth login`")
            else:
                reason = "no base_url configured"
            self._send(200, {
                "remote": remote,
                "model": z.get("model", ""),
                "vision_model": (z.get("vision", {}) or {}).get("model", ""),
                "base_url": z.get("base_url", ""),
                "key_present": key_present,
                "reason": reason,
            })
            return
        if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "seasons":
            self._send(200, {"seasons": _seasons_payload(Show(parts[2]))})
            return
        if len(parts) == 5 and parts[:2] == ["api", "show"] and parts[3] == "season":
            try:
                sno = int(parts[4].lstrip("S"))
            except ValueError:
                self._send(400, {"error": "invalid season"})
                return
            self._send(200, {"season": Show(parts[2]).read_season(sno)})
            return
        if len(parts) == 5 and parts[:2] == ["api", "show"] and parts[3] == "ep":
            self._send(200, episode_payload(parts[2], parts[4]))
            return
        if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "file":
            from urllib.parse import parse_qs, urlparse as _up
            q = parse_qs(_up(self.path).query)
            rel = (q.get("path") or [""])[0]
            data = _read_show_file(Show(parts[2]), rel)
            if data is None:
                self._send(404, {"error": "file not found or outside show dir"})
                return
            self._send(200, data)
            return
        if parts == ["api", "config"]:
            self._send(200, {"sections": _EDITABLE_CONFIG_SECTIONS})
            return
        if len(parts) == 3 and parts[:2] == ["api", "config"]:
            data = _config_section_payload(parts[2])
            if data is None:
                self._send(404, {"error": f"unknown config section: {parts[2]}"})
                return
            self._send(200, data)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        parts = self._path_parts()
        body = self._read_body()
        try:
            if len(parts) == 3 and parts[:2] == ["api", "config"]:
                # Settings tab: write back config/<section>.yaml (merged patch or
                # a full raw YAML replacement), then reload the config singleton.
                try:
                    if "yaml" in body:
                        data = _write_config_yaml(parts[2], str(body.get("yaml") or ""))
                    else:
                        data = _write_config_section(parts[2], body.get("values") or {})
                except ValueError as exc:
                    self._send(200, {"ok": False, "error": str(exc)})
                    return
                self._send(200, {"ok": True, **data})
                return
            if parts == ["api", "proposals"]:
                proposals = propose_concepts(guidance=body.get("guidance", ""),
                                             n=int(body.get("n", 3) or 3))
                try:
                    saved_as = _save_proposal_set(body.get("guidance", ""), proposals)
                except Exception:
                    saved_as = ""
                self._send(200, {"ok": True, "proposals": proposals,
                                 "saved_as": saved_as})
                return
            if parts == ["api", "shows"]:
                proposal = body.get("proposal")
                if proposal:
                    show_id = create_show_from_proposal(proposal, name=body.get("name", ""))
                else:
                    show_id = create_show(body.get("name", ""), brief=body.get("brief", ""),
                                          auto=bool(body.get("auto")))
                self._send(200, {"ok": True, "show_id": show_id})
                return
            if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "approve":
                msg = approval.approve_step(parts[2], body.get("step", ""),
                                            char=body.get("char", ""), notes=body.get("notes", ""),
                                            costume=body.get("costume", ""),
                                            slug=body.get("slug", ""),
                                            episode=body.get("episode", ""))
                self._send(200, {"ok": True, "messages": msg})
                return
            if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "reject":
                msg = approval.reject_step(parts[2], body.get("step", ""),
                                           char=body.get("char", ""),
                                           notes=body.get("notes", "no notes"),
                                           costume=body.get("costume", ""),
                                           mode=body.get("mode", "edit"),
                                           slug=body.get("slug", ""),
                                           episode=body.get("episode", ""))
                self._send(200, {"ok": True, "messages": msg})
                return
            if len(parts) == 5 and parts[:2] == ["api", "show"] and parts[3] == "shot":
                # /api/show/<id>/shot/<action> — per-shot approve/reject (Gate 4).
                show_id, shot_action = parts[2], parts[4]
                if shot_action in ("approve", "reject"):
                    ep = int(body.get("episode", 1) or 1)
                    shot_id = (body.get("shot") or "").strip()
                    notes = body.get("notes", "no notes")
                    if not shot_id:
                        self._send(400, {"ok": False, "error": "shot required"})
                        return
                    show_obj = Show(show_id)
                    if shot_action == "approve":
                        msg = approval.approve_shot(show_obj, ep, shot_id, notes)
                        self._send(200, {"ok": True, "messages": msg})
                    else:
                        msg = approval.reject_shot(show_obj, ep, shot_id, notes)
                        self._send(200, {"ok": True, "messages": msg})
                    return
            if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "generate":
                from .bootstrap import ACTIVITY
                from .clients.lmstudio import LMStudioClient
                from .planner import generate_episode_plan
                ep = int(body.get("episode", 1) or 1)
                show_id = parts[2]
                show = Show(show_id)
                llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=300)
                try:
                    plan = generate_episode_plan(show, ep, llm=llm)
                    n = len(plan.get("scenes", []))
                    self._send(200, {"ok": True, "messages": [
                        f"episode {ep}: plan generated ({n} scenes) — review & approve it "
                        "to write the shots"]})
                except Exception as exc:
                    self._send(200, {"ok": False, "error": str(exc)})
                finally:
                    ACTIVITY.pop(show_id, None)
                return
            if len(parts) == 5 and parts[:2] == ["api", "show"] and parts[3] == "plan" \
                    and parts[4] == "reject":
                from .bootstrap import ACTIVITY
                from .clients.lmstudio import LMStudioClient
                from .planner import (add_director_note, generate_episode_plan,
                                      reject_plan)
                ep = int(body.get("episode", 1) or 1)
                show_id = parts[2]
                show = Show(show_id)
                notes = body.get("notes", "no notes")
                reject_plan(show, ep, notes)
                llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=300)
                add_director_note(show, notes)
                try:
                    generate_episode_plan(show, ep, llm=llm, notes=notes)
                    self._send(200, {"ok": True, "messages": [
                        "outline rejected — regenerated from your notes"]})
                except Exception as exc:
                    self._send(200, {"ok": False, "error": str(exc)})
                finally:
                    ACTIVITY.pop(show_id, None)
                return
            if len(parts) == 5 and parts[:2] == ["api", "show"] and parts[3] == "plan" \
                    and parts[4] == "approve":
                from .bootstrap import ACTIVITY
                from .planner import approve_plan
                ep = int(body.get("episode", 1) or 1)
                show_id = parts[2]
                show = Show(show_id)
                # override=True lets a human approve a reviewer-rejected outline.
                approve_plan(show, ep, override=bool(body.get("override")))
                self._send(200, {"ok": True, "messages": [
                    "plan approved — writing the detailed scene section (background)…"]})
                ACTIVITY.pop(show_id, None)

                def _details():
                    try:
                        from .planner import generate_scene_details
                        generate_scene_details(show, ep)
                    except Exception as exc:
                        ACTIVITY[show_id] = {"detail": f"Scene details failed: {exc}",
                                             "ts": time.time()}
                    finally:
                        ACTIVITY.pop(show_id, None)
                import threading
                threading.Thread(target=_details, daemon=True).start()
                return
            if len(parts) == 6 and parts[:2] == ["api", "show"] and parts[3] == "scene-details":
                from .bootstrap import ACTIVITY
                ep = int(body.get("episode", 1) or 1)
                show_id = parts[2]
                show = Show(show_id)
                action = parts[4] or parts[5]
                if action == "approve":
                    from .episode_repair import hold_manual_rebuild, release_manual_rebuild
                    from .planner import approve_scene_details
                    from .storyboard import build_storyboard
                    # Hold the reconciler so it cannot race the manual build
                    # thread (both would assemble + build the storyboard at once).
                    hold_manual_rebuild(show_id, ep)
                    approve_scene_details(show, ep)
                    self._send(200, {"ok": True, "messages": [
                        "scene details approved — writing shots + storyboard (background)…"]})
                    ACTIVITY.pop(show_id, None)

                    def _build():
                        try:
                            from .planner import assemble_episode_script
                            assemble_episode_script(show, ep)
                            build_storyboard(show, ep)
                        except Exception as exc:
                            ACTIVITY[show_id] = {"detail": f"Episode build failed: {exc}",
                                                 "ts": time.time()}
                        finally:
                            release_manual_rebuild(show_id, ep)
                            ACTIVITY.pop(show_id, None)
                    import threading
                    threading.Thread(target=_build, daemon=True).start()
                    return
                if action == "reject":
                    from .clients.lmstudio import LMStudioClient
                    from .planner import generate_scene_details, reject_scene_details
                    notes = body.get("notes", "no notes")
                    reject_scene_details(show, ep, notes)
                    llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=300)
                    try:
                        generate_scene_details(show, ep, llm=llm, notes=notes)
                        self._send(200, {"ok": True, "messages": [
                            "scene details rejected — regenerating with your notes"]})
                    except Exception as exc:
                        self._send(200, {"ok": False, "error": str(exc)})
                    finally:
                        ACTIVITY.pop(show_id, None)
                    return
            if len(parts) == 5 and parts[:2] == ["api", "show"] and parts[3] == "shots" \
                    and parts[4] == "regenerate":
                from .clients.lmstudio import LMStudioClient
                from .episode_repair import hold_manual_rebuild, release_manual_rebuild
                from .planner import assemble_episode_script, clear_scene_shots
                ep = int(body.get("episode", 1) or 1)
                show_id = parts[2]
                show = Show(show_id)
                notes = body.get("notes", "")
                # Hold the reconciler so it does not race the manual rebuild
                # (both would run assemble + build_storyboard concurrently and
                # fight over the exclusive GPU lock). Released when the rebuild
                # thread finishes.
                hold_manual_rebuild(show_id, ep)
                clear_scene_shots(show, ep)
                self._send(200, {"ok": True, "messages": [
                    "shots cleared — rebuilding from the approved scene details (background)…"]})
                ACTIVITY.pop(show_id, None)

                def _rebuild():
                    try:
                        llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=300)
                        from .storyboard import build_storyboard
                        assemble_episode_script(show, ep, llm=llm, notes=notes)
                        build_storyboard(show, ep)
                    except Exception as exc:
                        ACTIVITY[show_id] = {"detail": f"Shot regen failed: {exc}",
                                             "ts": time.time()}
                    finally:
                        release_manual_rebuild(show_id, ep)
                        ACTIVITY.pop(show_id, None)
                import threading
                threading.Thread(target=_rebuild, daemon=True).start()
                return
            if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "storyboard":
                from .storyboard import build_storyboard, storyboard_blockers
                ep = int(body.get("episode", 1) or 1)
                blockers = storyboard_blockers(Show(parts[2]), ep)
                if blockers:
                    self._send(400, {"ok": False, "error":
                                     "cannot build storyboard: " + "; ".join(blockers)})
                    return
                build_storyboard(Show(parts[2]), ep)
                self._send(200, {"ok": True,
                                 "messages": ["storyboard generation started (background)"]})
                return
            if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "consistency":
                from .consistency import run_consistency_check
                from .clients.lmstudio import LMStudioClient
                ep = int(body.get("episode", 1) or 1)
                show_id = parts[2]
                llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=240)
                report = run_consistency_check(Show(show_id), ep, llm=llm)
                fails = [r for r in report if r.get("result") in ("failed",)]
                self._send(200, {"ok": True,
                                 "messages": [f"consistency: {len(report)} shots reviewed, "
                                              f"{len(fails)} failed"]})
                return
            if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "render":
                from .render import build_render
                ep = int(body.get("episode", 1) or 1)
                build_render(Show(parts[2]), ep)
                self._send(200, {"ok": True,
                                 "messages": ["video render started (H3, background)"]})
                return
            if len(parts) == 6 and parts[:2] == ["api", "show"] and parts[3] == "ep":
                show_id, ep, action = parts[2], parts[4], parts[5]
                if action == "approve":
                    msg = approval.approve_story(show_id, ep, body.get("notes", ""))
                    self._send(200, {"ok": True, "messages": msg})
                    return
                if action == "reject":
                    msg = approval.reject_story(show_id, ep, body.get("notes", "no notes"))
                    self._send(200, {"ok": True, "messages": msg})
                    return
                if action == "script-approve":
                    msg = approval.approve_script(show_id, ep, body.get("notes", ""))
                    self._send(200, {"ok": True, "messages": msg})
                    return
                if action == "stop":
                    from .episode_repair import pause_episode
                    from .storyboard import stop_storyboard
                    pause_episode(show_id, int(ep.replace("EP", "")))
                    stop_storyboard(show_id)  # cooperative stop of running previews
                    self._send(200, {"ok": True, "messages": [f"{ep} paused"]})
                    return
                if action == "resume":
                    from .episode_repair import resume_episode
                    resume_episode(show_id, int(ep.replace("EP", "")))
                    self._send(200, {"ok": True, "messages": [f"{ep} resumed"]})
                    return
            if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "file":
                rel = (body.get("path") or "").strip()
                content = body.get("content")
                if not rel or not isinstance(content, str):
                    self._send(400, {"ok": False, "error": "path and content required"})
                    return
                self._send(200, _write_show_file(Show(parts[2]), rel, content))
                return
            if len(parts) == 5 and parts[:2] == ["api", "show"] and parts[3] == "season" \
                    and parts[4] == "generate":
                from .bootstrap import ACTIVITY
                from .clients.lmstudio import LMStudioClient
                from .season import default_num_episodes, generate_season_plan
                show_id = parts[2]
                show = Show(show_id)
                season = int(body.get("season", 1) or 1)
                num_ep = int(body.get("num_episodes", 0) or 0) or default_num_episodes()
                feedback = body.get("feedback", "") or body.get("notes", "") or ""
                llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=300)
                try:
                    data = generate_season_plan(
                        show, season, num_ep, llm=llm,
                        feedback=feedback,
                        start_episode=(season - 1) * (num_ep or default_num_episodes()) + 1)
                    n = len(data.get("episodes", []))
                    self._send(200, {"ok": True, "messages": [
                        f"season {season}: planned {n} episodes — review & approve it; "
                        f"every episode below realizes its slot"]})
                except Exception as exc:
                    self._send(200, {"ok": False, "error": str(exc)})
                finally:
                    ACTIVITY.pop(show_id, None)
                return
            if len(parts) == 5 and parts[:2] == ["api", "show"] and parts[3] == "season" \
                    and parts[4] == "approve":
                from .season import approve_season
                show = Show(parts[2])
                season = int(body.get("season", 1) or 1)
                approve_season(show, season)
                self._send(200, {"ok": True, "messages": [
                    f"season {season} approved — episode outlines must realize their slots"]})
                return
            if len(parts) == 5 and parts[:2] == ["api", "show"] and parts[3] == "season" \
                    and parts[4] == "reject":
                from .season import reject_season
                show = Show(parts[2])
                season = int(body.get("season", 1) or 1)
                notes = body.get("notes", "no notes")
                reject_season(show, season, notes)
                self._send(200, {"ok": True, "messages": [
                    f"season {season} rejected — regenerate it with your notes"]})
                return
            if len(parts) == 5 and parts[:2] == ["api", "show"] and parts[3] == "costume" \
                    and parts[4] == "delete":
                from .storyboard import delete_costume
                show = Show(parts[2])
                cname = (body.get("char") or "").strip()
                label = (body.get("costume") or "").strip()
                if not cname or not label:
                    self._send(400, {"ok": False, "error": "char and costume required"})
                    return
                c = next((show.read_character(cid) for cid in show.list_characters()
                          if (show.read_character(cid).get("name") or "") == cname), None)
                if not c:
                    self._send(404, {"ok": False, "error": f"no character named {cname}"})
                    return
                delete_costume(show, c.get("id", ""), label)
                self._send(200, {"ok": True, "messages": [f"costume '{label}' deleted"]})
                return
            self._send(404, {"error": "not found"})
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface errors to the browser
            self._send(500, {"ok": False, "error": repr(exc)})

    def do_DELETE(self) -> None:
        parts = self._path_parts()
        try:
            if len(parts) == 5 and parts[:2] == ["api", "show"] \
                    and parts[3] == "ep":
                from .episode_repair import delete_episode
                msg = delete_episode(parts[2], parts[4])
                self._send(200, {"ok": True, "messages": msg})
                return
            if len(parts) == 3 and parts[:2] == ["api", "show"]:
                delete_show(parts[2])
                self._send(200, {"ok": True})
                return
            self._send(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"ok": False, "error": repr(exc)})

    def _send_html(self) -> None:
        try:
            body = _HTML.read_bytes()
        except OSError:
            self._send(500, "dashboard.html missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quieter access log
        import logging
        logging.getLogger("dashboard").info(fmt % args)


def serve(port: int = 8125, bind: str = "127.0.0.1") -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((bind, port), DashboardHandler)
    return httpd
