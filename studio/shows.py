"""Show lifecycle actions (create from a seed idea / delete) for the dashboard & CLI."""
from __future__ import annotations

import re
import shutil

import yaml

from .config import get_config
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
            "runtime_target_s": cfg.get("show_profile", "runtime_target_s", 1320),
            "style_guide": "",  # filled by the Gate 0 bootstrap
            "quality_baseline": cfg.get("show_profile", "baseline", "ranma-1-2"),
        },
        "arcs": [],
        "overall_plotline": None,
        "plotlines": [],
        "content_policy": cfg.get("show_profile", "maturity", "mature"),
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
    from .bus.events import CONCEPT_APPROVED, new_event
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
    concept["maturity"] = cfg.get("show_profile", "maturity", "mature")
    show.write_concept(concept)
    st = show.bootstrap_state()
    st["concept"] = {"status": "approved"}
    show.set_bootstrap_state(st)
    _emit(show, CONCEPT_APPROVED, {"manual": True, "via": "proposal-selection"})
    for line in BootstrapChain(show).advance():
        pass
    return show_id


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
