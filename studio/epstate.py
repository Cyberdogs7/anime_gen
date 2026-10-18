"""Single owner of episode state files.

ONE serializer and ONE set of path conventions for an episode's persisted state
(``plan.json``, ``scene_details.json`` and the ``script.rN.json`` revisions), so
every module writes them the same way instead of copy-pasting
``write_text(json.dumps(obj, indent=2, ensure_ascii=False))``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .show import Show


def episode_dir(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}"


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def write_json(path: Path, data: Any) -> None:
    """The canonical persisted-state serializer for episode files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


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
