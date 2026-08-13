"""Stage 2 + Stage 2a - script generation and the writers'-room revision loop.

Draft -> review (slop/continuity/fan_service, separate notes) -> revise ->
re-review, up to max_revisions. See DESIGN.md §9 Stage 2/2a.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import prompts
from .bootstrap import ACTIVITY
from .compile.durations import snap_duration
from .config import get_config
from .review import all_pass, run_reviewers
from .show import Show


def _has_spoken_text(line: str) -> bool:
    """True when a dialogue line contains actual speech (not just a stage direction).

    A padded line like "(Grunting)" or "(Internal/Unvoiced)" carries no spoken
    words and must not count toward the dialogue floor — it would reach TTS
    otherwise. A leading delivery note like "(whispering) Stay close." is fine.
    """
    stripped = re.sub(r"\([^)]*\)", "", line or "")
    return bool(re.search(r"\w", stripped))


def _auto_synopsis(bible: dict[str, Any], episode: int, continuity: dict[str, Any]) -> str:
    """Deterministic fallback when Development (LLM) is unavailable."""
    plotlines = bible.get("plotlines", []) or continuity.get("plotlines", []) or []
    active = [p for p in plotlines if str(p.get("status", "active")).lower() != "resolved"]
    if not active:
        return f"Episode {episode}: an ordinary day, but everything changes."
    picks = sorted(active, key=lambda p: p.get("last_seen_episode", 0))[:3]
    return f"Episode {episode}: " + " | ".join(
        f"{p.get('name')}: {p.get('summary', '')}" for p in picks)


def _runtime_review(script: dict[str, Any], target: int) -> dict[str, Any] | None:
    """Fail the script if its shot durations total well under the runtime target,
    or if dialogue is severely lacking for the planned length."""
    total = sum(s.get("duration_s", 0) for sc in script.get("scenes", [])
                for s in sc.get("shots", []))
    lines = sum(1 for sc in script.get("scenes", [])
                for s in sc.get("shots", [])
                for d in (s.get("dialogue", []) or [])
                if _has_spoken_text(d.get("line") or ""))
    min_lines = max(1, target // 6)   # ~67% spoken (~220 lines for a 22-min episode)
    if total >= target * 0.8 and lines >= min_lines:
        return None
    notes: list[str] = []
    if total < target * 0.8:
        notes.append(f"The script totals ~{int(total)}s but the target is {target}s "
                      f"(~{target // 60} min). Expand the episode: add roughly "
                      f"{int((target - total) / 10) + 1} more shots across additional scenes "
                      f"and beats so it fills the runtime.")
    if lines < min_lines:
        notes.append(f"The script has only {lines} dialogue lines but needs at least "
                      f"{min_lines} for a {target // 60}-minute episode. Characters must "
                      f"speak in most shots — dialogue drives the story, not silent action. "
                      f"Add lines to shots that have characters on screen.")
    return {"pass": False, "score": 0,
            "summary": f"structural failure (runtime {int(total)}s, {lines} lines)",
            "notes": notes}


def _normalize(script: dict[str, Any], cast_names: list[str]) -> dict[str, Any]:
    """Snap durations to the H3 grid, default fields, drop unknown speakers.

    ``cast_names`` are the characters with approved sheets. A speaker is kept if
    they are either in ``cast_names`` OR listed in the script's own ``cast``
    pre-section (a newly-introduced supporting character that has no sheet yet
    but is canon for this episode — the ref pass will create it).
    """
    script_cast = set(script.get("cast", []) or [])
    valid = set(cast_names) | script_cast

    for scene in script.get("scenes", []):
        for i, shot in enumerate(scene.get("shots", [])):
            shot.setdefault("id", f"{scene.get('id', 's')}_sh{i + 1:02d}")
            shot.setdefault("type", "ref2va")
            shot.setdefault("importance", "standard")
            shot.setdefault("on_camera", True)
            shot["duration_s"] = snap_duration(float(shot.get("duration_s", 10.125)))[2]
            # Drop unknown speakers AND padded lines that carry no spoken words
            # ("(Grunting)") — they must not reach TTS, and they must not count
            # toward the runtime review's dialogue floor.
            kept = [d for d in shot.get("dialogue", [])
                    if d.get("char") in valid
                    and _has_spoken_text(d.get("line") or "")]
            if kept != shot.get("dialogue", []):
                shot["dialogue"] = kept
            shot.setdefault("soundscape", "")
            shot.setdefault("music", "")
            shot.setdefault("references", {})
            if "characters" in shot["references"]:
                shot["references"]["characters"] = [
                    c for c in shot["references"]["characters"] if c in valid]
    # Guarantee the cast pre-section and a top-level summary.
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
    if not (script.get("summary") or "").strip():
        script["summary"] = " ".join(
            (sc.get("summary") or "").strip() for sc in script.get("scenes", [])
            if (sc.get("summary") or "").strip())
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
            dev = develop_episode(self.show, episode, llm=self.llm, cfg=self.cfg)
            if isinstance(dev, dict):
                return dev.get("synopsis", "") or _fallback_synopsis(
                    episode, _overall_state(self.show, bible),
                    _plotline_state(self.show, bible))
            return str(dev or "")
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
        target = int(bible.get("runtime_target_s", 1320) or 1320)
        dur = _runtime_review(script, target)
        if dur:
            reviews["duration"] = dur
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
            dur = _runtime_review(script, target)
            if dur:
                reviews["duration"] = dur
            passed = all_pass(reviews)

        return {"script": script, "reviews": reviews, "rounds": rounds, "passed": passed,
                "episode": episode}

    def _report_output(self, detail: str, text: str) -> None:
        ACTIVITY[self.show.show_id] = {"detail": detail, "output": text[-600:],
                                       "ts": time.time()}

    def _ask_script(self, system: str, bible, synopsis, continuity, characters) -> dict[str, Any]:
        return self.llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompts.script_prompt(bible, synopsis, continuity,
                                                               characters)}],
            model=self._role_model("script"), temperature=0.8, max_tokens=65536,
            on_progress=lambda n, t: self._report_output(
                f"Episode script — writing ({n} tokens generated…)", t))

    def _ask_revision(self, system: str, script, reviews) -> dict[str, Any]:
        return self.llm.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompts.revision_prompt(script, reviews)}],
            model=self._role_model("script"), temperature=0.6, max_tokens=65536,
            on_progress=lambda n, t: self._report_output(
                f"Episode script — revising ({n} tokens generated…)", t))
