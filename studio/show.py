"""Show model: read/write the per-show canon under shows/<show_id>/ (DESIGN.md §8).

Everything here is AI-generated + approved through the bootstrap chain; these
loaders are how stages read the canon. Paths follow DESIGN.md §8 layout.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from .config import get_config


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (text or "").strip().lower()).strip("-")


class Show:
    def __init__(self, show_id: str, root: Path | None = None):
        cfg = get_config()
        self.show_id = show_id
        self.dir = Path(root) if root else cfg.show_path(show_id)

    # ---- paths ----
    @property
    def characters_dir(self) -> Path:
        return self.dir / "characters"

    @property
    def voices_dir(self) -> Path:
        return self.dir / "voices"

    @property
    def scenes_dir(self) -> Path:
        return self.dir / "scenes"

    @property
    def continuity_path(self) -> Path:
        return self.dir / "continuity" / "state.json"

    @property
    def bootstrap_path(self) -> Path:
        return self.dir / "bootstrap.json"

    # ---- bible ----
    @property
    def bible_path(self) -> Path:
        return self.dir / "bible.yaml"

    def read_bible(self) -> dict[str, Any]:
        if not self.bible_path.exists():
            return {}
        data = yaml.safe_load(self.bible_path.read_text(encoding="utf-8")) or {}
        return dict(data)

    def write_bible(self, data: dict[str, Any]) -> Path:
        self.bible_path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        return self.bible_path

    # ---- concept ----
    @property
    def concept_path(self) -> Path:
        return self.dir / "concept.json"

    def read_concept(self) -> dict[str, Any]:
        if not self.concept_path.exists():
            return {}
        return json.loads(self.concept_path.read_text(encoding="utf-8"))

    def write_concept(self, data: dict[str, Any]) -> Path:
        self.concept_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return self.concept_path

    # ---- characters ----
    def list_characters(self) -> list[str]:
        if not self.characters_dir.exists():
            return []
        return sorted(p.stem for p in self.characters_dir.glob("*.yaml"))

    def read_character(self, char_id: str) -> dict[str, Any]:
        p = self.characters_dir / f"{char_id}.yaml"
        if not p.exists():
            return {}
        return dict(yaml.safe_load(p.read_text(encoding="utf-8")) or {})

    def write_character(self, data: dict[str, Any]) -> Path:
        char_id = data.get("id") or _slug(data.get("name", ""))
        p = self.characters_dir / f"{char_id}.yaml"
        p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        (self.characters_dir / char_id / "refs").mkdir(parents=True, exist_ok=True)
        return p

    def character_refs_dir(self, char_id: str) -> Path:
        return self.characters_dir / char_id / "refs"

    # ---- voices ----
    def read_voice(self, voice_id: str) -> dict[str, Any]:
        p = self.voices_dir / f"{voice_id}.yaml"
        if not p.exists():
            return {}
        return dict(yaml.safe_load(p.read_text(encoding="utf-8")) or {})

    def write_voice(self, data: dict[str, Any]) -> Path:
        p = self.voices_dir / f"{data.get('id', 'voice')}.yaml"
        p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return p

    # ---- scenes ----
    def list_scenes(self) -> list[str]:
        if not self.scenes_dir.exists():
            return []
        return sorted(p.stem for p in self.scenes_dir.glob("*.yaml"))

    def write_scene(self, data: dict[str, Any]) -> Path:
        loc = data.get("id") or _slug(data.get("name", "scene"))
        p = self.scenes_dir / f"{loc}.yaml"
        p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return p

    # ---- bootstrap state ----
    def bootstrap_state(self) -> dict[str, Any]:
        if not self.bootstrap_path.exists():
            return {"concept": {}, "bible": {}, "characters": [], "scenes": {}, "complete": False}
        return json.loads(self.bootstrap_path.read_text(encoding="utf-8"))

    def set_bootstrap_state(self, state: dict[str, Any]) -> None:
        self.bootstrap_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # ---- continuity ----
    def read_continuity(self) -> dict[str, Any]:
        if not self.continuity_path.exists():
            return {"episode": 0, "world": {}, "characters": {}, "plotlines": [],
                    "unresolved_threads": [], "continuity_rules": []}
        return json.loads(self.continuity_path.read_text(encoding="utf-8"))

    def write_continuity(self, state: dict[str, Any]) -> None:
        self.continuity_path.parent.mkdir(parents=True, exist_ok=True)
        self.continuity_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def open_show(show_id: str) -> Show:
    return Show(show_id)
