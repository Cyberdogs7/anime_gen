"""Single owner of episode state files.

ONE serializer and ONE set of path conventions for an episode's persisted state
(``plan.json``, ``scene_details.json`` and the ``script.rN.json`` revisions), so
every module writes them the same way instead of copy-pasting
``write_text(json.dumps(obj, indent=2, ensure_ascii=False))``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import fsutil
from .show import Show


def episode_dir(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}"


def read_json(path: Path, default: Any = None) -> Any:
    return fsutil.read_json(path, default)


def write_json(path: Path, data: Any) -> None:
    """The canonical persisted-state serializer for episode files."""
    fsutil.write_json(path, data)


# ---- path conventions ----
def plan_path(show: Show, episode: int) -> Path:
    return episode_dir(show, episode) / "plan.json"


def details_path(show: Show, episode: int) -> Path:
    return episode_dir(show, episode) / "scene_details.json"


def script_path(show: Show, episode: int, revision: int) -> Path:
    return episode_dir(show, episode) / f"script.r{revision}.json"


# ---- writers ----
def save_plan(show: Show, episode: int, plan: dict[str, Any]) -> None:
    write_json(plan_path(show, episode), plan)


def save_details(show: Show, episode: int, data: dict[str, Any]) -> None:
    write_json(details_path(show, episode), data)


def save_script(path: Path, script: dict[str, Any]) -> None:
    write_json(path, script)
