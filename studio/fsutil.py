"""Small filesystem helpers shared across the studio.

The pipeline writes and deletes episode artifacts concurrently — the dashboard
scans the runs directory while the reconciler/delete mutate it. Any glob-then-stat
is therefore a race: a file can vanish between the directory listing and the
``stat()`` call, raising ``FileNotFoundError`` (WinError 2 on Windows). These
helpers tolerate that: a path that disappears mid-scan is simply dropped.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


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
