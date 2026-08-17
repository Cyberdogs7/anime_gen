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


def _plotline_kind(show: Show, p: dict[str, Any]) -> str:
    """Classify a plotline as 'battle' or 'character'.

    Explicit ``kind`` on the plotline wins. Otherwise a thread whose cast includes
    an enemy/visual entity (bio-mechs, swarms) is a battle thread — those entities
    are the threat actors, even when a main-cast member also appears alongside
    them. A thread with NO enemy entity is a character thread.
    """
    kind = (p.get("kind") or "").strip().lower()
    if kind:
        return "battle" if kind in {"battle", "combat", "threat"} else "character"
    cfg = get_config()
    enemies = [n.lower() for n in cfg.get("growth", "enemy_entity_names", [])]
    chars = [str(c).lower() for c in (p.get("characters") or [])]
    involved_enemy = any(any(e in c for e in enemies) for c in chars)
    if involved_enemy:
        return "battle"
    return "character"


def _plotline_state(show: Show, bible: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge bible plotline seeds with continuity state into one active list."""
    continuity = show.read_continuity()
    by_id: dict[str, dict[str, Any]] = {}
    for p in bible.get("plotlines", []) or []:
        p = dict(p)
        p.setdefault("status", "active")
        p.setdefault("last_seen_episode", 0)
        p.setdefault("kind", _plotline_kind(show, p))
        by_id[p.get("id") or p.get("name", "")] = p
    for p in continuity.get("plotlines", []) or []:
        p = dict(p)
        pid = p.get("id") or p.get("name", "")
        if pid in by_id:
            by_id[pid].update(p)
        else:
            p.setdefault("status", "active")
            p.setdefault("last_seen_episode", 0)
            p.setdefault("kind", _plotline_kind(show, p))
            by_id[pid] = p
    out = list(by_id.values())
    for p in out:
        p.setdefault("kind", _plotline_kind(show, p))
    return out


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


def _episodes_since_battle(continuity: dict[str, Any], episode: int) -> int:
    """How many episodes since the last battle episode (default: all of them, so a
    battle is allowed immediately — the cadence check then throttles it)."""
    last = continuity.get("last_battle_episode")
    return episode if last is None else max(0, episode - int(last))


def _roll_int(rng, lo: int, hi: int) -> int:
    return rng.randint(lo, hi) if rng else 1


def roll_episode_plotlines(show: Show, episode: int, plotlines: list[dict[str, Any]],
                           cfg=None, rng=None, battle_cadence: int = 3,
                           overall: dict[str, Any] | None = None) -> dict[str, Any]:
    """ROLL for this episode's plotlines (replaces LLM plotline selection).

    Rules:
    - Running arcs (a plotline with ``arc_remaining > 0``) are ALWAYS featured —
      they override the pool. ``NEW`` is always an option.
    - Roll N (1-4) = total plotlines to feature.
    - Fill the remaining slots by rolling from the available pool: plotlines that
      are not running and not cooling down. Each rolled plotline gets a fresh arc
      length (1-6; battle threads capped shorter). ``NEW``, when rolled, means a
      new plotline is invented by the LLM and given a rolled arc length (1-6).
    - Battle plotlines are only available if the battle cadence is satisfied.
    - The OVERALL plotline (when given) is never rolled separately; a plotline
      that duplicates it by name is treated as the overall and excluded.

    READ-ONLY: returns a decision; arc/cooldown bookkeeping is committed later at
    PLAN APPROVAL. Returns:
    {"featured": [ids], "new_roll": bool, "arc_lengths": {id: int},
     "new_arc_length": int|None, "episode_type": str}
    """
    cfg = cfg or get_config()
    import random
    rng = rng or random.Random()
    battle_kinds = set(cfg.get("growth", "battle_plotline_kinds", ["battle"]))
    n_min = int(cfg.get("growth", "plotlines_per_episode_min", 1) or 1)
    n_max = int(cfg.get("growth", "plotlines_per_episode_max", 4) or 4)
    arc_min = int(cfg.get("growth", "arc_length_min", 1) or 1)
    arc_max = int(cfg.get("growth", "arc_length_max", 6) or 6)
    barc_min = int(cfg.get("growth", "battle_arc_length_min", 1) or 1)
    barc_max = int(cfg.get("growth", "battle_arc_length_max", 3) or 3)

    # The overall plotline is always present; a plotline sharing its name is the
    # same thread and must not be rolled separately.
    overall_name = (overall or {}).get("name") or ""
    overall_id = (overall or {}).get("id")

    # Running arcs override: plotlines with episodes remaining MUST be featured.
    running = [p for p in plotlines
               if int(p.get("arc_remaining", 0) or 0) > 0]
    episodes_since_battle = _episodes_since_battle(show.read_continuity(), episode)
    battle_due = episodes_since_battle >= battle_cadence

    # Available pool = active, not running, not cooling. Battle threads only enter
    # when the cadence allows a battle. Overall-duplicate threads are excluded.
    def _available(p):
        if str(p.get("status", "active")).lower() == "dormant":
            return False
        if p.get("id") == overall_id:
            return False
        if overall_name and (p.get("name") or "") == overall_name:
            return False
        if int(p.get("arc_remaining", 0) or 0) > 0:
            return False
        if int(p.get("cooldown_remaining", 0) or 0) > 0:
            return False
        if p.get("kind") in battle_kinds and not battle_due:
            return False
        return True

    pool = [p for p in plotlines if _available(p)]

    n = _roll_int(rng, n_min, n_max)
    featured: list[str] = [p.get("id") for p in running if p.get("id")]
    arc_lengths: dict[str, int] = {}

    # Fill remaining slots: NEW is always an option in the roll.
    slots = max(0, n - len(featured))
    picks: list[Any] = []
    if slots > 0:
        # Roll candidates = the pool + the NEW token.
        cands = list(pool) + [None]   # None == NEW
        # Shuffle to avoid any order bias, then take slots.
        rng.shuffle(cands)
        picks = cands[:slots]
    for pick in picks:
        if pick is None:
            continue   # NEW handled by caller
        pid = pick.get("id")
        if pid in featured:
            continue
        featured.append(pid)
        hi = barc_max if pick.get("kind") in battle_kinds else arc_max
        lo = barc_min if pick.get("kind") in battle_kinds else arc_min
        arc_lengths[pid] = _roll_int(rng, lo, hi)

    # Did the roll include NEW?
    new_roll = None in picks
    new_arc_length = _roll_int(rng, arc_min, arc_max) if new_roll else None

    # Episode type: battle if a battle plotline is featured (running or rolled),
    # else character. A running battle arc forces battle until it completes.
    episode_type = "battle" if any(
        p.get("kind") in battle_kinds
        for p in plotlines if p.get("id") in featured
    ) else "character"

    return {"featured": featured, "new_roll": new_roll,
            "arc_lengths": arc_lengths, "new_arc_length": new_arc_length,
            "episode_type": episode_type}


def develop_episode(show: Show, episode: int, llm=None, cfg=None, notes: str = "",
                    rng=None) -> dict[str, Any]:
    """Pick plotlines (overall always featured), optionally vet a new plotline, and
    return the episode synopsis + what to feature.

    READ-ONLY: this stage must NOT mutate continuity or the bible. Plotline
    bookkeeping (last_seen stamps, new-plotline canon persistence, new character
    sheets) happens later at PLAN APPROVAL via
    growth.commit_plotline_on_approval. Otherwise every rejected/regenerated
    episode would keep re-growing the same plotlines (the EP01 Apex-734 pile-up).

    Returns {"synopsis": str, "featured": [ids], "new_plotline": dict|None,
             "episode_type": str, "arc_lengths": {id: int}, "new_arc_length": int|None}.
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
    battle_cadence = int(cfg.get("growth", "battle_cadence_episodes", 3) or 3)
    battle_kinds = set(cfg.get("growth", "battle_plotline_kinds", ["battle"]))
    since = _episodes_since_new_plotline(continuity, episode)

    # ROLL for this episode's plotlines instead of letting the LLM pick. Running
    # arcs override; NEW is always an option; arc lengths and cooldowns are rolled
    # here and committed at approval.
    roll = roll_episode_plotlines(show, episode, plotlines, cfg=cfg,
                                  battle_cadence=battle_cadence, rng=rng,
                                  overall=overall)
    episode_type = roll["episode_type"]
    featured = list(roll["featured"])
    # The overall plotline is ALWAYS present regardless of the roll.
    if overall and overall.get("id") not in featured:
        featured.insert(0, overall["id"])

    # Build the candidate list the model is ALLOWED to use — exactly the rolled
    # set plus (if NEW was rolled) the instruction to invent one.
    chosen = [p for p in plotlines if p.get("id") in featured]
    user = prompts.development_prompt(
        episode, overall, chosen, continuity.get("unresolved_threads", []) or [],
        continuity, characters, new_cands, notes,
        cadence=cadence, episodes_since_new=since, episode_type=episode_type,
        new_roll=roll["new_roll"], featured=featured)
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
                "featured": featured, "new_plotline": None,
                "episode_type": episode_type,
                "arc_lengths": roll["arc_lengths"],
                "new_arc_length": roll["new_arc_length"]}
    try:
        data = llm.chat_json([{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                             model=model, temperature=0.6, max_tokens=4096)
    except Exception:
        return fallback
    if not isinstance(data, dict) or not data.get("synopsis"):
        return fallback
    # Vet a proposed new plotline with the LLM reviewer (read-only). It is NOT
    # persisted here — only reviewed so a rejected proposal is dropped now and an
    # approved one is committed at plan approval. Only allowed if NEW was rolled.
    new_plotline = None
    if roll["new_roll"]:
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
    return {"synopsis": data["synopsis"], "featured": featured,
            "new_plotline": new_plotline,
            "episode_type": episode_type,
            "arc_lengths": roll["arc_lengths"],
            "new_arc_length": roll["new_arc_length"]}
