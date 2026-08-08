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


def _read_yaml(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


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
        refs_dir = show.character_refs_dir(char_id)
        if refs_dir.exists():
            ref_images = sorted(
                p.name for p in refs_dir.iterdir()
                if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))
        voice_file = ""
        vp = show.dir / "assets" / "voice" / f"{char_id}_voice.wav"
        if vp.exists():
            voice_file = vp.name
        chars.append({**cs, "content": content, "ref_images": ref_images,
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
    n_runs = len([d for d in runs.iterdir() if d.is_dir() and d.name.startswith("EP")]) \
        if runs.exists() else 0
    return {
        "show_id": show_id,
        "bootstrap": bs,
        "concept": _read_json(show.concept_path),
        "bible": _read_yaml(show.bible_path),
        "characters": chars,
        "scenes": scenes,
        "next_episode": n_runs + 1,
    }


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
    scripts = sorted(d.glob("script.r*.json"), key=lambda p: p.stat().st_mtime)
    latest = scripts[-1] if scripts else None
    script = _read_json(latest) if latest else None
    reviews: dict[str, Any] = {}
    rdir = d / "reviews"
    if rdir.exists():
        for f in rdir.glob("*.json"):
            reviews[f.stem] = _read_json(f)

    scenes_out: list[dict[str, Any]] = []
    total_shots = 0
    dialogue_lines = 0
    hero_shots = 0
    for sc in (script or {}).get("scenes", []):
        shots_out = []
        for shot in sc.get("shots", []):
            total_shots += 1
            if shot.get("importance") == "hero":
                hero_shots += 1
            dialogue_lines += len(shot.get("dialogue", []) or [])
            sid = shot.get("id", "")
            keyframe = d / "storyboard" / f"{sid}.png"
            shots_out.append({
                "id": sid,
                "type": shot.get("type", ""),
                "importance": shot.get("importance", ""),
                "duration_s": shot.get("duration_s", 0),
                "camera": shot.get("camera", ""),
                "action": shot.get("action", ""),
                "dialogue": shot.get("dialogue", []) or [],
                "costumes": (shot.get("references") or {}).get("costumes", {}) or {},
                "keyframe": keyframe.name if keyframe.exists() else "",
            })
        scenes_out.append({
            "id": sc.get("id", ""),
            "location": sc.get("location", ""),
            "time_of_day": sc.get("time_of_day", ""),
            "summary": sc.get("summary", ""),
            "shots": shots_out,
        })

    objects: list[dict[str, Any]] = []
    od = d / "objects"
    if od.exists():
        for f in sorted(od.glob("*.png")):
            objects.append({"name": f.stem.replace("-", " ").title(),
                            "slug": f.stem, "image": f.name})

    # Episode cast: every character the script's cast lists, with their ref image.
    cast: list[dict[str, Any]] = []
    if script:
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
                    for label, f in variants_map.items():
                        if (rd / f).exists():
                            variants.append({"label": label, "image": f})
                            if label == "base":
                                ref_img = f
                except Exception:
                    pass
            cast.append({"name": name, "id": cid,
                         "role": c.get("role", ""), "image": ref_img,
                         "variants": variants})

    from .storyboard import storyboard_status
    sb = storyboard_status(show_id)
    cjson = d / "consistency.json"
    consistency = _read_json(cjson) if cjson.exists() else (sb.get("report") or [])
    # Final verdict per shot = the last round's entry.
    final_cons: dict[str, Any] = {}
    for entry in consistency:
        if entry.get("shot"):
            final_cons[entry["shot"]] = entry
    consistency = list(final_cons.values())
    return {
        "episode": ep,
        "script_file": latest.name if latest else None,
        "script": script,
        "reviews": reviews,
        "story": approval.story_state(show_id, ep),
        "summary": {
            "scenes": len(scenes_out),
            "shots": total_shots,
            "dialogue_lines": dialogue_lines,
            "hero_shots": hero_shots,
        },
        "scenes": scenes_out,
        "objects": objects,
        "cast": cast,
        "storyboard": sb,
        "consistency": consistency,
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
            from .bootstrap import ACTIVITY
            from .storyboard import storyboard_status
            a = ACTIVITY.get(parts[2])
            sb = storyboard_status(parts[2])
            detail = ""
            active = False
            if sb.get("state") == "running":
                active = True
                detail = sb.get("detail") or "Working…"
            elif a:
                active = True
                detail = a.get("detail", "Working…")
            self._send(200, {"active": active, "detail": detail, "storyboard": sb})
            return
        if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "episodes":
            self._send(200, {"episodes": episodes_payload(parts[2])})
            return
        if len(parts) == 5 and parts[:2] == ["api", "show"] and parts[3] == "ep":
            self._send(200, episode_payload(parts[2], parts[4]))
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
                                            char=body.get("char", ""), notes=body.get("notes", ""))
                self._send(200, {"ok": True, "messages": msg})
                return
            if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "reject":
                msg = approval.reject_step(parts[2], body.get("step", ""),
                                           char=body.get("char", ""), notes=body.get("notes", "no notes"))
                self._send(200, {"ok": True, "messages": msg})
                return
            if len(parts) == 4 and parts[:2] == ["api", "show"] and parts[3] == "generate":
                from .bootstrap import ACTIVITY
                from .clients.lmstudio import LMStudioClient
                from .scriptgen import WritersRoom
                from .storyboard import build_storyboard
                ep = int(body.get("episode", 1) or 1)
                show_id = parts[2]
                show = Show(show_id)
                llm = LMStudioClient(get_config().get("llm", "base_url"), timeout=900)
                ACTIVITY[show_id] = {"detail": f"Writing episode {ep} (Development → script → review)…",
                                     "ts": time.time()}
                try:
                    result = WritersRoom(show, llm=llm).run(ep)
                    self._send(200, {"ok": True, "messages": [
                        f"episode {ep}: passed={result['passed']} rounds={result['rounds']} "
                        f"— building storyboard + consistency (background)"]})
                except Exception as exc:
                    self._send(200, {"ok": False, "error": str(exc)})
                    ACTIVITY.pop(show_id, None)
                    return
                finally:
                    ACTIVITY.pop(show_id, None)
                # Storyboard + ref pass + costumes + consistency run automatically.
                build_storyboard(show, ep)
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
            self._send(404, {"error": "not found"})
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface errors to the browser
            self._send(500, {"ok": False, "error": repr(exc)})

    def do_DELETE(self) -> None:
        parts = self._path_parts()
        try:
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
