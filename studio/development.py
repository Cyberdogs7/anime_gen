"""Development stage - decides what happens in the next episode of a continuous,
plotline-driven show (Ranma 1/2-style, no seasons). DESIGN.md §9 Stage 1.

Every episode features the show's OVERALL plotline (the single continuous driving
force - always present) plus a rotating subset of the other active plotlines (which
may advance, cameo, or overlap), and occasionally introduces a new plotline (e.g. a
newly-added character with a love interest). Continuity tracks plotline state across
episodes; this module returns the episode synopsis for the writers' room to turn into
a script.
"""
from __future__ import annotations

from typing import Any

from . import prompts
from .clients import LMStudioClient
from .config import get_config
from .show import Show


def _overall_state(show: Show, bible: dict[str, Any]) -> dict[str, Any] | None:
    """Merge the bible's overall_plotline with continuity state (last_seen/status)."""
    op = bible.get("overall_plotline")
    if not op or not isinstance(op, dict):
        return None
    op = dict(op)
    cop = show.read_continuity().get("overall_plotline") or {}
    op["last_seen_episode"] = cop.get("last_seen_episode", 0)
    op["status"] = cop.get("status", op.get("status", "active"))
    return op


def _plotline_state(show: Show, bible: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge bible plotline seeds with continuity state into one active list."""
    continuity = show.read_continuity()
    by_id: dict[str, dict[str, Any]] = {}
    for p in bible.get("plotlines", []) or []:
        p = dict(p)
        p.setdefault("status", "active")
        p.setdefault("last_seen_episode", 0)
        by_id[p.get("id") or p.get("name", "")] = p
    for p in continuity.get("plotlines", []) or []:
        p = dict(p)
        pid = p.get("id") or p.get("name", "")
        if pid in by_id:
            by_id[pid].update(p)
        else:
            p.setdefault("status", "active")
            p.setdefault("last_seen_episode", 0)
            by_id[pid] = p
    return list(by_id.values())


def _new_character_candidates(show: Show, plotlines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cast members that exist but are not yet tied into any plotline."""
    involved: set[str] = set()
    for p in plotlines:
        involved.update(p.get("characters", []) or [])
    return [c for cid in show.list_characters()
            if (c := show.read_character(cid)).get("name") and c["name"] not in involved]


def _fallback_synopsis(episode: int, overall: dict[str, Any] | None,
                       plotlines: list[dict[str, Any]]) -> str:
    parts = []
    if overall and overall.get("name"):
        parts.append(f"{overall['name']}: {overall.get('summary', '')}")
    active = [p for p in plotlines if str(p.get("status", "active")).lower() != "resolved"]
    if not parts and not active:
        return f"Episode {episode}: an ordinary day, but everything changes."
    picks = sorted(active, key=lambda p: p.get("last_seen_episode", 0))[:3]
    parts.extend(f"{p.get('name')}: {p.get('summary', '')}" for p in picks)
    return f"Episode {episode}: " + " | ".join(parts)


def _record_seen(show: Show, continuity: dict[str, Any], episode: int,
                 data: dict[str, Any], merged: list[dict[str, Any]],
                 overall: dict[str, Any] | None) -> None:
    """Persist plotline state into continuity: last_seen for featured threads, the
    always-present overall plotline, and any newly-introduced plotline."""
    featured = {f.get("id") for f in data.get("featured_plotlines", []) if isinstance(f, dict)}
    for p in merged:
        if p.get("id") in featured:
            p["last_seen_episode"] = episode
    new_plotline = data.get("new_plotline")
    if isinstance(new_plotline, dict) and new_plotline.get("id"):
        new_plotline.setdefault("status", "active")
        new_plotline["last_seen_episode"] = episode
        # Idempotent: a previous approved episode may already carry this thread.
        if new_plotline["id"] not in {p.get("id") for p in merged}:
            merged.append(new_plotline)
        continuity["last_new_plotline_episode"] = episode
    continuity["plotlines"] = merged
    if overall:
        overall["last_seen_episode"] = episode
        continuity["overall_plotline"] = overall
    show.write_continuity(continuity)


def _episodes_since_new_plotline(continuity: dict[str, Any], episode: int) -> int:
    """How many episodes since a plotline was last introduced (default: all of them)."""
    last = continuity.get("last_new_plotline_episode")
    return episode if last is None else max(0, episode - int(last))


def develop_episode(show: Show, episode: int, llm=None, cfg=None, notes: str = "") -> dict[str, Any]:
    """Pick plotlines (overall always featured), optionally vet a new plotline, and
    return the episode synopsis + what to feature.

    READ-ONLY: this stage must NOT mutate continuity or the bible. Plotline
    bookkeeping (last_seen stamps, new-plotline canon persistence, new character
    sheets) happens later at PLAN APPROVAL via
    growth.commit_plotline_on_approval. Otherwise every rejected/regenerated
    episode would keep re-growing the same plotlines (the EP01 Apex-734 pile-up).

    Returns {"synopsis": str, "featured": [ids], "new_plotline": dict|None}.
    """
    cfg = cfg or get_config()
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"))
    bible = show.read_bible()
    continuity = show.read_continuity()
    overall = _overall_state(show, bible)
    plotlines = _plotline_state(show, bible)
    characters = [show.read_character(cid) for cid in show.list_characters()]
    new_cands = _new_character_candidates(show, plotlines)
    roles = cfg.get("llm", "roles", {})
    model = roles.get("showrunner") or cfg.get("llm", "model")
    profile = cfg.get("show_profile") or {}
    system = prompts.showrunner_system(profile, bible.get("content_policy", "mature"),
                                       profile.get("baseline", "ranma-1-2"))
    cadence = int(cfg.get("growth", "plotline_cadence_episodes", 3) or 3)
    since = _episodes_since_new_plotline(continuity, episode)
    user = prompts.development_prompt(
        episode, overall, plotlines, continuity.get("unresolved_threads", []) or [],
        continuity, characters, new_cands, notes,
        cadence=cadence, episodes_since_new=since)
    # Durable director constraints shape plotline selection, not just output text.
    try:
        from .planner import read_director_notes
        dir_notes = read_director_notes(show)
        if dir_notes:
            user += ("\nDIRECTOR'S STANDING CONSTRAINTS (ABSOLUTE, apply to plotline "
                     "selection and the synopsis; if a plotline conflicts, do NOT feature "
                     "it this episode):\n- " + "\n- ".join(dir_notes))
    except Exception:
        pass
    fallback = {"synopsis": _fallback_synopsis(episode, overall, plotlines),
                "featured": [], "new_plotline": None}
    try:
        data = llm.chat_json([{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                             model=model, temperature=0.7, max_tokens=4096)
    except Exception:
        return fallback
    if not isinstance(data, dict) or not data.get("synopsis"):
        return fallback
    # Vet a proposed new plotline with the LLM reviewer (read-only). It is NOT
    # persisted here — only reviewed so a rejected proposal is dropped now and an
    # approved one is committed at plan approval.
    new_plotline = None
    if isinstance(data.get("new_plotline"), dict) and data["new_plotline"].get("id"):
        try:
            from .growth import review_new_plotline
            result = review_new_plotline(show, data["new_plotline"], episode,
                                         llm=llm, cfg=cfg)
            if result.get("approved"):
                new_plotline = result["plotline"]
        except Exception as exc:
            log = __import__("logging").getLogger(__name__)
            log.warning("growth review failed for episode %s: %s", episode, exc)
            new_plotline = None
    featured = [f.get("id") for f in (data.get("featured_plotlines") or [])
                if isinstance(f, dict) and f.get("id")]
    return {"synopsis": data["synopsis"], "featured": featured,
            "new_plotline": new_plotline}
