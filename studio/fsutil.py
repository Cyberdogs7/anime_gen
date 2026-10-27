"""Small filesystem helpers shared across the studio.

Workers write and delete artifacts concurrently while readers scan the runs
directory. Any glob-then-stat is therefore a race: a file can vanish between the
directory listing and the ``stat()`` call, raising ``FileNotFoundError``
(WinError 2 on Windows). These helpers tolerate that: a path that disappears
mid-scan is simply dropped.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, tolerating a missing, empty or partially-written file.

    State files are written while the dashboard polls them; a reader can catch a
    zero-byte file mid-write. That is not a real error and must not crash a gate
    or a self-heal check.
    """
    fallback = {} if default is None else default
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text) if text.strip() else fallback
    except (OSError, ValueError):
        return fallback


def write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    """Atomically write JSON: temp file + os.replace.

    ``Path.write_text`` truncates the target first, so a concurrent reader can
    see an empty file. Replacing the whole file means readers see either the old
    complete document or the new complete document.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                    dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=indent, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows can briefly lock the target while a reader has it open.
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def rmtree_hard(path: Path | str, attempts: int = 6) -> None:
    """Remove a directory tree even when files are briefly locked (Windows).

    ``shutil.rmtree`` aborts on the first locked file. Retry with backoff,
    clearing read-only bits in between, then do a best-effort final pass.
    """
    path = Path(path)

    def _force(p: str) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
        except Exception:
            pass

    for i in range(attempts):
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
            return
        except Exception:
            for root, dirs, files in os.walk(path, onerror=lambda e: None):
                for name in list(files) + list(dirs):
                    _force(os.path.join(root, name))
            time.sleep(0.3 * (i + 1))
    shutil.rmtree(path, ignore_errors=True)


def existing_by_mtime(paths: Iterable[Path]) -> list[Path]:
    """Return the existing paths sorted by mtime, oldest first.

    Files that raise ``OSError`` on ``stat`` (deleted between the glob and the
    stat) are skipped, so a concurrent delete can't crash a directory scan.
    Order matches the previous ``sorted(glob, key=lambda p: p.stat().st_mtime)``
    so callers keep their ``[0]`` / ``[-1]`` (oldest / newest) semantics.
    """
    out: list[tuple[float, Path]] = []
    for p in paths:
        try:
            out.append((p.stat().st_mtime, p))
        except OSError:
            continue
    out.sort(key=lambda t: t[0])
    return [p for _, p in out]
