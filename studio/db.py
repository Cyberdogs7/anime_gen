"""SQLite persistence - the source of truth for run/asset/approval state.

Single-writer (the run controller on node B). See DESIGN.md §8.8 / §16.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, show_id TEXT NOT NULL, episode INT NOT NULL, status TEXT,
  started_at TEXT, finished_at TEXT, operating_mode TEXT, nightly_budget_min INT,
  progress_json TEXT, UNIQUE(show_id, episode));
CREATE TABLE IF NOT EXISTS stages(
  id INTEGER PRIMARY KEY, run_id INT REFERENCES runs(id), stage INT, name TEXT,
  status TEXT, started_at TEXT, finished_at TEXT, input_hash TEXT, output_path TEXT);
CREATE TABLE IF NOT EXISTS shots(
  id TEXT PRIMARY KEY, run_id INT, show_id TEXT, shot_idx INT, scene_id TEXT, status TEXT,
  duration_s REAL, grid_frames INT, attempts INT, video_asset_id TEXT, audio_asset_id TEXT,
  seed INT, prompt_hash TEXT, qc_json TEXT);
CREATE TABLE IF NOT EXISTS assets(
  id TEXT PRIMARY KEY, show_id TEXT, run_id INT, kind TEXT, stage INT, path TEXT,
  prompt_hash TEXT, seed INT, checksum TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS retakes(
  id TEXT PRIMARY KEY, shot_id TEXT, reason TEXT, attempt INT, budget_hit INT, status TEXT);
CREATE TABLE IF NOT EXISTS continuity(
  show_id TEXT PRIMARY KEY, state_json TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS qc_results(
  id TEXT PRIMARY KEY, shot_id TEXT, checks_json TEXT, composite REAL,
  verdict TEXT, passed INT, created_at TEXT);
CREATE TABLE IF NOT EXISTS script_reviews(
  id TEXT PRIMARY KEY, run_id INT, show_id TEXT, episode INT, round INT,
  reviewer TEXT, script_version TEXT, status TEXT, score REAL, notes_json TEXT, created_at TEXT,
  UNIQUE(show_id, episode, round, reviewer));
CREATE TABLE IF NOT EXISTS policy_events(
  id INTEGER PRIMARY KEY, run_id INT, show_id TEXT, stage INT, artifact_hash TEXT,
  verdict TEXT, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY, type TEXT, stream TEXT, show_id TEXT, ts TEXT, json TEXT);
"""


class DB:
    """Thin sqlite3 wrapper. Not thread-safe by design; one writer at a time."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- events (dedup / audit log) ----

    def event_seen(self, event_id: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,))
        return cur.fetchone() is not None

    def record_event(self, event_id: str, etype: str, stream: str, show_id: str, ts: str,
                     payload: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO events(event_id, type, stream, show_id, ts, json) "
            "VALUES(?,?,?,?,?,?)",
            (event_id, etype, stream, show_id, ts, json.dumps(payload)),
        )
        self.conn.commit()

    # ---- shows / continuity ----

    def get_continuity(self, show_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT state_json FROM continuity WHERE show_id = ?", (show_id,)
        )
        row = cur.fetchone()
        return json.loads(row["state_json"]) if row else None

    def set_continuity(self, show_id: str, state: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO continuity(show_id, state_json, updated_at) VALUES(?,?,datetime('now')) "
            "ON CONFLICT(show_id) DO UPDATE SET state_json=excluded.state_json, "
            "updated_at=datetime('now')",
            (show_id, json.dumps(state)),
        )
        self.conn.commit()

    # ---- runs ----

    def create_run(self, show_id: str, episode: int, mode: str, budget_min: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(show_id, episode, status, started_at, operating_mode, "
            "nightly_budget_min) VALUES(?,?,?,datetime('now'),?,?) "
            "ON CONFLICT(show_id, episode) DO UPDATE SET status='booting' RETURNING id",
            (show_id, episode, "booting", mode, budget_min),
        )
        run_id = cur.fetchone()["id"]
        self.conn.commit()
        return run_id

    def update_run(self, run_id: int, **fields: Any) -> None:
        allowed = {"status", "finished_at", "progress_json"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                vals.append(v)
        if not sets:
            return
        vals.append(run_id)
        self.conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id = ?", vals)
        self.conn.commit()

    def latest_episode(self, show_id: str) -> int:
        cur = self.conn.execute(
            "SELECT COALESCE(MAX(episode), 0) AS ep FROM runs WHERE show_id = ?", (show_id,)
        )
        return cur.fetchone()["ep"]

    # ---- stages ----

    def set_stage(self, run_id: int, stage_no: int, name: str, status: str,
                  input_hash: str = "", output_path: str = "") -> None:
        self.conn.execute(
            "INSERT INTO stages(run_id, stage, name, status, started_at, finished_at, "
            "input_hash, output_path) VALUES(?,?,?,?,datetime('now'),"
            "CASE WHEN ?='complete' THEN datetime('now') END,?,?)",
            (run_id, stage_no, name, status, status, input_hash, output_path),
        )
        self.conn.commit()

    def stage_status(self, run_id: int, stage_no: int) -> str | None:
        cur = self.conn.execute(
            "SELECT status FROM stages WHERE run_id = ? AND stage = ? ORDER BY id DESC LIMIT 1",
            (run_id, stage_no),
        )
        row = cur.fetchone()
        return row["status"] if row else None

    # ---- assets / shots (minimal for M0) ----

    def register_asset(self, asset_id: str, show_id: str, run_id: int, kind: str, stage: int,
                       path: str, prompt_hash: str = "", seed: int = 0) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO assets(id, show_id, run_id, kind, stage, path, prompt_hash, "
            "seed, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))",
            (asset_id, show_id, run_id, kind, stage, path, prompt_hash, seed, "ok"),
        )
        self.conn.commit()

    def mark_shot(self, shot_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(shot_id)
        self.conn.execute(f"UPDATE shots SET {', '.join(sets)} WHERE id = ?", vals)
        self.conn.commit()

    def add_shot(self, shot_id: str, run_id: int, show_id: str, shot_idx: int,
                 scene_id: str, duration_s: float, grid_frames: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO shots(id, run_id, show_id, shot_idx, scene_id, status, "
            "duration_s, grid_frames) VALUES(?,?,?,?,?,?,?,?)",
            (shot_id, run_id, show_id, shot_idx, scene_id, "pending", duration_s, grid_frames),
        )
        self.conn.commit()

    def log_policy(self, run_id: int, show_id: str, stage: int, artifact_hash: str,
                   verdict: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO policy_events(run_id, show_id, stage, artifact_hash, verdict, reason, "
            "created_at) VALUES(?,?,?,?,?,?,datetime('now'))",
            (run_id, show_id, stage, artifact_hash, verdict, reason),
        )
        self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()


def open_db(path: Path | str | None = None) -> DB:
    """Open the studio DB, defaulting to the config path."""
    if path is None:
        from .config import get_config
        path = get_config().db_path
    return DB(path)
