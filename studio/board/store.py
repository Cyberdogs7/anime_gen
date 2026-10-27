"""SQLite store for the agent board. One writer, one lock, atomic transitions."""
from __future__ import annotations

import json
import logging
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .cards import can_move, now, validate

log = logging.getLogger(__name__)

_JSON_FIELDS = ("payload", "result", "artifacts", "notes", "units", "depends_on")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS board_cards (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    show_id TEXT NOT NULL DEFAULT '',
    episode INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT '',
    reviewer TEXT NOT NULL DEFAULT '',
    lane TEXT NOT NULL DEFAULT 'llm',
    state TEXT NOT NULL DEFAULT 'planned',
    priority INTEGER NOT NULL DEFAULT 3,
    payload TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}',
    artifacts TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '[]',
    units TEXT NOT NULL DEFAULT '[]',
    depends_on TEXT NOT NULL DEFAULT '[]',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    claimed_by TEXT,
    claimed_at REAL,
    created TEXT NOT NULL DEFAULT '',
    updated TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS board_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    actor TEXT,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS board_runs (
    id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    role TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'produce',
    lane TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'running',
    started REAL NOT NULL,
    ended REAL,
    error TEXT,
    result TEXT
);
CREATE TABLE IF NOT EXISTS board_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS board_cards_state ON board_cards(state);
CREATE INDEX IF NOT EXISTS board_cards_lane ON board_cards(lane);
CREATE INDEX IF NOT EXISTS board_runs_state ON board_runs(state);
"""


def _enc(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _dec(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class BoardStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._subs: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- push updates ----
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._sub_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            if q in self._subs:
                self._subs.remove(q)

    def _emit(self, event: dict) -> None:
        with self._sub_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- meta ----
    def meta_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM board_meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO board_meta(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
            self._conn.commit()

    # ---- rows ----
    def _row_to_card(self, row: sqlite3.Row) -> dict[str, Any]:
        card = dict(row)
        for key in _JSON_FIELDS:
            default = {} if key in ("payload", "result") else []
            card[key] = _dec(card.get(key), default)
        return card

    def _row_to_run(self, row: sqlite3.Row) -> dict[str, Any]:
        run = dict(row)
        run["result"] = _dec(run.get("result"), {})
        return run

    def _history(self, card_id: str, from_state: str | None,
                 to_state: str, actor: str, reason: str = "") -> None:
        self._conn.execute(
            "INSERT INTO board_history(card_id, ts, from_state, to_state, actor, reason) "
            "VALUES (?,?,?,?,?,?)",
            (card_id, now(), from_state, to_state, actor, reason),
        )

    # ---- cards ----
    def create(self, card: dict[str, Any]) -> dict[str, Any]:
        validate(card)
        with self._lock:
            self._conn.execute(
                "INSERT INTO board_cards(id, kind, title, show_id, episode, role, reviewer, "
                "lane, state, priority, payload, result, artifacts, notes, units, depends_on, "
                "attempts, max_attempts, claimed_by, claimed_at, created, updated) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    card["id"], card["kind"], card.get("title", ""),
                    card.get("show_id", ""), int(card.get("episode") or 0),
                    card["role"], card.get("reviewer", ""), card["lane"],
                    card["state"], int(card.get("priority") or 3),
                    _enc(card.get("payload")), _enc(card.get("result")),
                    _enc(card.get("artifacts")), _enc(card.get("notes")),
                    _enc(card.get("units")), _enc(card.get("depends_on")),
                    int(card.get("attempts") or 0), int(card.get("max_attempts") or 2),
                    card.get("claimed_by"), card.get("claimed_at"),
                    card.get("created") or now(), card.get("updated") or now(),
                ),
            )
            self._history(card["id"], None, card["state"], "board")
            self._conn.commit()
        created = self.get(card["id"]) or card
        self._emit({"type": "card", "card": created})
        return created

    def upsert(self, card: dict[str, Any]) -> dict[str, Any]:
        """Create the card if missing; leave an existing card untouched."""
        existing = self.get(card["id"])
        return existing if existing else self.create(card)

    def get(self, card_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM board_cards WHERE id=?", (card_id,)).fetchone()
        return self._row_to_card(row) if row else None

    def all(self, *, state: str | None = None, lane: str | None = None,
            role: str | None = None, show_id: str | None = None,
            episode: int | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        for key, value in (("state", state), ("lane", lane), ("role", role),
                           ("show_id", show_id), ("episode", episode)):
            if value is not None:
                clauses.append(f"{key}=?")
                args.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = ("SELECT * FROM board_cards" + where +
               " ORDER BY priority DESC, episode ASC, created ASC")
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row_to_card(r) for r in rows]

    def update(self, card_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"title", "role", "reviewer", "lane", "priority", "payload",
                   "result", "artifacts", "notes", "units", "depends_on",
                   "max_attempts", "claimed_by", "claimed_at", "attempts"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update fields: {', '.join(sorted(bad))}")
        with self._lock:
            card = self.get(card_id)
            if not card:
                raise KeyError(card_id)
            merged = dict(card)
            merged.update(fields)
            merged["updated"] = now()
            self._conn.execute(
                "UPDATE board_cards SET title=?, role=?, reviewer=?, lane=?, priority=?, "
                "payload=?, result=?, artifacts=?, notes=?, units=?, depends_on=?, "
                "max_attempts=?, claimed_by=?, claimed_at=?, attempts=?, updated=? "
                "WHERE id=?",
                (
                    merged.get("title", ""), merged.get("role", ""),
                    merged.get("reviewer", ""), merged.get("lane", "llm"),
                    int(merged.get("priority") or 3),
                    _enc(merged.get("payload")), _enc(merged.get("result")),
                    _enc(merged.get("artifacts")), _enc(merged.get("notes")),
                    _enc(merged.get("units")), _enc(merged.get("depends_on")),
                    int(merged.get("max_attempts") or 2), merged.get("claimed_by"),
                    merged.get("claimed_at"), int(merged.get("attempts") or 0),
                    merged["updated"], card_id,
                ),
            )
            self._conn.commit()
        updated = self.get(card_id) or merged
        self._emit({"type": "card", "card": updated})
        return updated

    def advance(self, card_id: str, to_state: str, actor: str,
                reason: str = "") -> dict[str, Any]:
        with self._lock:
            card = self.get(card_id)
            if not card:
                raise KeyError(card_id)
            if not can_move(card["state"], to_state):
                raise ValueError(
                    f"illegal transition {card['state']} -> {to_state} for {card_id}")
            self._conn.execute(
                "UPDATE board_cards SET state=?, claimed_by=NULL, claimed_at=NULL, "
                "updated=? WHERE id=?",
                (to_state, now(), card_id),
            )
            self._history(card_id, card["state"], to_state, actor, reason)
            self._conn.commit()
        moved = self.get(card_id) or card
        self._emit({"type": "card", "card": moved})
        return moved

    def requeue_review(self, card_id: str, actor: str = "human") -> dict[str, Any] | None:
        """Send a card straight to its reviewer without re-running its producer.

        Used after a per-scene rewrite: the plan already exists, so the next
        step is a fresh review, not a merge that could undo the rewrite.
        """
        card = self.get(card_id)
        if not card or card.get("state") in ("in_work", "reviewing"):
            return None
        with self._lock:
            self._conn.execute(
                "UPDATE board_cards SET state='agent_verified', claimed_by=NULL, "
                "claimed_at=NULL, attempts=0, notes='[]', updated=? WHERE id=?",
                (now(), card_id),
            )
            self._history(card_id, card["state"], "agent_verified", actor,
                          "re-review")
            self._conn.commit()
        moved = self.get(card_id) or card
        self._emit({"type": "card", "card": moved})
        return moved

    def claim(self, card_id: str, by: str, expected_state: str,
              claimed_state: str) -> bool:
        """Atomically move a card into a claimed state. False if already taken."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE board_cards SET state=?, claimed_by=?, claimed_at=?, updated=? "
                "WHERE id=? AND state=? AND claimed_by IS NULL",
                (claimed_state, by, time.time(), now(), card_id, expected_state),
            )
            self._conn.commit()
            if cur.rowcount != 1:
                return False
            self._history(card_id, expected_state, claimed_state, by, "claim")
            self._emit({"type": "card", "card": self.get(card_id) or {}})
            return True

    def release(self, card_id: str, by: str) -> None:
        """Un-claim a card and return it to the state its phase started from."""
        with self._lock:
            card = self.get(card_id)
            if not card or card.get("claimed_by") != by:
                return
            back = {"in_work": "planned", "reviewing": "agent_verified"}.get(
                card["state"], card["state"])
            self._conn.execute(
                "UPDATE board_cards SET state=?, claimed_by=NULL, claimed_at=NULL, "
                "updated=? WHERE id=?",
                (back, now(), card_id),
            )
            self._history(card_id, card["state"], back, by, "release")
            self._conn.commit()
            self._emit({"type": "card", "card": self.get(card_id) or {}})

    def attach(self, card_id: str, path: str, caption: str = "",
               kind: str = "", asset: dict[str, Any] | None = None) -> dict[str, Any]:
        card = self.get(card_id)
        if not card:
            raise KeyError(card_id)
        artifacts = list(card.get("artifacts") or [])
        item = {"path": path, "caption": caption, "kind": kind, "ts": now()}
        if asset:
            item["asset"] = asset
        artifacts.append(item)
        return self.update(card_id, artifacts=artifacts)

    def note(self, card_id: str, by: str, text: str) -> dict[str, Any]:
        card = self.get(card_id)
        if not card:
            raise KeyError(card_id)
        notes = list(card.get("notes") or [])
        notes.append({"by": by, "ts": now(), "text": text})
        return self.update(card_id, notes=notes)

    def check_units(self, card_id: str, unit_ids: list[str], by: str) -> dict[str, Any]:
        card = self.get(card_id)
        if not card:
            raise KeyError(card_id)
        wanted = set(unit_ids)
        units = []
        for unit in card.get("units") or []:
            unit = dict(unit)
            if unit.get("id") in wanted:
                unit["done"] = True
                unit["by"] = by
            units.append(unit)
        return self.update(card_id, units=units)

    def delete_card(self, card_id: str) -> bool:
        with self._lock:
            card = self.get(card_id)
            if not card:
                return False
            runs = self._conn.execute(
                "SELECT id FROM board_runs WHERE card_id=? AND state='running'",
                (card_id,)).fetchall()
            self._conn.execute("DELETE FROM board_cards WHERE id=?", (card_id,))
            self._conn.execute("DELETE FROM board_history WHERE card_id=?", (card_id,))
            self._conn.execute(
                "UPDATE board_runs SET state='canceled', ended=?, error=? "
                "WHERE card_id=? AND state='running'",
                (time.time(), "card deleted", card_id))
            self._conn.commit()
        self._emit({"type": "deleted", "id": card_id})
        for row in runs:
            self._emit({"type": "run", "run": self.run(row["id"]) or {}})
        return True

    def history(self, card_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM board_history WHERE card_id=? ORDER BY id",
                (card_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- migration helpers ----
    def export_card(self, card_id: str) -> tuple[dict[str, Any] | None,
                                                 list[dict[str, Any]],
                                                 list[dict[str, Any]]]:
        """Return a card with its history and runs, for moving between stores."""
        return self.get(card_id), self.history(card_id), self.runs(card_id=card_id)

    def import_card(self, card: dict[str, Any],
                    history: list[dict[str, Any]] | None = None,
                    runs: list[dict[str, Any]] | None = None) -> None:
        """Insert a card plus history/runs, leaving an existing card untouched."""
        with self._lock:
            if self.get(card["id"]) is None:
                self.create(card)
            for row in history or []:
                self._conn.execute(
                    "INSERT INTO board_history(card_id, ts, from_state, to_state, "
                    "actor, reason) VALUES (?,?,?,?,?,?)",
                    (row.get("card_id") or card["id"], row.get("ts") or now(),
                     row.get("from_state"), row.get("to_state"),
                     row.get("actor"), row.get("reason")))
            for run in runs or []:
                self._conn.execute(
                    "INSERT OR IGNORE INTO board_runs(id, card_id, role, phase, "
                    "lane, model, state, started, ended, error, result) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (run["id"], run.get("card_id") or card["id"],
                     run.get("role") or "", run.get("phase") or "produce",
                     run.get("lane") or card.get("lane") or "llm",
                     run.get("model") or "", run.get("state") or "done",
                     run.get("started") or time.time(), run.get("ended"),
                     run.get("error"), _enc(run.get("result"))))
            self._conn.commit()

    def remove_card_rows(self, card_id: str) -> None:
        """Delete a card and its rows without the transition checks of delete_card."""
        with self._lock:
            self._conn.execute("DELETE FROM board_cards WHERE id=?", (card_id,))
            self._conn.execute("DELETE FROM board_history WHERE card_id=?", (card_id,))
            self._conn.execute("DELETE FROM board_runs WHERE card_id=?", (card_id,))
            self._conn.commit()

    # ---- runs ----
    def start_run(self, run_id: str, card_id: str, role: str, phase: str,
                  lane: str, model: str) -> dict[str, Any]:
        with self._lock:
            self._conn.execute(
                "INSERT INTO board_runs(id, card_id, role, phase, lane, model, "
                "state, started) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, card_id, role, phase, lane, model, "running", time.time()),
            )
            self._conn.commit()
        run = self.run(run_id) or {}
        self._emit({"type": "run", "run": run})
        return run

    def finish_run(self, run_id: str, state: str, error: str = "",
                   result: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE board_runs SET state=?, ended=?, error=?, result=? WHERE id=?",
                (state, time.time(), error or None, _enc(result or {}), run_id),
            )
            self._conn.commit()
        self._emit({"type": "run", "run": self.run(run_id) or {}})

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM board_runs WHERE id=?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def set_run_final(self, run_id: str, final: dict[str, Any]) -> bool:
        """Store an agent-reported verdict without ending the run.

        The opencode plugin calls this from ``studio_finish``; the runner keeps
        the process alive until it observes the verdict, then stops it.
        """
        with self._lock:
            run = self.run(run_id)
            if not run:
                return False
            result = dict(run.get("result") or {})
            result["final"] = final
            self._conn.execute(
                "UPDATE board_runs SET result=? WHERE id=?",
                (_enc(result), run_id),
            )
            self._conn.commit()
        self._emit({"type": "run", "run": self.run(run_id) or {}})
        return True

    def runs(self, *, state: str | None = None, lane: str | None = None,
             card_id: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        for key, value in (("state", state), ("lane", lane), ("card_id", card_id)):
            if value is not None:
                clauses.append(f"{key}=?")
                args.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM board_runs" + where + " ORDER BY started DESC", args
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def lane_load(self, lane: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM board_runs WHERE state='running' AND lane=?",
                (lane,)).fetchone()
        return int(row[0]) if row else 0

    def recover_missing(self, active_run_ids: set[str],
                        actor: str = "daemon") -> list[str]:
        """Fail running runs whose worker thread no longer exists.

        A board restart kills in-process workers but leaves their runs "running";
        without this the card stays in_work forever.
        """
        recovered: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM board_runs WHERE state='running'").fetchall()
            for row in rows:
                run = dict(row)
                if run["id"] in active_run_ids:
                    continue
                self._conn.execute(
                    "UPDATE board_runs SET state='failed', ended=?, error=? WHERE id=?",
                    (time.time(), "worker lost", run["id"]),
                )
                card = self.get(run["card_id"])
                if card and card.get("claimed_by") == run["id"]:
                    self._conn.execute(
                        "UPDATE board_cards SET claimed_by=NULL, claimed_at=NULL, "
                        "state='blocked', updated=? WHERE id=?",
                        (now(), card["id"]),
                    )
                    self._history(card["id"], card["state"], "blocked", actor,
                                  "worker lost")
                recovered.append(run["id"])
            self._conn.commit()
        return recovered

    def recover_stale(self, timeout_s: float, actor: str = "daemon") -> list[str]:
        """Fail runs that stopped heartbeating and release their cards."""
        cutoff = time.time() - float(timeout_s)
        recovered: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM board_runs WHERE state='running' AND started < ?",
                (cutoff,)).fetchall()
            for row in rows:
                run = dict(row)
                self._conn.execute(
                    "UPDATE board_runs SET state='failed', ended=?, error=? WHERE id=?",
                    (time.time(), f"stale after {int(timeout_s)}s", run["id"]),
                )
                card = self.get(run["card_id"])
                if card and card.get("claimed_by") == run["id"]:
                    self._conn.execute(
                        "UPDATE board_cards SET claimed_by=NULL, claimed_at=NULL, "
                        "state='blocked', updated=? WHERE id=?",
                        (now(), card["id"]),
                    )
                    self._history(card["id"], card["state"], "blocked", actor,
                                  "stale run")
                recovered.append(run["id"])
            self._conn.commit()
        return recovered


_SHOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_OPEN_SETS: list["BoardSet"] = []
_OPEN_SETS_LOCK = threading.Lock()


def valid_show_id(show_id: str) -> bool:
    return bool(show_id) and show_id not in (".", "..") and bool(
        _SHOW_ID_RE.match(show_id))


def close_show_stores(show_id: str) -> None:
    """Close any per-show board DB so the show folder can be moved/deleted."""
    with _OPEN_SETS_LOCK:
        sets = list(_OPEN_SETS)
    for board_set in sets:
        try:
            board_set.close_show(show_id)
        except Exception:
            log.exception("could not close board store for %s", show_id)


class BoardSet:
    """One board DB per show, with a single interface for the daemon and API.

    Card state lives at ``shows/<show_id>/board/board.db`` so a show folder is
    self-contained. The legacy ``studio.db`` board tables are still consulted
    for cards whose show folder is gone, and migrated on first open.
    """

    def __init__(self, cfg, *, legacy_path: Path | str | None = None,
                 migrate: bool = True, adopt: bool = False):
        self.cfg = cfg
        self._adopt = bool(adopt)
        self._lock = threading.RLock()
        self._stores: dict[str, BoardStore] = {}
        self._legacy = BoardStore(legacy_path or cfg.db_path)
        self._subs: dict[int, tuple[queue.Queue, list[tuple[BoardStore, queue.Queue]],
                                    threading.Event, list[threading.Thread]]] = {}
        with _OPEN_SETS_LOCK:
            _OPEN_SETS.append(self)
        if migrate:
            self.migrate_legacy()
        self._sync()

    # ---- stores ----
    def _board_path(self, show_id: str) -> Path:
        return Path(self.cfg.show_path(show_id)) / "board" / "board.db"

    def _store(self, show_id: str, *, create: bool = False) -> BoardStore | None:
        if not valid_show_id(show_id):
            raise ValueError(f"invalid show id {show_id!r}")
        with self._lock:
            store = self._stores.get(show_id)
            if store is not None:
                return store
            path = self._board_path(show_id)
            if not create and not path.exists():
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            store = BoardStore(path)
            self._stores[show_id] = store
            return store

    def _sync(self) -> None:
        shows_dir = Path(self.cfg.shows_dir)
        if not shows_dir.exists():
            return
        for path in shows_dir.glob("*/board/board.db"):
            show_id = path.parent.parent.name
            if not valid_show_id(show_id):
                continue
            with self._lock:
                if show_id not in self._stores:
                    try:
                        self._stores[show_id] = BoardStore(path)
                    except Exception:
                        log.exception("cannot open board DB %s", path)
        if self._adopt:
            self._adopt_discovered(shows_dir)

    def _adopt_discovered(self, shows_dir: Path) -> None:
        """Give any newly discovered show folder a board built from its files."""
        from .adopt import adopt_show
        from ..show import Show

        for path in shows_dir.iterdir():
            if not path.is_dir() or not valid_show_id(path.name):
                continue
            if not ((path / "bible.yaml").exists()
                    or (path / "bootstrap.json").exists()):
                continue
            try:
                store = self._store(path.name, create=True)
                if store is not None:
                    adopt_show(Show(path.name, root=path), store)
            except Exception:
                log.exception("cannot adopt show %s", path.name)

    def _all_stores(self) -> list[BoardStore]:
        with self._lock:
            return list(self._stores.values()) + [self._legacy]

    def _resolve(self, card_id: str, show_id: str | None = None
                 ) -> tuple[BoardStore | None, str]:
        if show_id:
            return self._store(show_id), show_id
        self._sync()
        hits = [(sid, store) for sid, store in self._stores.items()
                if store.get(card_id)]
        if len(hits) > 1:
            raise ValueError(
                f"ambiguous card id {card_id!r} in shows "
                f"{sorted(sid for sid, _ in hits)}")
        if hits:
            sid, store = hits[0]
            return store, sid
        if self._legacy.get(card_id):
            return self._legacy, ""
        return None, ""

    def _resolve_run(self, run_id: str) -> BoardStore:
        self._sync()
        for store in self._all_stores():
            if store.run(run_id):
                return store
        return self._legacy

    def shows(self) -> list[str]:
        self._sync()
        with self._lock:
            return sorted(self._stores)

    def close_show(self, show_id: str) -> None:
        with self._lock:
            store = self._stores.pop(show_id, None)
        if store is not None:
            try:
                store.close()
            except Exception:
                log.exception("error closing board store for %s", show_id)

    def close(self) -> None:
        with _OPEN_SETS_LOCK:
            if self in _OPEN_SETS:
                _OPEN_SETS.remove(self)
        with self._lock:
            stores = list(self._stores.values())
            self._stores.clear()
            subs = list(self._subs.values())
            self._subs.clear()
        for _, _, stop, threads in subs:
            stop.set()
        for store in stores:
            try:
                store.close()
            except Exception:
                pass
        try:
            self._legacy.close()
        except Exception:
            pass

    def migrate_legacy(self) -> None:
        """Move cards from the old central board tables into show folders."""
        try:
            cards = self._legacy.all()
        except Exception:
            return
        for card in cards:
            show_id = str(card.get("show_id") or "")
            if not valid_show_id(show_id):
                continue
            if not Path(self.cfg.show_path(show_id)).exists():
                continue
            try:
                store = self._store(show_id, create=True)
                if store is None:
                    continue
                if store.get(card["id"]) is None:
                    _, history, runs = self._legacy.export_card(card["id"])
                    store.import_card(card, history, runs)
                self._legacy.remove_card_rows(card["id"])
            except Exception:
                log.exception("cannot migrate board card %s", card["id"])

    # ---- cards ----
    def create(self, card: dict[str, Any]) -> dict[str, Any]:
        show_id = str(card.get("show_id") or "")
        store = self._store(show_id, create=True) if show_id else self._legacy
        return store.create(card)

    def upsert(self, card: dict[str, Any]) -> dict[str, Any]:
        show_id = str(card.get("show_id") or "")
        store = self._store(show_id, create=True) if show_id else self._legacy
        return store.upsert(card)

    def get(self, card_id: str, show_id: str | None = None) -> dict[str, Any] | None:
        if show_id:
            store = self._store(show_id)
            return store.get(card_id) if store else None
        store, _ = self._resolve(card_id)
        return store.get(card_id) if store else None

    def all(self, *, state: str | None = None, lane: str | None = None,
            role: str | None = None, show_id: str | None = None,
            episode: int | None = None) -> list[dict[str, Any]]:
        if show_id is not None:
            store = self._store(show_id)
            if store is None:
                return []
            return store.all(state=state, lane=lane, role=role,
                             episode=episode)
        self._sync()
        out: list[dict[str, Any]] = []
        for store in self._all_stores():
            out.extend(store.all(state=state, lane=lane, role=role,
                                 episode=episode))
        out.sort(key=lambda c: (-int(c.get("priority") or 0),
                                int(c.get("episode") or 0),
                                c.get("created") or ""))
        return out

    def update(self, card_id: str, show_id: str | None = None,
               **fields: Any) -> dict[str, Any]:
        store, _ = self._resolve(card_id, show_id)
        if store is None:
            raise KeyError(card_id)
        return store.update(card_id, **fields)

    def advance(self, card_id: str, to_state: str, actor: str,
                reason: str = "", show_id: str | None = None) -> dict[str, Any]:
        store, _ = self._resolve(card_id, show_id)
        if store is None:
            raise KeyError(card_id)
        return store.advance(card_id, to_state, actor, reason)

    def requeue_review(self, card_id: str, actor: str = "human",
                       show_id: str | None = None) -> dict[str, Any] | None:
        store, _ = self._resolve(card_id, show_id)
        if store is None:
            return None
        return store.requeue_review(card_id, actor)

    def claim(self, card_id: str, by: str, expected_state: str,
              claimed_state: str, show_id: str | None = None) -> bool:
        store, _ = self._resolve(card_id, show_id)
        if store is None:
            return False
        return store.claim(card_id, by, expected_state, claimed_state)

    def release(self, card_id: str, by: str, show_id: str | None = None) -> None:
        store, _ = self._resolve(card_id, show_id)
        if store is not None:
            store.release(card_id, by)

    def attach(self, card_id: str, path: str, caption: str = "",
               kind: str = "", asset: dict[str, Any] | None = None,
               show_id: str | None = None) -> dict[str, Any]:
        store, _ = self._resolve(card_id, show_id)
        if store is None:
            raise KeyError(card_id)
        return store.attach(card_id, path, caption, kind, asset)

    def note(self, card_id: str, by: str, text: str,
             show_id: str | None = None) -> dict[str, Any]:
        store, _ = self._resolve(card_id, show_id)
        if store is None:
            raise KeyError(card_id)
        return store.note(card_id, by, text)

    def check_units(self, card_id: str, unit_ids: list[str], by: str,
                    show_id: str | None = None) -> dict[str, Any]:
        store, _ = self._resolve(card_id, show_id)
        if store is None:
            raise KeyError(card_id)
        return store.check_units(card_id, unit_ids, by)

    def delete_card(self, card_id: str, show_id: str | None = None) -> bool:
        store, _ = self._resolve(card_id, show_id)
        if store is None:
            return False
        return store.delete_card(card_id)

    def history(self, card_id: str, show_id: str | None = None) -> list[dict[str, Any]]:
        store, _ = self._resolve(card_id, show_id)
        if store is None:
            return []
        return store.history(card_id)

    # ---- runs ----
    def start_run(self, run_id: str, card_id: str, role: str, phase: str,
                  lane: str, model: str, show_id: str | None = None
                  ) -> dict[str, Any]:
        store, _ = self._resolve(card_id, show_id)
        if store is None:
            store = self._legacy
        return store.start_run(run_id, card_id, role, phase, lane, model)

    def finish_run(self, run_id: str, state: str, error: str = "",
                   result: dict[str, Any] | None = None) -> None:
        self._resolve_run(run_id).finish_run(run_id, state, error, result)

    def set_run_final(self, run_id: str, final: dict[str, Any]) -> bool:
        return self._resolve_run(run_id).set_run_final(run_id, final)

    def run(self, run_id: str) -> dict[str, Any] | None:
        self._sync()
        for store in self._all_stores():
            run = store.run(run_id)
            if run:
                return run
        return None

    def runs(self, *, state: str | None = None, lane: str | None = None,
             card_id: str | None = None) -> list[dict[str, Any]]:
        self._sync()
        out: list[dict[str, Any]] = []
        for store in self._all_stores():
            out.extend(store.runs(state=state, lane=lane, card_id=card_id))
        out.sort(key=lambda r: r.get("started") or 0, reverse=True)
        return out

    def lane_load(self, lane: str) -> int:
        self._sync()
        return sum(store.lane_load(lane) for store in self._all_stores())

    def recover_missing(self, active_run_ids: set[str],
                        actor: str = "daemon") -> list[str]:
        self._sync()
        recovered: list[str] = []
        for store in self._all_stores():
            recovered.extend(store.recover_missing(active_run_ids, actor))
        return recovered

    def recover_stale(self, timeout_s: float, actor: str = "daemon") -> list[str]:
        self._sync()
        recovered: list[str] = []
        for store in self._all_stores():
            recovered.extend(store.recover_stale(timeout_s, actor))
        return recovered

    # ---- live updates ----
    def subscribe(self) -> queue.Queue:
        self._sync()
        out: queue.Queue = queue.Queue(maxsize=200)
        children = [(store, store.subscribe()) for store in self._all_stores()]
        stop = threading.Event()
        threads: list[threading.Thread] = []

        def pump(store: BoardStore, q: queue.Queue) -> None:
            while not stop.is_set():
                try:
                    event = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    out.put_nowait(event)
                except queue.Full:
                    pass

        for store, q in children:
            thread = threading.Thread(target=pump, args=(store, q), daemon=True)
            thread.start()
            threads.append(thread)
        with self._lock:
            self._subs[id(out)] = (out, children, stop, threads)
        return out

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            item = self._subs.pop(id(q), None)
        if not item:
            return
        _, children, stop, _ = item
        stop.set()
        for store, child in children:
            try:
                store.unsubscribe(child)
            except Exception:
                pass
