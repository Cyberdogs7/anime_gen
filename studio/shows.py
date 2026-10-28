"""Show lifecycle actions (create from a seed idea / delete) for the board & CLI."""
from __future__ import annotations

import re
import shutil

import yaml

from .config import DEFAULT_RUNTIME_TARGET_S, get_config
from .db import DB
from .show import Show


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def _skeleton(show_id: str) -> None:
    cfg = get_config()
    show_dir = cfg.show_path(show_id)
    for sub in ("characters", "voices", "scenes"):
        (show_dir / sub).mkdir(parents=True, exist_ok=True)
    (show_dir / "continuity").mkdir(parents=True, exist_ok=True)
    bible = {
        "series": {
            "title": show_id,
            "genre": cfg.get("show_profile", "genre", []),
            "tone": cfg.get("show_profile", "tone", []),
            "language": "en",
            "runtime_target_s": cfg.get("show_profile", "runtime_target_s", DEFAULT_RUNTIME_TARGET_S),
            "style_guide": "",  # rendering/linework only; filled by the Gate 0 bootstrap
            "art_direction": {},  # camera/lighting/palette reference; never in prompts
            # Per-show style references (set these in the show's bible).
            "baseline": "",
            "fanservice_baseline": "",
    # Optional per-show art medium for reference images; empty = the show
    # declares no medium (the code imposes none).
            "medium": "",
        },
        "arcs": [],
        "overall_plotline": None,
        "plotlines": [],
        "content_policy": cfg.get("show_profile", "maturity") or "",
    }
    (show_dir / "bible.yaml").write_text(
        yaml.safe_dump(bible, sort_keys=False, allow_unicode=True), encoding="utf-8",
    )
    db = DB(cfg.db_path)
    db.set_continuity(show_id, {"episode": 0, "world": {}, "characters": {},
                                "plotlines": [], "unresolved_threads": [],
                                "continuity_rules": []})
    db.close()


def create_show(name: str, brief: str = "", auto: bool = False) -> str:
    """Create a show skeleton, then run Gate 0 concept generation from the seed brief.

    Mirrors `init-show`: the concept is generated and left `pending` for human
    approval (unless `auto`, which auto-approves the whole bootstrap).
    """
    show_id = (name or "").strip().lower().replace(" ", "-")
    if not show_id:
        raise ValueError("a show name is required")
    cfg = get_config()
    if (cfg.show_path(show_id) / "bible.yaml").exists():
        raise ValueError(f"show '{show_id}' already exists")
    _skeleton(show_id)
    if brief.strip() or auto:
        from .bootstrap import BootstrapChain
        chain = BootstrapChain(Show(show_id))
        if auto:
            chain._auto_override = True
        chain.advance(brief=brief)
    return show_id


def create_show_from_proposal(proposal: dict, name: str = "") -> str:
    """Create a show from a selected showrunner concept proposal.

    The owner's pick is the approval: the concept is written as-is and marked
    approved (selection = approval), then the chain advances to generate the
    bible, which lands `pending` for review.
    """
    from .approval import _emit
    from .events import CONCEPT_APPROVED, new_event
    from .bootstrap import BootstrapChain

    cfg = get_config()
    title = (proposal.get("title") or "").strip()
    show_id = (name or "").strip().lower().replace(" ", "-") or _slug(title)
    if not show_id:
        raise ValueError("a show name or proposal title is required")
    if (cfg.show_path(show_id) / "bible.yaml").exists():
        raise ValueError(f"show '{show_id}' already exists")
    _skeleton(show_id)

    show = Show(show_id)
    concept = dict(proposal)
    # The selected proposal's rating IS the show's rating; the profile is only a
    # fallback for proposals that carry none.
    concept["maturity"] = str(proposal.get("maturity")
                              or cfg.get("show_profile", "maturity") or "").strip()
    show.write_concept(concept)
    st = show.bootstrap_state()
    st["concept"] = {"status": "approved"}
    show.set_bootstrap_state(st)
    _emit(show, CONCEPT_APPROVED, {"manual": True, "via": "proposal-selection"})
    for line in BootstrapChain(show).advance():
        pass
    return show_id


def rename_show(old_id: str, new_id: str, cfg=None) -> str:
    """Rename a show: folder plus every board reference embedding the old id.

    Used when a picked pitch supplies the show id: the show was created under
    a provisional id, and the pitch's slug becomes its real one. Card ids
    (``g0.<show>.<domain>``), ``show_id`` columns, ``depends_on`` entries and
    ``shows/<id>/``-prefixed artifact paths are all rewritten. Run ids and
    their log dirs are history and stay as-is (still valid paths).
    """
    import logging
    log = logging.getLogger("shows")
    cfg = cfg or get_config()
    old_id = (old_id or "").strip()
    new_id = (new_id or "").strip()
    if new_id == old_id:
        return old_id
    for label, sid in (("old", old_id), ("new", new_id)):
        if not sid or "/" in sid or "\\" in sid or sid in (".", ".."):
            raise ValueError(f"refusing to rename {label} show id {sid!r}")
    from .board.store import valid_show_id
    if not valid_show_id(new_id):
        raise ValueError(f"refusing to rename to invalid show id {new_id!r}")
    shows_root = cfg.shows_dir.resolve()
    src = cfg.show_path(old_id).resolve()
    dst = cfg.show_path(new_id).resolve()
    for target in (src, dst):
        if target == shows_root or not str(target).startswith(str(shows_root) + "\\"):
            raise ValueError(f"refusing to rename outside the shows dir: {target}")
    if not src.exists():
        raise ValueError(f"no such show {old_id!r}")
    if dst.exists():
        raise ValueError(f"show '{new_id}' already exists")

    from .board.store import close_show_stores
    close_show_stores(old_id)

    import sqlite3
    shutil.move(str(src), str(dst))
    log.info("rename_show %s -> %s", old_id, new_id)

    old_card = f"g0.{old_id}."
    new_card = f"g0.{new_id}."
    old_ep = f"{old_id}."
    new_ep = f"{new_id}."
    old_art = f"shows/{old_id}/"
    new_art = f"shows/{new_id}/"

    def _swap_id(cid: str) -> str:
        if cid.startswith(old_card):
            return new_card + cid[len(old_card):]
        if cid.startswith(old_ep):
            return new_ep + cid[len(old_ep):]
        return cid

    import json as _json
    db_path = dst / "board" / "board.db"
    if db_path.exists():
        con = sqlite3.connect(str(db_path))
        try:
            cards = con.execute(
                "SELECT id, show_id, depends_on, artifacts, payload FROM board_cards"
            ).fetchall()
            for cid, sid, deps, arts, payload in cards:
                nid = _swap_id(str(cid or ""))
                try:
                    dep_list = _json.loads(deps or "[]")
                    dep_list = [_swap_id(str(d)) for d in dep_list]
                    new_deps = _json.dumps(dep_list)
                except Exception:
                    new_deps = deps
                try:
                    art_list = _json.loads(arts or "[]")
                    for a in art_list:
                        p = str(a.get("path") or "")
                        if p.startswith(old_art):
                            a["path"] = new_art + p[len(old_art):]
                    new_arts = _json.dumps(art_list)
                except Exception:
                    new_arts = arts
                con.execute(
                    "UPDATE board_cards SET id=?, show_id=?, depends_on=?, "
                    "artifacts=? WHERE id=?", (nid, new_id, new_deps, new_arts, cid))
                con.execute("UPDATE board_runs SET card_id=? WHERE card_id=?",
                            (nid, cid))
                con.execute("UPDATE board_history SET card_id=? WHERE card_id=?",
                            (nid, cid))
            con.commit()
        finally:
            con.close()

    # Central studio.db rows that key on show_id move with the show.
    db = DB(cfg.db_path)
    con = db.conn
    try:
        for table in ("continuity", "policy_events", "events", "assets",
                      "script_reviews", "runs", "shots"):
            try:
                con.execute(f"UPDATE {table} SET show_id=? WHERE show_id=?",
                            (new_id, old_id))
            except Exception:
                pass
        try:
            rows = con.execute(
                "SELECT id FROM board_cards WHERE show_id=?", (old_id,)).fetchall()
            for (cid,) in rows:
                nid = _swap_id(str(cid or ""))
                con.execute("UPDATE board_cards SET id=?, show_id=? WHERE id=?",
                            (nid, new_id, cid))
                con.execute("UPDATE board_runs SET card_id=? WHERE card_id=?",
                            (nid, cid))
                con.execute("UPDATE board_history SET card_id=? WHERE card_id=?",
                            (nid, cid))
        except Exception:
            pass
        con.commit()
    finally:
        db.close()
    return new_id


def delete_show(show_id: str) -> None:
    """Delete a show: its DB rows (cascade order) and its folder under shows/.

    Hardened so it can never touch anything outside the target show: the id is
    validated, the resolved target must sit strictly inside the shows dir, and
    the folder is moved to ``<root>/.trash/<id>`` (recoverable) rather than
    permanently removed.
    """
    import logging
    log = logging.getLogger("shows")
    cfg = get_config()
    show_id = (show_id or "").strip()
    if not show_id or "/" in show_id or "\\" in show_id or show_id in (".", ".."):
        raise ValueError(f"refusing to delete invalid show id {show_id!r}")
    shows_root = cfg.shows_dir.resolve()
    target = cfg.show_path(show_id).resolve()
    if target == shows_root or not str(target).startswith(str(shows_root) + "\\"):
        raise ValueError(f"refusing to delete outside the shows dir: {target}")
    log.info("delete_show %s -> %s (db cleanup + move to trash)", show_id, target)

    from .board.store import close_show_stores
    close_show_stores(show_id)

    db = DB(cfg.db_path)
    con = db.conn
    try:
        run_ids = [r[0] for r in con.execute("SELECT id FROM runs WHERE show_id=?", (show_id,))]
        shot_ids = [r[0] for r in con.execute("SELECT id FROM shots WHERE show_id=?", (show_id,))]

        def _in(ids: list) -> str:
            return ",".join("?" for _ in ids) if ids else "NULL"

        if shot_ids:
            ph = _in(shot_ids)
            con.execute(f"DELETE FROM retakes WHERE shot_id IN ({ph})", shot_ids)
            con.execute(f"DELETE FROM qc_results WHERE shot_id IN ({ph})", shot_ids)
        if run_ids:
            ph = _in(run_ids)
            con.execute(f"DELETE FROM stages WHERE run_id IN ({ph})", run_ids)
            con.execute(f"DELETE FROM shots WHERE run_id IN ({ph})", run_ids)
        con.execute("DELETE FROM runs WHERE show_id=?", (show_id,))
        con.execute("DELETE FROM shots WHERE show_id=?", (show_id,))
        con.execute("DELETE FROM assets WHERE show_id=?", (show_id,))
        con.execute("DELETE FROM script_reviews WHERE show_id=?", (show_id,))
        con.execute("DELETE FROM continuity WHERE show_id=?", (show_id,))
        con.execute("DELETE FROM policy_events WHERE show_id=?", (show_id,))
        con.execute("DELETE FROM events WHERE show_id=?", (show_id,))
        card_ids = [r[0] for r in con.execute(
            "SELECT id FROM board_cards WHERE show_id=?", (show_id,))]
        if card_ids:
            ph = _in(card_ids)
            con.execute(f"DELETE FROM board_history WHERE card_id IN ({ph})", card_ids)
            con.execute(f"DELETE FROM board_runs WHERE card_id IN ({ph})", card_ids)
        con.execute("DELETE FROM board_cards WHERE show_id=?", (show_id,))
        con.commit()
    finally:
        db.close()

    if target.exists():
        trash = cfg.root / ".trash" / show_id
        if trash.exists():
            shutil.rmtree(trash, ignore_errors=True)
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(trash))
        log.info("delete_show: moved %s -> %s", target, trash)
