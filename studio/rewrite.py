"""One-off script rewrite with an explicitly chosen model.

When a script is stuck in a full-fail state, the automatic fix loops only ever
run with the configured local role model. This lets a human fire a SINGLE rewrite
pass with a hand-picked model to see whether a stronger model clears the writers'
room — without changing the pipeline config or re-running the whole episode.

It is one rewrite + one re-review, then it stops and hands back to the normal
gate. It is NOT a loop: nothing auto-retries, so it can only cost one call on the
chosen model per invocation.

Dashboard: GET the model list from the show payload (``models``) and POST
``/api/show/<id>/rewrite`` {episode, model}. CLI:
``python studio.py rewrite --show <id> --episode 1 --model <id>``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import fsutil, promptlog, prompts
from .config import get_config
from .show import Show


def _http_model_ids(url: str, headers: dict[str, str] | None = None,
                    timeout: float = 5.0) -> list[str]:
    """OpenAI-compatible GET <base>/models -> model ids (embeddings dropped)."""
    import httpx
    r = httpx.get(url, headers=headers or {}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    ids = [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]
    return [i for i in ids if "embed" not in i.lower()]


_MODELS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def pickable_models(cfg=None, ttl: float = 60.0) -> dict[str, list[str]]:
    """Models offered for a manual rewrite, read LIVE from LM Studio.

    Ids come from the local LM Studio ``/models`` endpoint, with the configured
    role/model names as a fallback when the endpoint is unreachable. Cached for
    ``ttl`` seconds so the dashboard's frequent show-payload polls never re-hit
    the network. ``ttl=0`` forces a fresh read (tests).
    """
    cfg = cfg or get_config()
    now = time.time()
    if _MODELS_CACHE["data"] is not None and ttl > 0 and now - _MODELS_CACHE["ts"] < ttl:
        return _MODELS_CACHE["data"]

    local_fallback: list[str] = []
    seen: set[str] = set()
    for m in list((cfg.get("llm", "roles", {}) or {}).values()) + [cfg.get("llm", "model")]:
        if m and str(m) not in seen:
            seen.add(str(m))
            local_fallback.append(str(m))

    local: list[str] = []
    lbase = str(cfg.get("llm", "base_url") or "").rstrip("/")
    if lbase:
        try:
            local = _http_model_ids(lbase + "/models")
        except Exception:
            local = []
    if not local:
        local = local_fallback

    data = {"local": list(dict.fromkeys(local))}
    _MODELS_CACHE["ts"] = now
    _MODELS_CACHE["data"] = data
    return data


def _latest_script_path(show: Show, episode: int) -> Path | None:
    runs = show.dir / "runs" / f"EP{episode:02d}"
    files = fsutil.existing_by_mtime(runs.glob("script.r*.json"))
    return files[-1] if files else None


def _notes_text(review: dict[str, Any]) -> str:
    """Free-form notes text from a persisted ``_review`` verdict (failing reviewers)."""
    from .review import review_notes_text
    revs = review.get("reviews") if isinstance(review, dict) else None
    return review_notes_text(revs) if isinstance(revs, dict) else ""


def rewrite_script_once(show: Show, episode: int, model: str,
                        cfg=None, llm=None, notes: str = "") -> dict[str, Any]:
    """Run ONE rewrite of the latest script with ``model``, then re-review once.

    Returns the rewritten script with a fresh ``_review`` verdict. Raises on a bad
    model choice or an unusable result (the caller surfaces the error; nothing is
    silently downgraded).
    """
    from .clients.lmstudio import LMStudioClient
    from .planner import (attach_review_verdict, read_episode_plan, _normalize,
                          _runtime_review, _target)
    from .review import run_reviewers
    from .bootstrap import ACTIVITY

    cfg = cfg or get_config()
    if not str(model or "").strip():
        raise ValueError("a model id is required for a manual rewrite")
    llm = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=600)

    path = _latest_script_path(show, episode)
    if path is None:
        raise FileNotFoundError(f"no script to rewrite for {show.show_id} EP{episode:02d}")
    script = json.loads(path.read_text(encoding="utf-8"))
    review = script.get("_review") or {}
    notes_text = _notes_text(review)
    if notes.strip():
        notes_text = (notes_text + f"\n\n[director] {notes}").strip()
    try:
        cur_round = int(path.stem.split(".r")[-1])
    except (ValueError, IndexError):
        cur_round = 1
    next_round = cur_round + 1

    bible = show.read_bible()
    style = show.style_profile(cfg)
    system = prompts.showrunner_system(
        style, bible.get("content_policy") or "", style.get("baseline", ""))
    show_id = show.show_id
    ACTIVITY[show_id] = {"detail": f"Episode {episode}: one-shot rewrite with "
                         f"{model}…", "ts": time.time()}
    try:
        out = promptlog.logged_chat_json(
            llm, show, episode, f"script-rewrite-{model}",
            [{"role": "system", "content": system},
             {"role": "user", "content": prompts.revision_prompt(
                 script, notes_text, outline=read_episode_plan(show, episode))}],
            model=model,
            role="showrunner", temperature=0.6, max_tokens=65536,
            on_progress=lambda n, t: ACTIVITY.update({show_id: {
                "detail": f"Episode {episode}: rewriting with {model} ({n} tokens…)",
                "output": t[-500:], "ts": time.time()}}))
    finally:
        ACTIVITY.pop(show_id, None)

    if isinstance(out, dict) and isinstance(out.get("script"), dict):
        out = out["script"]
    if not isinstance(out, dict) or not out.get("scenes"):
        raise RuntimeError(f"rewrite with {model!r} returned an unusable script")

    cast_names = [c.get("name") for c in
                  (show.read_character(cid) for cid in show.list_characters())
                  if c.get("name")]
    script = _normalize(out, cast_names)

    # Persist the new revision, then re-review it ONCE (reviewers use their own
    # configured role models — the chosen model is only billed for the rewrite).
    from . import epstate
    new_path = path.with_name(f"script.r{next_round}.json")
    epstate.save_script(new_path, script)
    ACTIVITY[show_id] = {"detail": f"Episode {episode}: re-reviewing the rewrite…",
                         "ts": time.time()}
    try:
        reviews = run_reviewers(script, show=show, cfg=cfg, llm=llm,
                                episode=episode, round_no=next_round,
                                notes_dir=show.dir / "runs" / f"EP{episode:02d}" / "reviews")
        dur = _runtime_review(script, _target(show))
        if dur:
            reviews["duration"] = dur
        passed = all(v.get("pass") for v in reviews.values())
        attach_review_verdict(script, reviews, passed, next_round)
        epstate.save_script(new_path, script)
    finally:
        ACTIVITY.pop(show_id, None)
    return script


def regenerate_outline_once(show: Show, episode: int, model: str,
                            cfg=None, llm=None, notes: str = "") -> dict[str, Any]:
    """Regenerate the episode OUTLINE once with ``model``, then re-run its review.

    The outline gate is where an episode most often stalls (a downstream writer
    can't fix length/location/fan-service while a bad outline holds it rigid), so
    a human needs to retry the OUTLINE with a stronger model — not just the script.
    The chosen model drives every outline-WRITING call (season, development,
    skeleton, beats); the structure/room reviewers keep their cheap role models.
    """
    from .clients.lmstudio import LMStudioClient
    from .planner import _ModelOverride, generate_episode_plan

    cfg = cfg or get_config()
    if not str(model or "").strip():
        raise ValueError("a model id is required for a manual outline regeneration")
    base = llm or LMStudioClient(cfg.get("llm", "base_url"), timeout=600)
    wrapped = _ModelOverride(base, model=model)
    return generate_episode_plan(show, episode, llm=wrapped, cfg=cfg, notes=notes)
