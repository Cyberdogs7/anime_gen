"""Mature-content SERVICE spec: the per-episode FLOOR and the periodic CEILING.

A mature/X-rated show is two tiers:

* **Floor (every episode):** concrete, countable shot-level ecchi beats — panty
  shots, bare-chest reveals, the episode's service set piece. The
  ``fan_service`` reviewer verifies these against a running ledger and fails
  when a required element is MISSING (a critical failure).
* **Ceiling (periodic):** a fully explicit "payoff" scene — the reward for all
  the teasing before it. It is SCHEDULED, not left to the model, and it is
  VARIED per season (never always the same episode number).

This module is the single owner of that spec: it reads ``mature_spec`` from the
bible, decides which episodes carry the ceiling, and tallies the floor ledger.
Prompt text lives in :mod:`studio.prompts`; this is the logic other modules call.
"""
from __future__ import annotations

import random
from typing import Any

from .config import get_config

# Content policies / quotients that mark a show as mature.
_MATURE_QUOTIENTS = {
    "light_ecchi", "ecchi", "explicit_ecchi", "explicit", "x", "x-rated",
    "hentai", "nc-17", "xxx",
}
_MATURE_POLICIES = {"mature", "adult", "explicit", "18+", "nc-17", "xxx", "x"}


def spec(bible: dict[str, Any] | None) -> dict[str, Any]:
    """The show's ``mature_spec`` block (or ``{}``)."""
    return (bible or {}).get("mature_spec") or {}


def is_mature(bible: dict[str, Any] | None) -> bool:
    """True when the show declares mature/X content via spec or content policy."""
    ms = spec(bible)
    if ms.get("quotient"):
        return True
    pol = str((bible or {}).get("content_policy") or "").strip().lower()
    return pol in _MATURE_POLICIES


def explicit_level(bible: dict[str, Any] | None) -> str:
    """'light_ecchi' | 'ecchi' | 'explicit_ecchi' | 'explicit'. Unset -> ecchi."""
    ms = spec(bible)
    lvl = str(ms.get("quotient") or "").strip().lower()
    if lvl in _MATURE_QUOTIENTS:
        return lvl
    pol = str((bible or {}).get("content_policy") or "").strip().lower()
    if pol in ("explicit", "x", "x-rated", "hentai", "nc-17", "xxx"):
        return "explicit"
    return "ecchi"


def floor(bible: dict[str, Any] | None) -> dict[str, int]:
    """Per-episode FLOOR minimums, as countable shot items.

    Reads ``mature_spec.per_episode_floor`` and falls back to the legacy
    ``quotas.service_scenes_per_episode`` (as a service-scene count). Missing
    keys default to 0 so a show can opt out item by item.
    """
    ms = spec(bible)
    fl = dict(ms.get("per_episode_floor") or {})

    def _min(v: Any) -> int:
        if isinstance(v, (list, tuple)):
            v = v[0] if v else 0
        try:
            return int(v or 0)
        except Exception:
            return 0

    out: dict[str, int] = {}
    for key in ("panty_shots", "bare_chest_reveals", "service_scenes"):
        out[key] = _min(fl.get(key, 0))
    if not out.get("service_scenes"):
        q = (ms.get("quotas") or {}).get("service_scenes_per_episode")
        if isinstance(q, (list, tuple)) and q:
            try:
                out["service_scenes"] = int(q[0])
            except Exception:
                pass
    return out


def _range(v: Any) -> tuple[int, int]:
    """Normalize an int or [lo, hi] into (lo, hi)."""
    if isinstance(v, (list, tuple)) and v:
        lo, hi = int(v[0]), int(v[-1] if len(v) > 1 else v[0])
        return (min(lo, hi), max(lo, hi))
    try:
        n = int(v or 0)
    except Exception:
        n = 0
    return (n, n)


def ceiling(bible: dict[str, Any] | None) -> dict[str, Any]:
    """The periodic CEILING spec: a fully explicit payoff scene.

    ``per_arc`` may be an int OR a range ``[lo, hi]`` — how many ceiling episodes
    per season (the actual count is chosen per season by :func:`reward_count` so
    it varies, like the slot). ``conditions`` = what earns one, ``characters`` =
    who may carry one. ``enabled`` defaults True for an explicit-level show.
    """
    ms = spec(bible)
    c = dict(ms.get("explicit_sex_scenes") or {})
    raw = c.get("per_arc")
    if raw is None:
        raw = (ms.get("quotas") or {}).get("reward_scenes_per_arc", 0)
    lo, hi = _range(raw)
    c["per_arc_min"] = lo
    c["per_arc_max"] = hi
    c["per_arc"] = lo            # backward-compatible single value = the minimum
    c.setdefault("conditions", "earned by a duel win, a debt called in, a "
                 "confession, or a claimed stake")
    c.setdefault("enabled", explicit_level(bible) == "explicit")
    return c


def reward_count(show_id: str, season_no: int, bible: dict[str, Any] | None) -> int:
    """How many ceiling episodes this season carries — chosen within ``per_arc``.

    A range yields a season-stable count that VARIES across seasons (seeded by
    show + season), so the number of payoff scenes differs season to season too.
    """
    c = ceiling(bible)
    lo, hi = int(c.get("per_arc_min", 0)), int(c.get("per_arc_max", 0))
    if hi <= 0:
        return 0
    if lo == hi:
        return lo
    rng = random.Random(f"{show_id}|S{int(season_no)}|reward_count")
    return rng.randint(lo, hi)


def reward_episodes(show_id: str, season_no: int, n_episodes: int, count: int,
                    *, seed: str = "") -> list[int]:
    """Which in-season episode positions (1..n_episodes) carry the CEILING scene.

    VARIED per season: seeded by (show_id, season), so a given season is stable
    and reproducible, but no season reuses the previous season's slot (never
    "E04 is always the explicit episode"). One reward is placed per equal BAND
    of the season, with a within-band jitter, so rewards stay spaced out.
    """
    n = max(1, int(n_episodes))
    k = max(0, min(int(count), n))
    if k == 0:
        return []
    rng = random.Random(f"{show_id}|S{int(season_no)}|reward|{seed}")
    band = n / k
    picks: list[int] = []
    for i in range(k):
        lo = int(i * band) + 1
        hi = max(lo, min(int((i + 1) * band), n))
        picks.append(rng.randint(lo, hi))
    return sorted(set(picks))


def is_reward_episode(show, episode: int, cfg=None) -> bool:
    """True when this GLOBAL episode number is a scheduled CEILING episode."""
    bible = show.read_bible()
    c = ceiling(bible)
    if not c.get("enabled") or c.get("per_arc_max", 0) <= 0:
        return False
    from .season import (default_num_episodes, season_episode_index,
                         season_number_for_episode)
    cfg = cfg or get_config()
    n = default_num_episodes(cfg)
    season_no = season_number_for_episode(episode, n)
    pos = season_episode_index(episode, n)
    count = reward_count(show.show_id, season_no, bible)
    return pos in reward_episodes(show.show_id, season_no, n, count)


def service_ledger(scenes: list[dict[str, Any]] | None) -> dict[str, int]:
    """Tally floor/ceiling items across an episode's scenes.

    Counts, per scene list, the ``service`` markers a scene-detail/shot pass
    stamps: ``service_scenes`` (scenes with a service beat), ``panty_shots``,
    ``bare_chest_reveals`` and ``explicit`` (ceiling scenes). Structure-agnostic:
    a scene may carry ``service: {panty_shots: 1, bare_chest_reveals: 0,
    explicit: false}`` or the counts may live on individual shots.
    """
    led = {"service_scenes": 0, "panty_shots": 0, "bare_chest_reveals": 0,
           "explicit": 0}
    for sc in (scenes or []):
        if not isinstance(sc, dict):
            continue
        items = [sc.get("service")]
        for sh in (sc.get("shots") or []):
            if isinstance(sh, dict):
                items.append(sh.get("service"))
        scene_has_service = False
        for it in items:
            if not isinstance(it, dict):
                continue
            p = int(it.get("panty_shots", 0) or 0)
            b = int(it.get("bare_chest_reveals", 0) or 0)
            e = bool(it.get("explicit", False))
            led["panty_shots"] += p
            led["bare_chest_reveals"] += b
            if e:
                led["explicit"] += 1
            if p or b or e or it.get("service"):
                scene_has_service = True
        if scene_has_service:
            led["service_scenes"] += 1
    return led


def ledger_shortfalls(bible: dict[str, Any] | None, led: dict[str, int],
                      *, reward: bool = False) -> list[str]:
    """The FLOOR (and, on a reward episode, CEILING) items not yet met.

    Returns a list of human-readable shortfalls; empty means the ledger clears.
    These are the REQUIRED ELEMENTS whose absence the fan_service reviewer treats
    as a critical failure.
    """
    out: list[str] = []
    fl = floor(bible)
    for key, label in (("panty_shots", "panty shots"),
                       ("bare_chest_reveals", "bare-chest reveals"),
                       ("service_scenes", "service scenes")):
        need = fl.get(key, 0)
        have = int((led or {}).get(key, 0))
        if need and have < need:
            out.append(f"{label}: {have}/{need}")
    c = ceiling(bible)
    if reward and c.get("enabled") and int((led or {}).get("explicit", 0)) < 1:
        out.append("explicit payoff scene: 0/1")
    return out
