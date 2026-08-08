"""Run controller - run lifecycle, resume, budget (M0 skeleton).

At M0 this handles RunRequested -> create run -> RunStarted, and RunStarted
emission. Stage consumers (dev/script/...) are registered in cli.serve.
"""
from __future__ import annotations

import logging

from .bus import broker as broker_mod
from .bus.events import (
    RUN_ABORTED, RUN_REQUESTED, RUN_STARTED, Event, new_event,
)
from .config import get_config
from .db import DB

log = logging.getLogger(__name__)

#: ordered stage names, 1-indexed (DESIGN.md §9)
STAGES = [
    "development",        # 1
    "script",             # 2
    "script_review",      # 2a
    "keyframes",          # 3
    "validation",         # 4
    "shot_production",    # 5
    "dialogue",           # 6
    "assembly",           # 7
    "qc",                 # 8
    "delivery",           # 9
]


class RunController:
    def __init__(self, cfg, db: DB, bus: "broker_mod.BaseBroker", source_node: str = "worker-B"):
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self.source_node = source_node

    def on_run_requested(self, event: Event) -> None:
        if event.type != RUN_REQUESTED:      # only react to RunRequested, not our own emissions
            return
        show_id = event.show_id or event.payload.get("show_id", "")
        if not show_id:
            log.error("RunRequested without show_id; ignoring")
            return
        mode = event.payload.get("mode", self.cfg.get("pipeline", "mode", "overnight"))
        budget = int(event.payload.get("budget_min", self.cfg.get("pipeline", "budget_min", 480)))

        run_id, episode, resumed = self._next_run(show_id)
        if not resumed:
            self.db.set_stage(run_id, 1, STAGES[0], "running")
        log.info("run %s for show %s episode %d (mode=%s, budget=%dmin)%s",
                 run_id, show_id, episode, mode, budget, " [resumed]" if resumed else "")
        self.bus.publish(
            "bus:run",
            new_event(RUN_STARTED, show_id=show_id,
                      run_id=f"run-{run_id}", episode=episode,
                      payload={"run_id": run_id, "mode": mode, "budget_min": budget}),
        )

    def _next_run(self, show_id: str) -> tuple[int, int, bool]:
        """Find the in-flight run to resume, or start the next episode.

        Returns (run_id, episode, resumed). A run whose status is still
        booting/running is resumed rather than a new episode started.
        """
        row = self.db.query(
            "SELECT id, episode, status FROM runs WHERE show_id=? "
            "ORDER BY episode DESC, id DESC LIMIT 1", (show_id,))
        if row and row[0]["status"] in ("booting", "running"):
            r = row[0]
            return r["id"], r["episode"], True
        state = self.db.get_continuity(show_id)
        if state and state.get("episode", 0) > 0:
            episode = int(state["episode"]) + 1
        else:
            episode = self.db.latest_episode(show_id) + 1
        run_id = self.db.create_run(show_id, episode, "overnight", 480)
        return run_id, episode, False

    def mark_aborted(self, run_id: int, reason: str) -> None:
        self.db.update_run(run_id, status="aborted")
        self.bus.publish("bus:run", new_event(RUN_ABORTED, payload={"reason": reason}))


def make_run_controller(cfg=None, db=None, bus=None) -> RunController:
    cfg = cfg or get_config()
    db = db or DB(cfg.db_path)
    bus = bus or broker_mod.make_broker(cfg["bus"])
    return RunController(cfg, db, bus)
