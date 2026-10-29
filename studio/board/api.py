"""Local HTTP API for the board.

The board UI and humans use this. Opencode agents use it too through the
``studio.py board-tool`` / ``board-finish`` bridge. Single writer: every
mutation goes through the store.
"""
from __future__ import annotations

import json
import mimetypes
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import get_config
from . import assets as asset_lib
from .daemon import BoardDaemon, request_episode
from .roles import lane_config, load_roles
from .store import BoardSet

DEFAULT_PORT = 8130

_JOBS: dict[str, dict] = {}
_CANCELS: dict[str, threading.Event] = {}
_JOBS_LOCK = threading.Lock()
# Keep a finished job visible long enough for a slow worker to collect it, and
# NEVER expire a running one: a single outline step can legitimately take over
# an hour, and dropping its record made board-wait 404 and the worker retry.
_JOB_TTL_S = 6 * 3600
_JOB_TERMINAL = ("done", "failed", "canceled")


def _job_start(store, cfg, name: str, run_id: str, card_id: str,
               args: dict) -> str:
    """Start (or reuse) one long tool job for this card.

    Long tools are async and the worker polls them. If the worker died and was
    restarted -- or simply called the tool twice -- reuse the job that is still
    running for the same card/tool instead of spawning a duplicate that competes
    for the same GPU.
    """
    with _JOBS_LOCK:
        for jid, item in _JOBS.items():
            if (item.get("state") == "running"
                    and item.get("name") == name
                    and item.get("card_id") == card_id):
                return jid
        jid = f"job_{uuid.uuid4().hex[:12]}"
        cancel = threading.Event()
        _CANCELS[jid] = cancel
        _JOBS[jid] = {"id": jid, "name": name, "card_id": card_id,
                      "run_id": run_id, "state": "running", "result": None,
                      "error": None, "created": time.time()}

    def _run() -> None:
        try:
            result = _run_board_tool(store, cfg, name, run_id, card_id, args,
                                     cancel_event=cancel)
            with _JOBS_LOCK:
                item = _JOBS.get(jid)
                if item is None:
                    return
                if item.get("state") == "canceled":
                    item["result"] = result
                else:
                    item.update(state="done", result=result)
        except Exception as exc:
            with _JOBS_LOCK:
                item = _JOBS.get(jid)
                if item is not None:
                    item.update(state=("canceled"
                                       if item.get("state") == "canceled"
                                       else "failed"),
                                error=f"{type(exc).__name__}: {exc}")
    threading.Thread(target=_run, daemon=True, name=jid).start()
    return jid


def cancel_jobs_for_card(card_id: str, reason: str = "worker stopped") -> int:
    """Cancel running long jobs owned by a card whose worker has gone away.

    A Python thread blocked in a model call cannot be killed, so cancellation is
    cooperative: the job is marked ``canceled`` immediately (so board-wait and
    the dedupe guard stop treating it as live) and the pipeline aborts at its
    next checkpoint.
    """
    canceled = 0
    with _JOBS_LOCK:
        for jid, item in _JOBS.items():
            if item.get("state") != "running" or item.get("card_id") != card_id:
                continue
            item.update(state="canceled", error=reason)
            event = _CANCELS.get(jid)
            if event is not None:
                event.set()
            canceled += 1
    return canceled


def _job_get(jid: str) -> dict | None:
    with _JOBS_LOCK:
        item = _JOBS.get(jid)
        if not item:
            return None
        now = time.time()
        for key in [k for k, v in _JOBS.items()
                    if (v.get("state") in _JOB_TERMINAL
                        and now - float(v.get("created") or 0) > _JOB_TTL_S)]:
            _JOBS.pop(key, None)
            _CANCELS.pop(key, None)
        return dict(_JOBS.get(jid) or {})


def _run_board_tool(store, cfg, name: str, run_id: str, card_id: str,
                    args: dict, cancel_event=None):
    from ..clients.lmstudio import LMStudioClient
    from . import tools as tools_mod
    run = store.run(run_id)
    card = store.get(card_id)
    if not run or not card:
        raise KeyError("unknown run or card")
    if run.get("card_id") != card_id or card.get("claimed_by") != run_id:
        raise PermissionError("run does not own this card")
    role = load_roles(cfg).get(str(run.get("role") or ""))
    if not role:
        raise KeyError("unknown role")
    llm = LMStudioClient(cfg.get("llm", "base_url"), timeout=role.timeout_s)
    phase = str(run.get("phase") or "produce")
    ctx = tools_mod.ToolContext(
        store=store, card=card, role=role,
        phase=phase,
        run_id=run_id, llm=llm, cfg=cfg, cancel_event=cancel_event)
    try:
        return tools_mod.call(ctx, name, args)
    finally:
        # A tool may load a different model (12B writer, 8B reviewer), evicting
        # the opencode agent's model. Restore the agent's model before its next
        # step or the next request fails with "Model unloaded."
        if phase in ("produce", "review") and role.model:
            try:
                from ..gpu_manager import preload_model
                preload_model(cfg, role.model, strict_context=False)
            except Exception:
                pass
def _static(handler, cfg, name: str) -> None:
    path = Path(cfg.root) / "board" / name
    if not path.is_file():
        return _json(handler, 404, {"error": f"no static file {name}"})
    data = path.read_bytes()
    ctype = {".html": "text/html; charset=utf-8",
             ".css": "text/css; charset=utf-8",
             ".js": "application/javascript; charset=utf-8"}.get(
                 path.suffix, "application/octet-stream")
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except (ConnectionError, OSError):
        pass


def _artifact(handler, cfg, rel: str) -> None:
    root = Path(cfg.root).resolve()
    path = (root / rel).resolve()
    if root not in path.parents and path != root:
        return _json(handler, 403, {"error": "path escapes the studio root"})
    if not path.is_file():
        return _json(handler, 404, {"error": "artifact not found"})
    data = path.read_bytes()
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except (ConnectionError, OSError):
        pass


def _resolve_show_file(show, rel: str):
    if not rel or rel.startswith("/") or ".." in rel.replace("\\", "/").split("/"):
        return None
    p = (Path(show.dir) / rel).resolve()
    base = Path(show.dir).resolve()
    if p != base and base not in p.parents:
        return None
    return p


def _read_show_file(show, rel: str, parse: bool = False):
    p = _resolve_show_file(show, rel)
    if p is None or not p.exists() or not p.is_file():
        return None
    out = {"path": rel, "content": p.read_text(encoding="utf-8", errors="replace"),
           "format": p.suffix.lstrip(".").lower()}
    if parse and p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml as _yaml
            out["data"] = _yaml.safe_load(out["content"])
        except Exception:
            pass
    return out


def _write_show_file(show, rel: str, content: str) -> dict:
    p = _resolve_show_file(show, rel)
    if p is None:
        raise ValueError(f"invalid or outside-show path: {rel!r}")
    p.parent.mkdir(parents=True, exist_ok=True)
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
    p.write_text(content, encoding="utf-8", newline="\n")
    return {"ok": True, "path": rel, "bytes": len(content)}


def _show_files(show) -> list[dict]:
    base = Path(show.dir)
    out = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (".yaml", ".yml", ".json"):
            continue
        rel = p.relative_to(base).as_posix()
        if rel.startswith(("runs/", "assets/", "archive/")):
            continue
        out.append({"path": rel, "format": p.suffix.lstrip(".").lower(),
                    "bytes": p.stat().st_size})
    return out


_VIEWABLE_SUFFIXES = (".json", ".yaml", ".yml", ".md", ".txt", ".csv", ".log")


def _card_files(show, card) -> list[dict]:
    """Every data file that belongs to this card's stage, for the review panel."""
    base = Path(show.dir)
    episode = int(card.get("episode") or 0)
    roots: list[Path] = []
    if episode > 0:
        run = base / "runs" / f"EP{episode:02d}"
        if run.exists():
            roots.append(run)
    # Canon is needed to review any stage, not just Gate 0.
    for name in ("bible.yaml", "bible.yml", "plan.json", "plan.skeleton.json"):
        p = base / name
        if p.is_file():
            roots.append(p)
    for name in ("characters", "scenes", "seasons", "voices", "gate0", "assets"):
        d = base / name
        if d.is_dir():
            roots.append(d)
    files: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        items = [root] if root.is_file() else sorted(root.rglob("*"))
        for p in items:
            if not p.is_file():
                continue
            rel = p.relative_to(base).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            fmt = p.suffix.lstrip(".").lower()
            files.append({"path": rel, "bytes": p.stat().st_size, "format": fmt,
                          "viewable": p.suffix.lower() in _VIEWABLE_SUFFIXES})
    files.sort(key=lambda x: x["path"])
    return files


def _show_episodes(show, store) -> list[dict]:
    base = Path(show.dir) / "runs"
    out = []
    if not base.exists():
        return out
    for d in sorted(base.glob("EP*")):
        if not d.is_dir():
            continue
        try:
            ep = int(d.name[2:])
        except ValueError:
            continue
        scripts = sorted(d.glob("script.r*.json"))
        plan = {}
        plan_p = d / "plan.json"
        if plan_p.exists():
            try:
                plan = json.loads(plan_p.read_text(encoding="utf-8"))
            except Exception:
                plan = {}
        video = sorted((d / "video").glob("*.mp4")) if (d / "video").exists() else []
        cards = store.all(show_id=show.show_id, episode=ep)
        out.append({
            "episode": ep,
            "plan": (d / "plan.json").exists(),
            "title": plan.get("title", ""),
            "type": plan.get("episode_type", ""),
            "synopsis": plan.get("synopsis") or plan.get("plot", ""),
            "script": scripts[-1].name if scripts else "",
            "storyboard": (d / "storyboard").exists(),
            "shots": len(video),
            "episode_file": (d / "episode.mp4").exists(),
            "cards": len(cards),
            "accepted": sum(1 for c in cards if c["state"] == "accepted"),
            "states": {s: sum(1 for c in cards if c["state"] == s)
                       for s in ("planned", "in_work", "reviewing", "blocked", "accepted")},
        })
    return out


def _show_regenerate(store, show_id: str, kind: str, char: str = "",
                     notes: str = ""):
    import time
    from ..show import Show
    from .cards import new_card
    show = Show(show_id)
    st = show.bootstrap_state()
    stamp = int(time.time())
    if kind in ("ideas", "bible", "characters", "scenes"):
        # Partitioned-stage rebuild: reset just that stage and mint a stamped
        # card chained behind its parent stage. (Legacy "plan"/"concept"
        # kinds below keep the old single-shot rebuild for pre-partition shows.)
        parents = {"ideas": [], "bible": [f"g0.{show_id}.ideas"],
                   "characters": [f"g0.{show_id}.bible"],
                   "scenes": [f"g0.{show_id}.characters"]}
        kinds = {"ideas": "gate0_ideas", "bible": "gate0_bible",
                 "characters": "gate0_characters", "scenes": "gate0_scenes"}
        titles = {"ideas": "Re-pitch show ideas off the brief",
                  "bible": "Rewrite the series bible",
                  "characters": "Re-propose the cast",
                  "scenes": "Rebuild the scene registry"}
        payload: dict[str, Any] = {"auto": False}
        if kind == "ideas":
            orig = store.get(f"g0.{show_id}.ideas") or {}
            op = (orig.get("payload") or {})
            payload = {"brief": str(op.get("brief") or ""),
                       "n": int(op.get("n") or 5), "auto": False,
                       "notes": notes}
        if kind == "bible":
            st.setdefault("bible", {})["status"] = ""
        if kind == "characters":
            for entry in st.get("characters", []):
                entry["proposal"] = ""
        if kind == "scenes":
            st.setdefault("scenes", {})["status"] = ""
        st["complete"] = False
        show.set_bootstrap_state(st)
        card = new_card(f"g0.{show_id}.{kind}.r{stamp}", kind=kinds[kind],
                        title=titles[kind], show_id=show_id, role=kinds[kind],
                        reviewer="human", lane="llm", priority=9,
                        max_attempts=3, payload=payload,
                        depends_on=parents[kind])
        return store.upsert(card)
    if kind in ("plan", "concept"):
        if kind in ("plan", "concept"):
            st.setdefault("concept", {})["status"] = ""
        if kind in ("plan", "bible"):
            st.setdefault("bible", {})["status"] = ""
        if kind == "plan":
            for entry in st.get("characters", []):
                entry["proposal"] = ""
        if kind in ("plan", "scenes"):
            st.setdefault("scenes", {})["status"] = ""
        st["complete"] = False
        show.set_bootstrap_state(st)
        cid = f"g0.{show_id}.plan.r{stamp}"
        # Preserve the owner's original brief: a retry without it generates
        # generic content (or nothing the owner recognizes). Fall back to ""
        # only when no prior plan card carries a brief.
        _orig = store.get(f"g0.{show_id}.plan") or {}
        _brief = str(((_orig.get("payload") or {}).get("brief")) or "").strip()
        if not _brief:
            for _c in store.all(show_id=show_id):
                if _c.get("kind") == "gate0_plan":
                    _brief = str(((_c.get("payload") or {}).get("brief")) or "").strip()
                    if _brief:
                        break
        card = new_card(cid, kind="gate0_plan",
                        title="Rebuild the show bible, cast & scenes",
                        show_id=show_id, role="gate0_plan", reviewer="human",
                        lane="llm", priority=9, max_attempts=3,
                        payload={"brief": _brief, "auto": False})
    elif kind in ("refs", "voices", "voice_text"):
        if kind == "voice_text" and not char:
            raise ValueError("char is required for voice_text")
        import re as _re
        slug = _re.sub(r"[^a-z0-9-]+", "-", char.lower()).strip("-")
        if kind in ("voice_text", "voices") and char:
            # Voice work lives on the (per-character) voices card only: text,
            # identity and recording are one job, so a "write" card is never
            # minted. Requeue the character's voices card (or mint one if the
            # only ones around are already accepted).
            target = None
            for c in store.all(show_id=show_id):
                if c.get("kind") != "gate0_voices":
                    continue
                if str((c.get("payload") or {}).get("char") or "").lower() != char.lower():
                    continue
                if c.get("state") in ("accepted",):
                    continue
                target = c
                break
            if target is None:
                for entry in st.get("characters", []):
                    if str(entry.get("name") or "").lower() == char.lower():
                        entry["voice"] = ""
                show.set_bootstrap_state(st)
                cid = f"g0.{show_id}.voices.{slug}.{stamp}"
                card = new_card(
                    cid, kind="gate0_voices",
                    title=f"Record voice sample: {char}",
                    show_id=show_id, role="gate0_voices", reviewer="human",
                    lane="audio", priority=8, max_attempts=2,
                    payload={"char": char})
                return store.upsert(card)
            if target.get("state") in ("blocked", "deferred"):
                # Reuse instead of minting a duplicate: this card is the
                # character's one voice card. Unblock it so the daemon
                # picks it back up.
                for entry in st.get("characters", []):
                    if str(entry.get("name") or "").lower() == char.lower():
                        entry["voice"] = ""
                show.set_bootstrap_state(st)
                store.update(target["id"], attempts=0)
                try:
                    store.advance(target["id"], "planned", "human",
                                    "voice rework requested")
                except ValueError:
                    pass
                return store.get(target["id"])
            if target.get("state") == "reviewing":
                for entry in st.get("characters", []):
                    if str(entry.get("name") or "").lower() == char.lower():
                        entry["voice"] = ""
                show.set_bootstrap_state(st)
                store.update(target["id"], attempts=0)
                try:
                    store.advance(target["id"], "retake", "human",
                                  "voice rework requested")
                except ValueError:
                    pass
            return store.get(target["id"])
        if kind == "voices":
            active = [c for c in store.all(show_id=show_id)
                      if c.get("kind") == "gate0_voices"
                      and not (c.get("payload") or {}).get("char")
                      and c.get("state") not in ("accepted", "blocked", "deferred")]
            for entry in st.get("characters", []):
                entry["voice"] = ""
            show.set_bootstrap_state(st)
            if active:
                return active[0]
            cid = f"g0.{show_id}.voices.r{stamp}"
            card = new_card(cid, kind="gate0_voices", title="Record voice samples for the cast",
                            show_id=show_id, role="gate0_voices", reviewer="human",
                            lane="audio", priority=8, max_attempts=2)
            return store.upsert(card)
        for entry in st.get("characters", []):
            if char and entry.get("name", "").lower() != char.lower():
                continue
            entry["refs"] = ""
        show.set_bootstrap_state(st)
        cid = f"g0.{show_id}.images.r{stamp}"
        card = new_card(cid, kind="gate0_images", title="Redraw character & location references",
                        show_id=show_id, role="gate0_images", reviewer="human",
                        lane="image", priority=8, max_attempts=2)
    else:
        raise ValueError(f"unknown regenerate kind {kind!r}")
    return store.upsert(card)


def _reject_image(store, card_id: str, path: str, notes: str):
    import shutil
    from ..show import Show
    card = store.get(card_id)
    if not card:
        raise ValueError(f"no card {card_id!r}")
    if card.get("state") == "accepted":
        # Late rejection: reopen the accepted card for rework instead of
        # stranding the user (accepted is otherwise terminal).
        store.update(card_id, attempts=0)
        try:
            store.advance(card_id, "retake", "human",
                           notes or "reopened for image retake")
        except ValueError:
            pass
        card = store.get(card_id) or card
    show = Show(str(card.get("show_id") or ""))
    asset = asset_lib.parse_asset(show, path)
    if not asset:
        raise ValueError("that path is not a character or scene image")
    if not asset_lib.is_retakeable(asset):
        raise ValueError(f"{asset_lib.asset_title(asset)} cannot be retaken individually yet")
    attached = [a for a in (card.get("artifacts") or [])
                if str(a.get("path") or "").replace("\\", "/") == asset["path"]]
    if not attached:
        raise ValueError("that image is not attached to this card")
    # Same card does the rework: mark the asset rejected (with notes), stash
    # the notes on the card for the rerun, archive the rejected file, and
    # send the card itself back through retake. No separate retake card.
    asset_lib.mark_rejected(show, asset, notes)
    source = Path(show.dir.parents[1]) / asset["path"]
    if source.exists():
        stamp = int(time.time() * 1000)
        dest = source.parent / "rejected" / f"{source.stem}.{stamp}{source.suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    payload = dict(card.get("payload") or {})
    payload["notes"] = notes or "rejected"
    store.update(card_id,
                 result={**(card.get("result") or {}), "rejected": notes or "rejected"},
                 payload=payload, attempts=0)
    updated = asset_lib.update_artifact(
        store, card_id, asset["path"], status="rejected",
        notes=notes, asset=asset)
    try:
        updated = store.advance(card_id, "retake", "human", notes or "rejected")
    except ValueError:
        pass
    store.note(card_id, "human",
               f"Rejected {asset_lib.asset_title(asset)}: {notes or 'no notes'}; "
               f"reworking on this card")
    return store.get(card_id), None


def _approve_image(store, card_id: str, path: str):
    """Approve one attached image in place (no retake card)."""
    from ..show import Show
    card = store.get(card_id)
    if not card:
        raise ValueError(f"no card {card_id!r}")
    if card.get("state") == "accepted":
        raise ValueError("that card is already accepted")
    show = Show(str(card.get("show_id") or ""))
    asset = asset_lib.parse_asset(show, path)
    if not asset:
        raise ValueError("that path is not a character or scene image")
    attached = [a for a in (card.get("artifacts") or [])
                if str(a.get("path") or "").replace("\\", "/") == asset["path"]]
    if not attached:
        raise ValueError("that image is not attached to this card")
    asset_lib.mark_approved(show, asset)
    updated = asset_lib.update_artifact(
        store, card_id, asset["path"], status="accepted")
    store.note(card_id, "human",
               f"Accepted {asset_lib.asset_title(asset)}")
    return store.get(card_id)


def _reject_scene(store, card_id: str, scene_id: str, notes: str):
    """Queue a per-scene rewrite card, like an image retake."""
    from ..planner import read_episode_plan
    from ..show import Show
    from .cards import new_card

    card = store.get(card_id)
    if not card:
        raise ValueError(f"no card {card_id!r}")
    show = Show(str(card.get("show_id") or ""))
    episode = int(card.get("episode") or 0)
    plan = read_episode_plan(show, episode)
    scenes = plan.get("scenes") or []
    scene = next((s for s in scenes if str(s.get("id")) == scene_id), None)
    if not scene:
        raise ValueError(f"no scene {scene_id!r} in the current plan")
    # One open rewrite per scene: re-clicking Reject opens the same card.
    for existing in store.all():
        payload = existing.get("payload") or {}
        if (existing.get("kind") == "outline_scene"
                and payload.get("parent") == card_id
                and payload.get("scene_id") == scene_id
                and existing.get("state") not in ("accepted", "blocked")):
            return card, existing
    stamp = int(time.time() * 1000)
    retake_id = f"{card_id}.retake.scene.{scene_id}.{stamp}"
    summary = str(scene.get("summary") or "").strip()
    title = f"Rewrite scene {scene_id}"
    if summary:
        title += f": {summary[:70]}"
    retake = new_card(
        retake_id, kind="outline_scene", title=title,
        show_id=show.show_id, episode=episode, role="scene_writer",
        reviewer="", lane="llm", priority=int(card.get("priority") or 5),
        max_attempts=2,
        payload={"tool": "rewrite_scene",
                 "args": {"scene_id": scene_id, "notes": notes, "parent": card_id},
                 "scene_id": scene_id, "notes": notes, "parent": card_id},
    )
    retake = store.upsert(retake)
    store.note(card_id, "human",
               f"Rejected scene {scene_id}: {notes or 'no notes'}; queued {retake_id}")
    return store.get(card_id), retake


def _reject_voice(store, card_id: str, path: str, notes: str):
    import shutil
    from ..show import Show
    card = store.get(card_id)
    if not card:
        raise ValueError(f"no card {card_id!r}")
    if card.get("state") == "accepted":
        # Late rejection: reopen the accepted card for rework instead of
        # stranding the user (accepted is otherwise terminal).
        store.update(card_id, attempts=0)
        try:
            store.advance(card_id, "retake", "human",
                           notes or "reopened for voice retake")
        except ValueError:
            pass
        card = store.get(card_id) or card
    show = Show(str(card.get("show_id") or ""))
    asset = asset_lib.parse_asset(show, path)
    if not asset or asset.get("type") != "voice":
        raise ValueError("that path is not a voice sample")
    if not asset_lib.is_retakeable(asset):
        raise ValueError("that file is a voice config, not a voice sample")
    attached = [a for a in (card.get("artifacts") or [])
                if str(a.get("path") or "").replace("\\", "/") == asset["path"]]
    if not attached:
        raise ValueError("that voice sample is not attached to this card")
    char = show.read_character(str(asset.get("id") or "")) or {}
    name = str(char.get("name") or asset.get("label") or asset.get("id") or "")
    state = show.bootstrap_state()
    for entry in state.get("characters", []):
        if str(entry.get("name") or "").lower() == name.lower():
            entry["voice"] = "rejected"
            entry["voice_notes"] = notes or ""
            break
    show.set_bootstrap_state(state)
    # Same card does the full loop now (identity + text + re-record via the
    # voices tool's refresh step): no second card, no blocked original.
    source = Path(show.dir.parents[1]) / asset["path"]
    if source.exists():
        stamp = int(time.time() * 1000)
        dest = source.parent / "rejected" / f"{source.stem}.{stamp}{source.suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    payload = dict(card.get("payload") or {})
    payload["notes"] = notes or "rejected"
    payload["char"] = name
    store.update(card_id,
                 result={**(card.get("result") or {}), "rejected": notes or "rejected"},
                 payload=payload, attempts=0)
    updated = asset_lib.update_artifact(
        store, card_id, asset["path"], status="rejected",
        notes=notes, asset=asset)
    try:
        updated = store.advance(card_id, "retake", "human", notes or "rejected")
    except ValueError:
        pass
    store.note(card_id, "human",
               f"Rejected {asset_lib.asset_title(asset)}: {notes or 'no notes'}; reworking in place")
    return store.get(card_id), None


def _decide_retake(store, card: dict, action: str, notes: str):
    from ..show import Show
    payload = dict(card.get("payload") or {})
    asset = dict(payload.get("asset") or {})
    if not asset:
        raise ValueError("retake card has no asset")
    show = Show(str(card.get("show_id") or ""))
    parent = str(payload.get("parent") or "")
    if action == "approve":
        asset_lib.mark_approved(show, asset)
        if parent:
            asset_lib.update_artifact(store, parent, asset["path"],
                                      status="accepted", retake_id=card["id"])
            store.note(parent, "human",
                       f"Retake accepted for {asset_lib.asset_title(asset)}")
        store.update(card["id"], result={**(card.get("result") or {}),
                                         "asset": asset, "review": "accepted"})
        return store.advance(card["id"], "accepted", "human", "retake accepted")
    asset_lib.mark_rejected(show, asset, notes)
    if parent:
        asset_lib.update_artifact(store, parent, asset["path"],
                                  status="rejected", notes=notes,
                                  retake_id=card["id"])
        store.note(parent, "human",
                   f"Retake rejected for {asset_lib.asset_title(asset)}: {notes or 'no notes'}")
    attempts = int(card.get("attempts") or 0) + 1
    payload["notes"] = notes or "retake rejected"
    payload.setdefault("args", {})["notes"] = payload["notes"]
    store.update(card["id"], payload=payload, attempts=attempts,
                 result={**(card.get("result") or {}), "rejected": notes or ""})
    current = store.get(card["id"]) or card
    if attempts >= int(current.get("max_attempts") or 2):
        return store.advance(card["id"], "blocked", "human", "retake cap")
    return store.advance(card["id"], "retake", "human", notes or "retake rejected")


def _run_log(cfg, run_id: str) -> dict:
    # Every file artifact a run produced, so the card view can show them
    # inline: worker stdio (board/runs) plus prompt + tool trace
    # (runs/agents). Legacy agent runs keep their prompt.md only there.
    if not run_id or any(s in run_id for s in ("/", "\\", "..")):
        raise ValueError(f"bad run id {run_id!r}")
    base = Path(cfg.root) / "board" / "runs" / run_id
    agent_base = Path(cfg.root) / "runs" / "agents" / run_id
    out = {"id": run_id}

    def _tail(path: Path) -> str:
        return path.read_text(
            encoding="utf-8", errors="replace")[-30000:] if path.exists() else ""
    for name in ("prompt.md", "stdout.log", "stderr.log"):
        path = base / name
        if not path.exists() and name == "prompt.md":
            path = agent_base / name
        out[name.split(".")[0]] = _tail(path)
    out["trace"] = _tail(agent_base / "trace.json")
    return out


def _model_catalog(cfg) -> list[dict]:
    """The actual model list from opencode, grouped local/remote.

    Source: `opencode models` (the same list the CLI uses), not just the
    provider map in opencode.jsonc. Local entries are limited to models that
    LM Studio currently has installed.
    """
    local_bare: list[str] = []
    remote: list[str] = []
    try:
        from .opencode import _opencode_exe
        proc = subprocess.run([_opencode_exe() or "opencode", "models"],
                              capture_output=True, text=True, timeout=20,
                              encoding="utf-8", errors="replace")
        for line in (proc.stdout or "").splitlines():
            mid = line.strip()
            if not mid or "/" not in mid:
                continue
            if mid.startswith("lm_studio_local/"):
                local_bare.append(mid.split("/", 1)[1])
            else:
                remote.append(mid)
    except Exception:
        pass
    for name in ("opencode.jsonc", "opencode.json"):
        path = Path.home() / ".config" / "opencode" / name
        if not path.exists():
            continue
        try:
            text = re.sub(r"^\s*//.*$", "", path.read_text(encoding="utf-8"), flags=re.M)
            conf = json.loads(text)
        except Exception:
            continue
        for prov, pconf in (conf.get("provider") or {}).items():
            if not isinstance(pconf, dict):
                continue
            for mid in (pconf.get("models") or {}):
                if prov == "lm_studio_local":
                    local_bare.append(str(mid))
                else:
                    remote.append(f"{prov}/{mid}")
        for key in ("model", "small_model"):
            mid = str(conf.get(key) or "").strip()
            if "/" in mid:
                remote.append(mid)
        break
    available = None
    base = str(cfg.get("llm", "base_url") or "http://127.0.0.1:1234/v1").rstrip("/")
    try:
        import httpx
        with httpx.Client(timeout=4.0) as client:
            data = client.get(base + "/models").json()
        available = {str(m.get("id") or "") for m in (data.get("data") or [])}
    except Exception:
        available = None
    local = [m for m in dict.fromkeys(local_bare)
             if available is None or m in available]
    models = [{"id": f"lm_studio_local/{m}", "kind": "local", "label": m}
              for m in local]
    models += [{"id": m, "kind": "remote", "label": m}
               for m in dict.fromkeys(remote)]
    models.sort(key=lambda m: (m["kind"] != "local", m["id"]))
    return models

def _show_list(cfg, store) -> list[dict]:
    from ..show import Show
    out = []
    for sid in cfg.list_shows():
        try:
            st = Show(sid).bootstrap_state() or {}
        except Exception:
            st = {}
        cards = store.all(show_id=sid)
        out.append({
            "id": sid,
            "title": (Show(sid).read_bible() or {}).get("title", ""),
            "complete": bool(st.get("complete")),
            "cards": len(cards),
            "accepted": sum(1 for c in cards if c["state"] == "accepted"),
            "episodes": sorted({int(c.get("episode") or 0)
                                for c in cards if int(c.get("episode") or 0) > 0}),
            "status": {k: (st.get(k) or {}).get("status", "")
                       for k in ("concept", "bible", "scenes")},
        })
    return out


def _show_detail(cfg, store, sid: str) -> dict:
    from ..show import Show
    from . import gate0
    show = Show(sid)
    state = show.bootstrap_state() or {}
    cards = store.all(show_id=sid)
    gate = gate0.status(show)
    gate_chars = {c.get("name"): c for c in gate.get("characters", [])}
    from .. import refs as refs_mod
    bible = show.read_bible() or {}
    from ..development import _plotline_state
    plotlines = _plotline_state(show, bible)
    seasons = []
    for sno in show.list_seasons():
        data = show.read_season(sno) or {}
        seasons.append({
            "season": data.get("season", sno),
            "title": data.get("title", f"Season {sno}"),
            "status": data.get("status", "pending"),
            "num_episodes": data.get("num_episodes", len(data.get("episodes") or [])),
            "season_arc": data.get("season_arc") or {},
            "episodes": [e for e in (data.get("episodes") or []) if isinstance(e, dict)],
        })
    if not seasons:
        slots = {}
        runs_dir = Path(show.dir) / "runs"
        for d in sorted(runs_dir.glob("EP*")) if runs_dir.exists() else []:
            plan_p = d / "plan.json"
            if not plan_p.exists():
                continue
            try:
                plan = json.loads(plan_p.read_text(encoding="utf-8"))
            except Exception:
                continue
            entry = (plan.get("_season") or {}).get("entry")
            if isinstance(entry, dict) and entry.get("episode"):
                slots[int(entry["episode"])] = entry
        if slots:
            seasons = [{"season": 1, "title": "Season 1",
                        "status": "from outline", "num_episodes": len(slots),
                        "episodes": [slots[k] for k in sorted(slots)]}]
    chars = []
    for cid in show.list_characters():
        ch = show.read_character(cid) or {}
        data = refs_mod.load_refs(show, cid) or {}
        images = []
        for label, fn in (data.get("variants") or {}).items():
            p = Path(show.character_refs_dir(cid)) / str(fn)
            if p.exists():
                images.append({"label": str(label),
                               "path": f"shows/{sid}/characters/{cid}/refs/{fn}"})
        voice = Path(show.dir) / "assets" / "voice" / f"{cid}_voice.wav"
        chars.append({**ch, **gate_chars.get(ch.get("name"), {}),
                      "refs_images": images,
                      "voice_sample": (f"shows/{sid}/assets/voice/{cid}_voice.wav"
                                       if voice.exists() else "")})
    scenes = []
    for scene_id in show.list_scenes():
        sp = Path(show.scenes_dir) / f"{scene_id}.yaml"
        try:
            import yaml as _yaml
            data = _yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        rd = Path(show.scenes_dir) / scene_id / "refs"
        images = [{"label": p.stem,
                   "path": f"shows/{sid}/scenes/{scene_id}/refs/{p.name}"}
                  for p in sorted(rd.glob("*.png"))] if rd.exists() else []
        scenes.append({"id": scene_id, **data, "images": images})
    return {
        "id": sid,
        "bible": bible,
        "concept": show.read_concept() or {},
        "ideas": show.read_ideas() or {},
        "continuity": show.read_continuity() or {},
        "plotlines": plotlines,
        "seasons": seasons,
        "characters": chars,
        "gate0": gate,
        "state": state,
        "cards": cards,
        "files": _show_files(show),
        "episodes": _show_episodes(show, store),
        "scenes": scenes,
    }


def _json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)
    except (ConnectionError, OSError):
        # The agent's bash timeout closed the socket; nothing to send.
        pass


def _body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except ValueError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data


class BoardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    store: BoardSet
    daemon: BoardDaemon
    cfg: Any

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        return

    def _events(self) -> None:
        q = self.store.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(
                    ("data: " + json.dumps(event, default=str) + "\n\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.store.unsubscribe(q)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        try:
            if parsed.path in ("/", "/board", "/index.html"):
                return _static(self, self.cfg, "index.html")
            if parsed.path in ("/board.css", "/board.js"):
                return _static(self, self.cfg, parsed.path.lstrip("/"))
            if parsed.path == "/api/health":
                return _json(self, 200, {"ok": True})
            if parsed.path == "/api/events":
                return self._events()
            if parsed.path in ("/api/board", "/api/board/"):
                cards = self.store.all()
                return _json(self, 200, {
                    "cards": cards,
                    "runs": self.store.runs(),
                    "lanes": lane_config(self.cfg),
                    "daemon": bool(self.daemon.running),
                    "roles": {name: {"lane": r.lane, "model": r.model,
                                     "kind": r.kind, "tools": list(r.tools)}
                              for name, r in load_roles(self.cfg).items()},
                })
            if parsed.path == "/api/card":
                from ..show import Show
                cid = (q.get("id") or [""])[0]
                card = self.store.get(cid)
                if not card:
                    return _json(self, 404, {"error": f"no card {cid!r}"})
                card = dict(card)
                show = Show(str(card.get("show_id") or ""))
                card["artifacts"] = asset_lib.enrich_artifacts(
                    show, list(card.get("artifacts") or []))
                return _json(self, 200, {"card": card,
                                         "history": self.store.history(cid)})
            if parsed.path == "/api/card/files":
                from ..show import Show
                cid = (q.get("id") or [""])[0]
                card = self.store.get(cid)
                if not card:
                    return _json(self, 404, {"error": f"no card {cid!r}"})
                files = _card_files(Show(str(card.get("show_id") or "")), card)
                return _json(self, 200, {"files": files,
                                         "show_id": card.get("show_id"),
                                         "episode": card.get("episode")})
            if parsed.path == "/api/run":
                rid = (q.get("id") or [""])[0]
                run = self.store.run(rid)
                if not run:
                    return _json(self, 404, {"error": f"no run {rid!r}"})
                return _json(self, 200, {"run": run})
            if parsed.path == "/api/runs":
                return _json(self, 200, {"runs": self.store.runs(
                    state=(q.get("state") or [None])[0])})
            if parsed.path == "/api/agents":
                return _json(self, 200, {
                    "lanes": lane_config(self.cfg),
                    "opencode": self.cfg.get("agents", "opencode", {}),
                    "roles": {name: {"lane": r.lane, "model": r.model,
                                     "kind": r.kind, "temperature": r.temperature,
                                     "max_steps": r.max_steps,
                                     "tools": list(r.tools)}
                              for name, r in load_roles(self.cfg).items()},
                })
            if parsed.path.startswith("/api/jobs/"):
                jid = parsed.path.rsplit("/", 1)[-1]
                item = _job_get(jid)
                if not item:
                    return _json(self, 404, {"error": f"no job {jid!r}"})
                return _json(self, 200, item)
            if parsed.path == "/api/models":
                return _json(self, 200, {"models": _model_catalog(self.cfg)})
            if parsed.path == "/api/shows":
                return _json(self, 200, {"shows": _show_list(self.cfg, self.store)})
            if parsed.path == "/api/show":
                sid = (q.get("id") or [""])[0]
                if not sid:
                    return _json(self, 400, {"error": "id is required"})
                return _json(self, 200, _show_detail(self.cfg, self.store, sid))
            if parsed.path == "/api/show/episode":
                sid = (q.get("show_id") or [""])[0]
                ep = int((q.get("episode") or ["0"])[0] or 0)
                d = Path(self.cfg.show_path(sid)) / "runs" / f"EP{ep:02d}"
                plan = {}
                script = {}
                try:
                    plan = json.loads((d / "plan.json").read_text(encoding="utf-8"))
                except Exception:
                    plan = {}
                try:
                    scripts = sorted(d.glob("script.r*.json"))
                    if scripts:
                        script = json.loads(scripts[-1].read_text(encoding="utf-8"))
                except Exception:
                    script = {}
                return _json(self, 200, {"show_id": sid, "episode": ep,
                                         "plan": plan, "script": script})
            if parsed.path == "/api/show/file":
                from ..show import Show
                sid = (q.get("show_id") or [""])[0]
                rel = (q.get("path") or [""])[0]
                want_parsed = (q.get("parsed") or [""])[0] == "1"
                data = _read_show_file(Show(sid), rel,
                                       parse=want_parsed) if sid else None
                if not data:
                    return _json(self, 404, {"error": "file not found"})
                return _json(self, 200, data)
            if parsed.path == "/api/artifact":
                rel = (q.get("path") or [""])[0]
                return _artifact(self, self.cfg, rel)
            if parsed.path == "/api/run/log":
                rid = (q.get("id") or [""])[0]
                return _json(self, 200, _run_log(self.cfg, rid))
            return _json(self, 404, {"error": "not found"})
        except Exception as exc:
            return _json(self, 500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            data = _body(self)
            if parsed.path == "/api/agents/model":
                role = str(data.get("role") or "")
                model = str(data.get("model") or "")
                roles = self.cfg.data.setdefault("agents", {}).setdefault("roles", {})
                if role not in roles:
                    return _json(self, 400, {"error": f"unknown role {role!r}"})
                roles[role]["model"] = model
                try:
                    import re
                    path = Path(self.cfg.root) / "config" / "agents.yaml"
                    text = path.read_text(encoding="utf-8")
                    pat = re.compile(
                        rf"(^  {re.escape(role)}:\n(?:    .*\n)*?    model: )"
                        rf"(\"[^\"]*\"|\S+)", re.M)
                    if pat.search(text):
                        text = pat.sub(
                            lambda m: m.group(1) + json.dumps(model), text, count=1)
                        path.write_text(text, encoding="utf-8", newline="\n")
                except Exception:
                    pass
                return _json(self, 200, {
                    "role": role, "model": model,
                    "roles": {name: {"lane": r.lane, "model": r.model,
                                     "kind": r.kind}
                              for name, r in load_roles(self.cfg).items()},
                })
            if parsed.path == "/api/show":
                name = str(data.get("name") or "").strip()
                brief = str(data.get("brief") or "").strip()
                from ..shows import _skeleton
                from .gate0 import bootstrap_cards
                # The name is optional: without one the show gets a provisional
                # id and the picked pitch's slug becomes its real id.
                auto_id = not name
                show_id = (name.lower().replace(" ", "-")
                           if name else f"show-{int(time.time())}")
                if (Path(self.cfg.show_path(show_id)) / "bible.yaml").exists():
                    return _json(self, 409, {"error": f"show '{show_id}' exists"})
                _skeleton(show_id)
                cards = [self.store.upsert(c)
                         for c in bootstrap_cards(show_id, brief=brief,
                                                  auto_id=auto_id)]
                return _json(self, 200, {"cards": cards, "show_id": show_id,
                                         "auto_id": auto_id})
            if parsed.path == "/api/show/episode":
                sid = (q.get("show_id") or [""])[0]
                ep = int((q.get("episode") or ["0"])[0] or 0)
                d = Path(self.cfg.show_path(sid)) / "runs" / f"EP{ep:02d}"
                plan = {}
                script = {}
                try:
                    plan = json.loads((d / "plan.json").read_text(encoding="utf-8"))
                except Exception:
                    plan = {}
                try:
                    scripts = sorted(d.glob("script.r*.json"))
                    if scripts:
                        script = json.loads(scripts[-1].read_text(encoding="utf-8"))
                except Exception:
                    script = {}
                return _json(self, 200, {"show_id": sid, "episode": ep,
                                         "plan": plan, "script": script})
            if parsed.path == "/api/show/file":
                from ..show import Show
                sid = str(data.get("show_id") or "")
                rel = str(data.get("path") or "")
                content = data.get("content")
                if not sid or not rel or not isinstance(content, str):
                    return _json(self, 400, {"error": "show_id, path and content are required"})
                try:
                    out = _write_show_file(Show(sid), rel, content)
                except Exception as exc:
                    return _json(self, 400, {"error": str(exc)})
                return _json(self, 200, out)
            if parsed.path == "/api/show/regenerate":
                sid = str(data.get("show_id") or "")
                kind = str(data.get("kind") or "")
                if not sid or not kind:
                    return _json(self, 400, {"error": "show_id and kind are required"})
                try:
                    card = _show_regenerate(self.store, sid, kind,
                                            str(data.get("char") or ""),
                                            str(data.get("notes") or ""))
                except Exception as exc:
                    return _json(self, 400, {"error": str(exc)})
                return _json(self, 200, {"card": card})
            if parsed.path == "/api/show/reselect":
                sid = str(data.get("show_id") or "")
                pid = str(data.get("pitch_id") or "")
                if not sid or not pid:
                    return _json(self, 400, {"error": "show_id and pitch_id are required"})
                from ..show import Show as _Show
                from .cards import new_card as _new_card
                from . import gate0 as _gate0
                from ..bootstrap import BootstrapChain as _Chain
                show = _Show(sid)
                ideas = show.read_ideas() or {}
                pitch = next((p for p in (ideas.get("pitches") or [])
                              if str(p.get("id")) == pid), None)
                if pitch is None:
                    return _json(self, 404, {"error": f"no pitch {pid!r} in the archive"})
                # A reselected pitch can also rename the show when it was
                # created without a name (provisional id).
                renamed = False
                wanted = str(pitch.get("show_id") or "").strip()
                if wanted and wanted != sid:
                    from ..shows import rename_show as _rename_show
                    from .store import valid_show_id as _valid_show_id
                    if (_valid_show_id(wanted)
                            and not Path(self.cfg.show_path(wanted)).exists()):
                        _rename_show(sid, wanted)
                        try:
                            self.store.close_show(sid)
                        except Exception:
                            pass
                        sid, renamed = wanted, True
                        show = _Show(sid)
                        ideas = show.read_ideas() or {}
                        pitch = next((p for p in (ideas.get("pitches") or [])
                                      if str(p.get("id")) == pid), pitch)
                profile = self.cfg.get("show_profile") or {}
                maturity = str(pitch.get("maturity")
                               or profile.get("maturity") or "").strip()
                show.write_concept({
                    "title": str(pitch.get("title") or ""),
                    "logline": str(pitch.get("logline") or ""),
                    "premise": str(pitch.get("premise") or ""),
                    "genre": list(pitch.get("genre") or []),
                    "tone": list(pitch.get("tone") or []),
                    "hook": str(pitch.get("hook") or ""),
                    "maturity": maturity,
                    "picked_from": pid,
                    "picked_pitches": [pid],
                })
                now = int(time.time())
                if pitch.get("status") != "selected":
                    pitch["status"] = "selected"
                    pitch["selected_at"] = now
                ideas["active"] = pid
                show.write_ideas(ideas)
                # Rewind downstream stages; accepted stage cards stay as
                # history and get stamped replacements, planned ones just run.
                st = show.bootstrap_state() or {}
                st["concept"] = {"status": "approved"}
                st["bible"] = {"status": ""}
                st["characters"] = []
                st["scenes"] = {"status": ""}
                st["complete"] = False
                show.set_bootstrap_state(st)
                from ..events import CONCEPT_APPROVED, new_event
                from ..approval import _emit
                _emit(show, CONCEPT_APPROVED,
                      {"manual": True, "via": "reselect", "pitch": pid})
                prev = f"g0.{sid}.ideas"
                minted = []
                for domain, kind, role, title in (
                    ("bible", "gate0_bible", "gate0_bible", "Write the series bible"),
                    ("characters", "gate0_characters", "gate0_characters",
                     "Propose the cast from the approved bible"),
                    ("scenes", "gate0_scenes", "gate0_scenes",
                     "Build the scene registry from the approved bible"),
                ):
                    orig = self.store.get(f"g0.{sid}.{domain}")
                    if orig is not None and str(orig.get("state")) != "accepted":
                        prev = str(orig.get("id"))
                        continue
                    new = _new_card(f"g0.{sid}.{domain}.r{now}", kind=kind,
                                    title=title, show_id=sid, role=role,
                                    reviewer="human", lane="llm", priority=9,
                                    max_attempts=3, payload={"auto": False},
                                    depends_on=[prev])
                    self.store.upsert(new)
                    minted.append(new)
                    prev = str(new.get("id"))
                # A reselect is a pick: accept every reviewing ideas card so
                # the stage reads complete and downstream stages unblock.
                for _c in self.store.all(show_id=sid):
                    if (str(_c.get("kind") or "") != "gate0_ideas"
                            or str(_c.get("state") or "") != "reviewing"):
                        continue
                    try:
                        self.store.advance(
                            str(_c.get("id")), "accepted", "human",
                            f"reselected pitch {pid}; concept approved")
                    except ValueError:
                        pass
                _Chain(show, cfg=self.cfg, llm=None).check_complete()
                return _json(self, 200, {"concept": show.read_concept(),
                    "cards": minted, "status": _gate0.status(show),
                    "show_id": sid, "renamed": renamed})
            if parsed.path == "/api/show/delete":
                sid = str(data.get("show_id") or "").strip()
                if not sid:
                    return _json(self, 400, {"error": "show_id is required"})
                from ..shows import delete_show as _delete_show
                try:
                    _delete_show(sid)
                except Exception as exc:
                    return _json(self, 400, {"error": str(exc)})
                try:
                    self.store.close_show(sid)
                except Exception:
                    pass
                return _json(self, 200, {"deleted": sid})
            if parsed.path == "/api/episode":
                show_id = str(data.get("show_id") or "")
                episode = int(data.get("episode") or 0)
                if not show_id or episode <= 0:
                    return _json(self, 400, {"error": "show_id and episode are required"})
                cards = request_episode(self.store, show_id, episode)
                return _json(self, 200, {"cards": cards})
            if parsed.path == "/api/cards":
                card = self.store.create(data)
                return _json(self, 200, {"card": card})
            if parsed.path == "/api/cards/note":
                card = self.store.note(str(data.get("id") or ""),
                                       str(data.get("by") or "human"),
                                       str(data.get("text") or ""))
                return _json(self, 200, {"card": card})
            if parsed.path == "/api/cards/advance":
                cid = str(data.get("id") or "")
                dest = str(data.get("to") or "")
                if dest == "planned":
                    # A human retry (Unblock / Retry button) is a deliberate
                    # fresh start: reset the attempt counter so the daemon's
                    # retake-cap check doesn't instantly re-block the card.
                    # Automatic daemon retakes still increment and cap.
                    cur = self.store.get(cid) or {}
                    if str(cur.get("state") or "") in ("blocked", "retake"):
                        self.store.update(cid, attempts=0)
                card = self.store.advance(
                    cid, dest,
                    str(data.get("by") or "human"), str(data.get("reason") or ""))
                return _json(self, 200, {"card": card})
            if parsed.path == "/api/cards/reject_image":
                card_id = str(data.get("card_id") or "")
                path = str(data.get("path") or "")
                if not card_id or not path:
                    return _json(self, 400, {"error": "card_id and path are required"})
                try:
                    card, retake = _reject_image(
                        self.store, card_id, path, str(data.get("notes") or ""))
                except Exception as exc:
                    return _json(self, 400, {"error": str(exc)})
                return _json(self, 200, {"card": card, "retake": retake})
            if parsed.path == "/api/cards/approve_image":
                card_id = str(data.get("card_id") or "")
                path = str(data.get("path") or "")
                if not card_id or not path:
                    return _json(self, 400, {"error": "card_id and path are required"})
                try:
                    card = _approve_image(self.store, card_id, path)
                except Exception as exc:
                    return _json(self, 400, {"error": str(exc)})
                return _json(self, 200, {"card": card})
            if parsed.path == "/api/cards/reject_scene":
                card_id = str(data.get("card_id") or "")
                scene_id = str(data.get("scene_id") or "")
                if not card_id or not scene_id:
                    return _json(self, 400,
                                 {"error": "card_id and scene_id are required"})
                try:
                    card, retake = _reject_scene(
                        self.store, card_id, scene_id,
                        str(data.get("notes") or ""))
                except Exception as exc:
                    return _json(self, 400, {"error": str(exc)})
                return _json(self, 200, {"card": card, "retake": retake})
            if parsed.path == "/api/cards/review":
                card = self.store.requeue_review(str(data.get("id") or ""))
                if not card:
                    return _json(self, 400, {"error": "card is busy or missing"})
                return _json(self, 200, {"card": card})
            if parsed.path == "/api/cards/reject_voice":
                card_id = str(data.get("card_id") or "")
                path = str(data.get("path") or "")
                if not card_id or not path:
                    return _json(self, 400, {"error": "card_id and path are required"})
                try:
                    card, retake = _reject_voice(
                        self.store, card_id, path, str(data.get("notes") or ""))
                except Exception as exc:
                    return _json(self, 400, {"error": str(exc)})
                return _json(self, 200, {"card": card, "retake": retake})
            if parsed.path == "/api/cards/delete":
                cid = str(data.get("id") or "")
                if not self.store.get(cid):
                    return _json(self, 404, {"error": f"no card {cid!r}"})
                self.store.delete_card(cid)
                return _json(self, 200, {"deleted": True, "id": cid})
            if parsed.path == "/api/cards/patch":
                fields = data.get("fields") or {}
                if not isinstance(fields, dict):
                    return _json(self, 400, {"error": "fields must be an object"})
                card = self.store.update(str(data.get("id") or ""), **fields)
                return _json(self, 200, {"card": card})
            if parsed.path == "/api/gate0/decide":
                card_id = str(data.get("card_id") or "")
                action = str(data.get("action") or "").lower()
                notes = str(data.get("notes") or "")
                if action not in ("approve", "reject"):
                    return _json(self, 400, {"error": "action must be approve or reject"})
                card = self.store.get(card_id)
                if not card:
                    return _json(self, 404, {"error": f"no card {card_id!r}"})
                if str(card.get("reviewer") or "") != "human":
                    return _json(self, 400, {"error": "not a human-review card"})
                if str(card.get("kind") or "") == "image_retake":
                    try:
                        out = _decide_retake(self.store, card, action, notes)
                    except Exception as exc:
                        return _json(self, 400, {"error": str(exc)})
                    return _json(self, 200, {"card": out})
                from .. import approval
                from ..bootstrap import BootstrapChain
                from ..show import Show
                from . import gate0
                domain = gate0.KIND_DOMAINS.get(str(card.get("kind") or ""))
                if not domain:
                    return _json(self, 400, {"error": "not a Gate 0 card"})
                if action == "approve":
                    open_children = asset_lib.open_children(self.store, card_id)
                    if open_children:
                        return _json(self, 409, {
                            "error": f"{len(open_children)} child retake(s) still open",
                            "retakes": [c.get("id") for c in open_children]})
                show = Show(str(card.get("show_id") or ""))
                if domain == "scenes":
                    # Self-heal registries predating per-scene tracking:
                    # every on-disk location gets a pending entry unless
                    # already recorded.
                    _st = show.bootstrap_state() or {}
                    _locs = _st.setdefault("scenes", {}).setdefault("locations", {})
                    _changed = False
                    if show.scenes_dir.exists():
                        for _p in sorted(show.scenes_dir.glob("*.yaml")):
                            if _p.stem not in _locs:
                                _locs[_p.stem] = "pending"
                                _changed = True
                    if _changed:
                        show.set_bootstrap_state(_st)
                current = gate0.status(show, domain)
                card_char = str((card.get("payload") or {}).get("char") or "")
                if domain == "voices" and card_char:
                    pending = {"step": "voice", "char": card_char}
                else:
                    pending = current.get("pending") or {}
                if domain == "ideas" and action == "approve":
                    picks = data.get("picks") or []
                    if isinstance(picks, str):
                        picks = [picks]
                    picks = [str(p).strip() for p in picks if str(p).strip()]
                    ideas = show.read_ideas() or {}
                    pitches = ideas.get("pitches") or []
                    if not pitches:
                        return _json(self, 409, {"error":
                            "no pitches archived yet; the ideas card has not run"})
                    by_id = {str(p.get("id")): p for p in pitches if p.get("id")}
                    unknown = [p for p in picks if p not in by_id]
                    if unknown:
                        return _json(self, 400, {"error":
                            f"unknown pitch ids: {', '.join(unknown)}"})
                    if not picks:
                        return _json(self, 400, {"error":
                            "select at least one pitch"})
                    # The picked pitch supplies the show id: a provisional
                    # show (created without a name) takes the first pick's
                    # slug as its real id.
                    old_sid = str(card.get("show_id") or "")
                    wanted = str(by_id[picks[0]].get("show_id") or "").strip()
                    renamed = False
                    if ((card.get("payload") or {}).get("auto_id")
                            and wanted and wanted != old_sid):
                        from ..shows import rename_show as _rename_show
                        from .store import valid_show_id as _valid_show_id
                        if (_valid_show_id(wanted)
                                and not Path(self.cfg.show_path(wanted)).exists()):
                            _rename_show(old_sid, wanted)
                            try:
                                self.store.close_show(old_sid)
                            except Exception:
                                pass
                            old_sid = wanted
                            card_id = (f"g0.{wanted}.ideas"
                                       if card_id == f"g0.{card.get('show_id')}.ideas"
                                       else card_id.replace(
                                           f"g0.{card.get('show_id')}.",
                                           f"g0.{wanted}.", 1))
                            renamed = True
                            show = Show(old_sid)
                            ideas = show.read_ideas() or {}
                            pitches = ideas.get("pitches") or []
                            by_id = {str(p.get("id")): p for p in pitches
                                     if p.get("id")}
                    now = int(time.time())
                    for pid in picks:
                        if by_id[pid].get("status") != "selected":
                            by_id[pid]["status"] = "selected"
                            by_id[pid]["selected_at"] = now
                    ideas["active"] = picks[0]
                    show.write_ideas(ideas)
                    # Picking auto-approves: the first pick IS the concept.
                    first = by_id[picks[0]]
                    profile = self.cfg.get("show_profile") or {}
                    maturity = str(first.get("maturity")
                                   or profile.get("maturity") or "").strip()
                    show.write_concept({
                        "title": str(first.get("title") or ""),
                        "logline": str(first.get("logline") or ""),
                        "premise": str(first.get("premise") or ""),
                        "genre": list(first.get("genre") or []),
                        "tone": list(first.get("tone") or []),
                        "hook": str(first.get("hook") or ""),
                        "maturity": maturity,
                        "picked_from": picks[0],
                        "picked_pitches": picks,
                    })
                    bst = show.bootstrap_state() or {}
                    bst.setdefault("concept", {})["status"] = "approved"
                    show.set_bootstrap_state(bst)
                    from ..events import CONCEPT_APPROVED, new_event
                    from ..approval import _emit
                    _emit(show, CONCEPT_APPROVED,
                          {"manual": True, "via": "pitch-selection", "picks": picks})
                    BootstrapChain(show, cfg=self.cfg, llm=None).check_complete()
                    st = gate0.status(show, "ideas")
                    self.store.update(card_id, result={**st, "picks": picks})
                    try:
                        self.store.advance(card_id, "accepted", "human",
                            f"{len(picks)} pitch(es) selected; concept approved")
                    except ValueError:
                        pass
                    # A pick completes the ideas stage: accept every reviewing
                    # ideas card (original + any re-pitch revisions) so the
                    # board reflects the approval and downstream stages unblock.
                    for _c in self.store.all(show_id=old_sid):
                        if (gate0.KIND_DOMAINS.get(str(_c.get("kind") or ""))
                                != "ideas"
                                or str(_c.get("state") or "") != "reviewing"
                                or str(_c.get("id")) == card_id):
                            continue
                        try:
                            self.store.advance(
                                str(_c.get("id")), "accepted", "human",
                                f"pitch {picks[0]} selected; concept approved")
                        except ValueError:
                            pass
                    return _json(self, 200, {"card": self.store.get(card_id),
                        "status": st, "show_id": old_sid, "renamed": renamed,
                        "messages": [f"concept approved from pitch {picks[0]}"]})
                if action == "reject" and domain in ("plan", "ideas", "bible",
                                                    "characters", "scenes"):
                    # Reject sends the ORIGINAL card back for rework (no
                    # revision cards): the notes land on its payload so the
                    # rerun picks them up as feedback, the attempt counts
                    # toward the retake cap, and the daemon requeues the
                    # card to planned automatically.
                    pending_reject = current.get("pending") or {}
                    req_char = str(data.get("char") or "").strip()
                    if req_char:
                        # Per-item reject: scope the rework to one roster entry.
                        names = [str(c.get("name") or "") for c in
                                 (show.bootstrap_state() or {}).get("characters", [])]
                        if req_char not in names:
                            return _json(self, 400, {"error":
                                f"no character named {req_char!r}"})
                        pending_reject = {"step": "character", "char": req_char}
                    payload = dict(card.get("payload") or {})
                    payload["notes"] = notes or "rejected"
                    payload["step"] = pending_reject.get("step", "")
                    payload["char"] = pending_reject.get("char", "")
                    if data.get("scene"):
                        payload["scene"] = str(data.get("scene"))
                    # A human reject is a deliberate fresh start, not an
                    # automatic retry: reset the counter so the retake cap
                    # (which guards runaway workers) never blocks it.
                    # Worker-driven failures still increment and cap.
                    self.store.update(card_id,
                        result={**current, "rejected": notes or "rejected"},
                        payload=payload,
                        attempts=0)
                    try:
                        self.store.advance(card_id, "retake", "human",
                                           notes or "rejected")
                    except ValueError:
                        pass
                    return _json(self, 200, {"card": self.store.get(card_id),
                                               "status": current})
                if action == "reject" and domain in ("images", "voices"):
                    pending_reject = current.get("pending") or {}
                    step = str(pending_reject.get("step") or "")
                    ident = str(pending_reject.get("char") or card_char or "")
                    req_char = str(data.get("char") or "").strip()
                    req_scene = str(data.get("scene") or "").strip()
                    if domain == "images" and req_scene:
                        # Per-item reject from the Missing/failed refs rows:
                        # honor the requested scene, not the card's pending
                        # pointer (which may name an already-done item).
                        if req_scene not in show.list_scenes():
                            return _json(self, 400, {"error":
                                f"no scene named {req_scene!r}"})
                        step, ident = "scene", req_scene
                    elif domain == "images" and req_char:
                        # Same for characters: a stub has no artifact, so the
                        # pending pointer can never name it — without this the
                        # request silently rejects the wrong character's image.
                        names = [str(c.get("name") or "") for c in
                                 (show.bootstrap_state() or {}).get("characters", [])]
                        if req_char not in names:
                            return _json(self, 400, {"error":
                                f"no character named {req_char!r}"})
                        step, ident = "refs", req_char
                    if domain == "voices" and ident:
                        for art in card.get("artifacts") or []:
                            ast = art.get("asset") or {}
                            if (ast.get("type") == "voice"
                                    and str(ast.get("id") or "").lower()
                                    == ident.lower()):
                                asset_lib.mark_rejected(show, ast, notes)
                                break
                        # Re-arm the bootstrap flag or the rerun skips this
                        # voice as already done.
                        _vst = show.bootstrap_state()
                        for _e in _vst.get("characters", []):
                            if str(_e.get("name") or "").lower() == ident.lower():
                                _e["voice"] = "rejected"
                                _e["voice_notes"] = notes or "rejected"
                        show.set_bootstrap_state(_vst)
                        # Same card reworks the voice end to end (identity +
                        # text refresh, then re-record): no second card.
                        payload = dict(card.get("payload") or {})
                        payload["notes"] = notes or "rejected"
                        payload["char"] = ident
                        self.store.update(card_id,
                            result={**current, "rejected": notes or "rejected"},
                            payload=payload, attempts=0)
                        try:
                            self.store.advance(card_id, "retake", "human",
                                               notes or "rejected")
                        except ValueError:
                            pass
                        return _json(self, 200, {"card": self.store.get(card_id),
                                                  "status": current})
                    if domain == "images" and ident:
                        target_type = "scene" if step == "scene" else "character"
                        path = ""
                        for art in card.get("artifacts") or []:
                            ast = art.get("asset") or {}
                            if (ast.get("type") == target_type
                                    and str(ast.get("id") or "").lower()
                                    == ident.lower()):
                                path = str(art.get("path") or "")
                                break
                        if path:
                            # _reject_image already reworks the same card and
                            # moves it to retake; nothing left to block here.
                            try:
                                updated, _ = _reject_image(
                                    self.store, card_id, path, notes)
                            except Exception as exc:
                                return _json(self, 400, {"error": str(exc)})
                            return _json(self, 200, {
                                "card": updated, "status": current})
                        # No artifact matched: the item is a stub/missing ref
                        # with no file to archive. Scope the rerun to it and
                        # requeue the card; advance_images regenerates stubs.
                        payload = dict(card.get("payload") or {})
                        payload["notes"] = notes or "rejected"
                        if target_type == "scene":
                            payload["scene"] = ident
                        else:
                            payload["char"] = ident
                        self.store.update(card_id,
                            result={**current,
                                    "rejected": notes or "rejected"},
                            payload=payload, attempts=0)
                        self.store.note(card_id, "human",
                            f"Rejected missing {target_type} reference: "
                            f"{ident}: {notes or 'no notes'}; reworking on "
                            f"this card")
                        try:
                            self.store.advance(card_id, "retake", "human",
                                               notes or "rejected")
                        except ValueError:
                            pass
                        return _json(self, 200, {
                            "card": self.store.get(card_id),
                            "status": current})
                if action == "reject":
                    # Reject sends the ORIGINAL card back for rework (no
                    # revision cards, no blocked dead-ends): notes land on
                    # its payload so the rerun picks them up, the attempt
                    # counts toward the retake cap, and the daemon requeues
                    # to planned automatically. Fix cards keep their
                    # existing flow (a rerun would mint duplicate renders).
                    if str(card.get("kind") or "") == "fix":
                        self.store.update(card_id, result={
                            **current, "rejected": notes or "rejected"})
                        try:
                            self.store.advance(card_id, "blocked", "human",
                                                 notes or "rejected")
                        except ValueError:
                            pass
                        return _json(self, 200, {"card": self.store.get(card_id),
                                                  "status": current})
                    payload = dict(card.get("payload") or {})
                    payload["notes"] = notes or "rejected"
                    if data.get("scene"):
                        payload["scene"] = str(data.get("scene"))
                    self.store.update(card_id,
                        result={**current, "rejected": notes or "rejected"},
                        payload=payload,
                        attempts=0)
                    if domain == "voices" and card_char:
                        st = show.bootstrap_state()
                        for entry in st.get("characters", []):
                            if str(entry.get("name") or "").lower() == card_char.lower():
                                entry["voice"] = ""
                        show.set_bootstrap_state(st)
                    try:
                        self.store.advance(card_id, "retake", "human",
                                           notes or "rejected")
                    except ValueError:
                        pass
                    return _json(self, 200, {"card": self.store.get(card_id),
                                               "status": current})
                if not pending:
                    if card.get("state") != "accepted":
                        try:
                            self.store.advance(card_id, "accepted", "human",
                                               f"{domain} approved")
                        except ValueError:
                            pass
                    return _json(self, 200, {"card": self.store.get(card_id),
                                             "status": current, "messages": []})
                step = str(pending.get("step") or "")
                char = str(pending.get("char") or "")
                show_id = str(show.show_id)
                req_char = str(data.get("char") or "").strip()
                if req_char and domain in ("characters", "voices"):
                    # Per-item approve: target one roster entry instead of
                    # the first pending one.
                    names = [str(c.get("name") or "") for c in
                             (show.bootstrap_state() or {}).get("characters", [])]
                    if req_char not in names:
                        return _json(self, 400, {"error":
                            f"no character named {req_char!r}"})
                    entry = next((c for c in
                                  (show.bootstrap_state() or {}).get("characters", [])
                                  if str(c.get("name") or "") == req_char), {})
                    key = "proposal" if domain == "characters" else "voice"
                    if entry.get(key) == "approved":
                        return _json(self, 200, {"card": self.store.get(card_id),
                            "status": current,
                            "messages": [f"{req_char} is already approved"]})
                    step = "character" if domain == "characters" else "voice"
                    char = req_char
                if step == "scene" and domain == "images":
                    return _json(self, 409, {
                        "error": "scene ref is pending; approve its retake card first"})
                if domain == "plan" and step == "character":
                    messages = approval.approve_proposal(show_id, char, notes)
                elif domain == "scenes" and data.get("all"):
                    messages = []
                    bloc = (show.bootstrap_state() or {}).get("scenes", {})
                    for sid, s in list((bloc.get("locations") or {}).items()):
                        if s == "approved":
                            continue
                        try:
                            messages.extend(approval.approve_step(
                                show_id, "scene", slug=sid, notes=notes,
                                advance=False))
                        except ValueError:
                            pass
                elif domain == "scenes" and step == "scene":
                    sid = str(data.get("scene") or pending.get("scene") or "").strip()
                    if not sid:
                        return _json(self, 400, {"error":
                            "no scene selected for approval"})
                    messages = approval.approve_step(
                        show_id, "scene", slug=sid, notes=notes, advance=False)
                elif domain == "characters" and data.get("all"):
                    messages = []
                    bst = show.bootstrap_state() or {}
                    for entry in list(bst.get("characters") or []):
                        if entry.get("proposal") == "approved":
                            continue
                        nm = str(entry.get("name") or "")
                        if not nm:
                            continue
                        try:
                            messages.extend(approval.approve_step(
                                show_id, "character", char=nm, notes=notes,
                                advance=False))
                        except ValueError:
                            pass
                elif domain == "images":
                    messages = approval.approve_step(show_id, "refs", char=char,
                                                     notes=notes, advance=False)
                elif domain == "voices":
                    messages = approval.approve_step(show_id, "voice", char=char,
                                                     notes=notes, advance=False)
                else:
                    messages = approval.approve_step(show_id, step, char=char,
                                                     notes=notes, advance=False)
                BootstrapChain(show, cfg=self.cfg, llm=None).check_complete()
                st = gate0.status(show, domain)
                card_done = not st.get("pending")
                if domain == "voices" and card_char:
                    entry = next((c for c in st.get("characters", [])
                                  if str(c.get("name") or "").lower()
                                  == card_char.lower()), {})
                    card_done = entry.get("voice") == "approved"
                    st["pending"] = (None if card_done
                                     else {"step": "voice", "char": card_char})
                self.store.update(card_id, result={**st, "messages": messages})
                if card_done:
                    child_parent = str((card.get("payload") or {}).get("parent") or "")
                    child_asset = (card.get("payload") or {}).get("asset") or {}
                    if child_parent and child_asset.get("path"):
                        asset_lib.update_artifact(
                            self.store, child_parent, child_asset["path"],
                            status="accepted", retake_id=card_id)
                        self.store.note(
                            child_parent, "human",
                            f"Retake accepted for {asset_lib.asset_title(child_asset)}")
                    self.store.advance(card_id, "accepted", "human",
                                       f"{domain} approved")
                elif card.get("state") != "reviewing":
                    try:
                        self.store.advance(card_id, "reviewing", "human", "Gate 0")
                    except ValueError:
                        pass
                return _json(self, 200, {"card": self.store.get(card_id),
                                         "status": st, "messages": messages})
            if parsed.path.startswith("/api/tools/"):
                name = parsed.path[len("/api/tools/"):]
                run_id = str(data.get("run_id") or "")
                card_id = str(data.get("card_id") or "")
                run = self.store.run(run_id)
                card = self.store.get(card_id)
                if not run or not card:
                    return _json(self, 404, {"error": "unknown run or card"})
                if run.get("card_id") != card_id or card.get("claimed_by") != run_id:
                    return _json(self, 409, {"error": "run does not own this card"})
                role = load_roles(self.cfg).get(str(run.get("role") or ""))
                if not role:
                    return _json(self, 400, {"error": "unknown role"})
                from . import tools as tools_mod
                spec = tools_mod.TOOLS.get(name)
                if data.get("async") or (spec and spec.long):
                    jid = _job_start(self.store, self.cfg, name, run_id,
                                     card_id, data.get("args") or {})
                    return _json(self, 200, {"job_id": jid, "state": "running"})
                result = _run_board_tool(self.store, self.cfg, name, run_id,
                                         card_id, data.get("args") or {})
                return _json(self, 200, {"result": result})
            if parsed.path == "/api/runs/finish":
                run_id = str(data.get("run_id") or "")
                if not self.store.run(run_id):
                    return _json(self, 404, {"error": f"no run {run_id!r}"})
                final = {k: data.get(k) for k in
                         ("status", "verdict", "summary", "notes", "error")
                         if data.get(k) is not None}
                self.store.set_run_final(run_id, final)
                return _json(self, 200, {"ok": True})
            if parsed.path == "/api/version":
                import os as _os
                def _mt(name):
                    try:
                        return int(_os.path.getmtime(
                            Path(self.cfg.root) / name))
                    except OSError:
                        return 0
                return _json(self, 200, {
                    "board_js": _mt("board/board.js"),
                    "api": _mt("studio/board/api.py"),
                    "daemon": _mt("studio/board/daemon.py"),
                })
            if parsed.path == "/api/daemon":
                enabled = bool(data.get("enabled", True))
                if enabled:
                    self.daemon.start()
                else:
                    self.daemon.stop()
                return _json(self, 200, {"enabled": enabled})
            return _json(self, 404, {"error": "not found"})
        except Exception as exc:
            return _json(self, 400, {"error": f"{type(exc).__name__}: {exc}"})


class BoardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def make_server(store: BoardSet, daemon: BoardDaemon, cfg,
                port: int = DEFAULT_PORT,
                bind: str = "127.0.0.1") -> ThreadingHTTPServer:
    handler = type("Handler", (BoardHandler,), {
        "store": store, "daemon": daemon, "cfg": cfg})
    return BoardHTTPServer((bind, int(port)), handler)


def serve(port: int = DEFAULT_PORT, bind: str = "127.0.0.1",
          start_daemon: bool = True, cfg=None) -> ThreadingHTTPServer:
    cfg = cfg or get_config()
    store = BoardSet(cfg, adopt=True)
    daemon = BoardDaemon(store, cfg)
    httpd = make_server(store, daemon, cfg, port=port, bind=bind)
    if start_daemon:
        daemon.start()
    thread = threading.Thread(target=httpd.serve_forever, name="board-http",
                              daemon=True)
    thread.start()
    return httpd
