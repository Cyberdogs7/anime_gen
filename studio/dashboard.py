"""Local human approval & review dashboard (DESIGN.md §9.6).

A dependency-free HTTP server (stdlib only) that binds 127.0.0.1, serves a
single-page review surface, and exposes a small JSON API. Every approve/reject
action goes through :mod:`studio.approval`, so the pipeline sees the decision on
the approval bus stream exactly as if a consumer posted it.
"""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import approval
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
    except Exception:
        return None


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
        "next_episode": next_ep,
    }


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
    scripts = sorted(d.glob("script.r*.json"), key=lambda p: p.stat().st_mtime)
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
            m = _re.match(r"^(.*)\.r(\d+)$", f.stem)
            reviewer = m.group(1) if m else f.stem
            round_no = int(m.group(2)) if m else 0
            data = _read_json(f)
            entry = reviews.setdefault(reviewer, {"rounds": []})
            entry["rounds"].append({"round": round_no, **{
                k: data.get(k) for k in ("score", "pass", "summary", "notes", "maturity_score")
                if k in data}})
        # Sort each reviewer's rounds ascending and expose latest + best.
        for reviewer, entry in reviews.items():
            entry["rounds"].sort(key=lambda r: r.get("round", 0))
            entry["latest"] = entry["rounds"][-1] if entry["rounds"] else {}
            entry["passed_any"] = any(r.get("pass") is True for r in entry["rounds"])
            entry["passed_latest"] = entry["latest"].get("pass") is True

    scenes_out: list[dict[str, Any]] = []
    total_shots = 0
    dialogue_lines = 0
    hero_shots = 0

    def _shot_payload(shot: dict[str, Any]) -> dict[str, Any]:
        sid = shot.get("id", "")
        keyframe = d / "storyboard" / f"{sid}.png"
        preview = d / "storyboard" / f"{sid}.mp4"
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
        approved = _approved_object_slugs(show, ep_num)
        for f in sorted(od.glob("*.png")):
            objects.append({"name": f.stem.replace("-", " ").title(),
                            "slug": f.stem, "image": f.name,
                            "approved": f.stem in approved})

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
    return {
        "episode": ep,
        "script_file": latest.name if latest else None,
        "script": script,
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
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        parts = self._path_parts()
        body = self._read_body()
        try:
            if parts == ["api", "proposals"]:
                proposals = propose_concepts(guidance=body.get("guidance", ""),
                                             n=int(body.get("n", 3) or 3))
                self._send(200, {"ok": True, "proposals": proposals})
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
                from .storyboard import build_storyboard
                ep = int(body.get("episode", 1) or 1)
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
                if action == "stop":
                    from .episode_repair import pause_episode
                    pause_episode(show_id, int(ep.replace("EP", "")))
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
