"""Stage 2 + Stage 2a - script generation and the writers'-room revision loop.

Draft -> review (slop/continuity/fan_service, separate notes) -> revise ->
re-review, up to max_revisions. See DESIGN.md §9 Stage 2/2a.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import prompts
from .bootstrap import ACTIVITY
from .compile.durations import snap_duration
from .config import get_config
from .review import all_pass, run_reviewers
from .show import Show


def _auto_synopsis(bible: dict[str, Any], episode: int, continuity: dict[str, Any]) -> str:
    """Deterministic fallback when Development (LLM) is unavailable."""
    plotlines = bible.get("plotlines", []) or continuity.get("plotlines", []) or []
    active = [p for p in plotlines if str(p.get("status", "active")).lower() != "resolved"]
    if not active:
        return f"Episode {episode}: an ordinary day, but everything changes."
    picks = sorted(active, key=lambda p: p.get("last_seen_episode", 0))[:3]
    return f"Episode {episode}: " + " | ".join(
        f"{p.get('name')}: {p.get('summary', '')}" for p in picks)


def _normalize(script: dict[str, Any], cast_names: list[str]) -> dict[str, Any]:
    """Snap durations to the H3 grid, default fields, drop unknown speakers."""
    for scene in script.get("scenes", []):
        for i, shot in enumerate(scene.get("shots", [])):
            shot.setdefault("id", f"{scene.get('id', 's')}_sh{i + 1:02d}")
            shot.setdefault("type", "ref2va")
            shot.setdefault("importance", "standard")
            shot.setdefault("on_camera", True)
            shot["duration_s"] = snap_duration(float(shot.get("duration_s", 10.125)))[2]
            kept = [d for d in shot.get("dialogue", [])
                    if d.get("char") in cast_names]
            if kept != shot.get("dialogue", []):
                shot["dialogue"] = kept
            shot.setdefault("soundscape", "")
            shot.setdefault("music", "")
            shot.setdefault("references", {})
            if "characters" in shot["references"]:
                shot["references"]["characters"] = [
                    c for c in shot["references"]["characters"] if c in cast_names]
    # Guarantee the cast pre-section: every exact name that appears in shots.
    if not isinstance(script.get("cast"), list):
        seen: list[str] = []
        for scene in script.get("scenes", []):
            for shot in scene.get("shots", []):
                for c in (shot.get("references") or {}).get("characters", []) or []:
                    if c and c not in seen:
                        seen.append(c)
                for d in shot.get("dialogue", []) or []:
                    if d.get("char") and d["char"] not in seen:
                        seen.append(d["char"])
        script["cast"] = seen
    return script


class WritersRoom:
    def __init__(self, show: Show, cfg=None, llm=None):
        self.cfg = cfg or get_config()
        self.show = show
        self.llm = llm
        self._model = None

    def _role_model(self, role: str) -> str:
        roles = self.cfg.get("llm", "roles", {})
        return roles.get(role) or self.cfg.get("llm", "model")

    def _report(self, detail: str) -> None:
        ACTIVITY[self.show.show_id] = {"detail": detail, "ts": time.time()}

    def _runs_dir(self, episode: int) -> Path:
        d = self.show.dir / "runs" / f"EP{episode:02d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_script(self, episode: int, round_no: int, script: dict[str, Any]) -> Path:
        p = self._runs_dir(episode) / f"script.r{round_no}.json"
        p.write_text(json.dumps(script, indent=2), encoding="utf-8")
        return p

    def _develop(self, episode: int, bible: dict[str, Any],
                 continuity: dict[str, Any]) -> str:
        """Development stage: pick plotlines for this episode, return a synopsis."""
        from .development import (_fallback_synopsis, _overall_state,
                                  _plotline_state, develop_episode)
        try:
            return develop_episode(self.show, episode, llm=self.llm, cfg=self.cfg)
        except Exception:
            return _fallback_synopsis(episode, _overall_state(self.show, bible),
                                      _plotline_state(self.show, bible))

    def run(self, episode: int, synopsis: str = "",
            max_revisions: int | None = None) -> dict[str, Any]:
        """Generate + review + revise. Returns {'script', 'reviews', 'rounds', 'passed'}."""
        max_revisions = max_revisions or int(self.cfg.get("reviewers", "max_revisions", 2))
        bible = self.show.read_bible()
        continuity = self.show.read_continuity()
        characters = [self.show.read_character(cid) for cid in self.show.list_characters()]
        cast_names = [c.get("name") for c in characters]
        self._report(f"Episode {episode}: Development — picking plotlines & synopsis…")
        synopsis = synopsis or self._develop(episode, bible, continuity)
        profile = self.cfg["show_profile"]

        system = prompts.showrunner_system(
            profile, bible.get("content_policy", "mature"), profile.get("baseline", "ranma-1-2"))

        self._report(f"Episode {episode}: writing script (round 1)…")
        script = self._ask_script(system, bible, synopsis, continuity, characters)
        script = _normalize(script, cast_names)
        self._write_script(episode, 1, script)

        self._report(f"Episode {episode}: writers' room review (round 1)…")
        reviews = run_reviewers(script, show=self.show, cfg=self.cfg, llm=self.llm,
                                episode=episode, round_no=1,
                                notes_dir=self._runs_dir(episode) / "reviews")
        rounds = 1
        passed = all_pass(reviews)
        while not passed and rounds < max_revisions:
            rounds += 1
            self._report(f"Episode {episode}: revising script (round {rounds})…")
            script = self._ask_revision(system, script, reviews)
            script = _normalize(script, cast_names)
            self._write_script(episode, rounds, script)
            self._report(f"Episode {episode}: writers' room re-review (round {rounds})…")
            reviews = run_reviewers(script, show=self.show, cfg=self.cfg, llm=self.llm,
                                    episode=episode, round_no=rounds,
                                    notes_dir=self._runs_dir(episode) / "reviews")
            passed = all_pass(reviews)

        return {"script": script, "reviews": reviews, "rounds": rounds, "passed": passed,
                "episode": episode}

    def _ask_script(self, system: str, bible, synopsis, continuity, characters) -> dict[str, Any]:
        return self.llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompts.script_prompt(bible, synopsis, continuity,
                                                               characters)}],
            model=self._role_model("script"), temperature=0.8, max_tokens=16384)

    def _ask_revision(self, system: str, script, reviews) -> dict[str, Any]:
        return self.llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompts.revision_prompt(script, reviews)}],
            model=self._role_model("script"), temperature=0.6, max_tokens=16384)
