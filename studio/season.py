"""Season plan — top of the script hierarchy.

Hierarchy (top-down, strict canon):
    Season (12 high-level episode plot points)
      -> Episode synopsis (development expands ONE season entry)
        -> Episode outline/plan (plan realizes the synopsis)
          -> Scene details -> Scene shots -> Assembled script

The season plan is generated once per season and approved at its own gate.
Every downstream stage must realize the assigned episode entry — rolls are
gone; the season file is the single source of truth for what happens when.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import prompts
from .bootstrap import ACTIVITY
from .config import get_config
from .show import Show


def season_number_for_episode(episode: int, num_episodes: int = 12) -> int:
    """Which season contains a global episode number (1-indexed)."""
    return max(1, (int(episode) - 1) // int(num_episodes) + 1)


def season_episode_index(episode: int, num_episodes: int = 12) -> int:
    """1-indexed position of the episode inside its season."""
    return (int(episode) - 1) % int(num_episodes) + 1


def default_num_episodes(cfg=None) -> int:
    cfg = cfg or get_config()
    try:
        return max(1, int(cfg.get("growth", "season_episodes", 12) or 12))
    except Exception:
        return 12


def read_season_entry(show: Show, episode: int, cfg=None) -> tuple[int, dict[str, Any]]:
    """Return (season_no, episode_entry) for a global episode number, or (0, {}).

    Looks up the approved (or pending) season file and finds the entry whose
    ``episode`` equals the global episode number. Empty entry means no season
    covers this episode yet.
    """
    cfg = cfg or get_config()
    n = default_num_episodes(cfg)
    season_no = season_number_for_episode(episode, n)
    data = show.read_season(season_no)
    for e in data.get("episodes", []) or []:
        try:
            if int(e.get("episode", -1)) == int(episode):
                return season_no, dict(e)
        except Exception:
            continue
    return season_no, {}


def generate_season_plan(show: Show, season: int = 1, num_episodes: int = 0,
                         llm=None, cfg=None, feedback: str = "",
                         start_episode: int = 1) -> dict[str, Any]:
    """Generate the full season of high-level plot points. Writes S<NN>.json.

    ``start_episode`` is the global episode number of the season's EP1 slot
    (season 1 starts at 1, season 2 at 13, ...). With feedback, revises the
    existing file in place instead of generating fresh.
    """
    from .clients.lmstudio import LMStudioClient
    from .development import _overall_state, _plotline_state

    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=300)
    num_episodes = int(num_episodes or default_num_episodes(cfg))
    if start_episode < 1:
        start_episode = (int(season) - 1) * num_episodes + 1
    roles = cfg.get("llm", "roles", {})
    model = roles.get("showrunner") or cfg.get("llm", "model")
    profile = show.style_profile(cfg)
    bible = show.read_bible()
    characters = [show.read_character(cid) for cid in show.list_characters()]
    overall = _overall_state(show, bible)
    plotlines = _plotline_state(show, bible)
    try:
        from .planner import read_director_notes
        dir_notes = read_director_notes(show)
    except Exception:
        dir_notes = []

    system = prompts.showrunner_system(
        profile, bible.get("content_policy", "mature"),
        profile.get("baseline", ""))

    if feedback.strip() and show.read_season(season).get("episodes"):
        current = show.read_season(season)
        user = (prompts.season_plan_prompt(
            bible, characters, overall, plotlines, season, num_episodes,
            feedback="", director_constraints=dir_notes)
            + f"\nCURRENT REJECTED SEASON — revise it in place, keep what the "
              f"feedback did not flag:\n{json.dumps(current, ensure_ascii=False)}\n"
              f"DIRECTOR'S FEEDBACK (ABSOLUTE):\n{feedback}\n"
              "Return ONLY the revised season JSON.")
    else:
        user = prompts.season_plan_prompt(
            bible, characters, overall, plotlines, season, num_episodes,
            feedback=feedback, director_constraints=dir_notes)

    ACTIVITY[show.show_id] = {"detail": f"Season {season}: planning {num_episodes} episodes…",
                              "ts": time.time()}
    data = llm.chat_json(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        model=model, temperature=0.6, max_tokens=32768, role="showrunner",
        on_progress=lambda n, t: ACTIVITY.update({show.show_id: {
            "detail": f"Season {season}: planning ({n} tokens…)",
            "output": t[-500:], "ts": time.time()}}))
    if not isinstance(data, dict) or not data.get("episodes"):
        raise RuntimeError(f"season {season} plan returned no episodes")

    # Normalize: exactly num_episodes entries, global numbering, strict types.
    eps = [e for e in data.get("episodes", []) if isinstance(e, dict)]
    eps = eps[:num_episodes]
    fixed: list[dict[str, Any]] = []
    for i, e in enumerate(eps):
        ep_no = start_episode + i
        e = dict(e)
        e["episode"] = ep_no
        e.setdefault("title", f"Episode {ep_no}")
        et = str(e.get("episode_type", "character")).lower()
        e["episode_type"] = "battle" if et == "battle" else "character"
        e.setdefault("logline", "")
        feats = e.get("featured_plotlines", []) or []
        e["featured_plotlines"] = [f for f in feats if isinstance(f, str) and f][:4]
        beats = [b for b in (e.get("key_beats", []) or []) if isinstance(b, str) and b.strip()]
        e["key_beats"] = beats[:8] or [e.get("logline", "") or f"Episode {ep_no} story."]
        e.setdefault("cliffhanger", "")
        e.setdefault("character_arc", "")
        if not isinstance(e.get("new_plotline"), dict):
            e["new_plotline"] = None
        fixed.append(e)
    # Pad if the model shorted us (shouldn't happen, but keep numbering strict).
    while len(fixed) < num_episodes:
        ep_no = start_episode + len(fixed)
        fixed.append({"episode": ep_no, "title": f"Episode {ep_no}",
                      "episode_type": "character", "logline": "",
                      "featured_plotlines": [], "new_plotline": None,
                      "key_beats": [f"Episode {ep_no} story."],
                      "cliffhanger": "", "character_arc": ""})
    data["season"] = season
    data["episodes"] = fixed
    data["num_episodes"] = num_episodes
    data["start_episode"] = start_episode
    if feedback.strip():
        data["rejected_notes"] = feedback
    data.setdefault("status", "pending")
    show.write_season(data, season)
    return data


def ensure_season(show: Show, episode: int, llm=None, cfg=None) -> tuple[int, dict[str, Any]]:
    """Return (season_no, entry) for episode, generating the season if missing.

    Hands-free: the first episode of an uncovered season triggers season
    generation so development never runs without canon above it.
    """
    cfg = cfg or get_config()
    n = default_num_episodes(cfg)
    season_no = season_number_for_episode(episode, n)
    sno, entry = read_season_entry(show, episode, cfg)
    if entry:
        return sno, entry
    if show.read_season(season_no).get("episodes"):
        return season_no, {}
    start = (season_no - 1) * n + 1
    try:
        data = generate_season_plan(show, season_no, n, llm=llm, cfg=cfg,
                                    start_episode=start)
        for e in data.get("episodes", []) or []:
            try:
                if int(e.get("episode", -1)) == int(episode):
                    return season_no, dict(e)
            except Exception:
                continue
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "season generation failed for %s S%02d: %s", show.show_id, season_no, exc)
    return season_no, {}


def approve_season(show: Show, season: int = 1) -> dict[str, Any]:
    data = show.read_season(season)
    data["status"] = "approved"
    show.write_season(data, season)
    return data


def reject_season(show: Show, season: int = 1, notes: str = "") -> dict[str, Any]:
    data = show.read_season(season)
    data["status"] = "rejected"
    data["rejected_notes"] = notes
    show.write_season(data, season)
    return data


def season_covers(show: Show, episode: int, cfg=None) -> bool:
    """True when a season file already has an entry for this episode."""
    _, entry = read_season_entry(show, episode, cfg)
    return bool(entry)
