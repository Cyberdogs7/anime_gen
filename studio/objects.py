"""Single owner of recurring-object refs.

Mirrors :mod:`studio.refs` for characters: ONE place that reads/writes an object
ref's persisted state (``runs/EP##/objects/approvals.json`` and ``prompts.json``),
so approval/prompt handling can't drift across modules. Nothing else should touch
those files directly.
"""
from __future__ import annotations

import json
from pathlib import Path

from .show import Show


def objects_dir(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / "objects"


def _load(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8")) or default
    except Exception:
        return default


# ---- approvals ----
def approved_slugs(show: Show, episode: int) -> set[str]:
    return set(_load(objects_dir(show, episode) / "approvals.json", {}).get("approved") or [])


def rejected_notes(show: Show, episode: int) -> dict[str, str]:
    """Slugs marked REJECTED (awaiting rerender) -> their rejection notes."""
    return dict(_load(objects_dir(show, episode) / "approvals.json", {}).get("rejected") or {})


def approve(show: Show, episode: int, slug: str) -> None:
    od = objects_dir(show, episode)
    od.mkdir(parents=True, exist_ok=True)
    data = _load(od / "approvals.json", {})
    slugs = set(data.get("approved") or [])
    slugs.add(slug)
    data["approved"] = sorted(slugs)
    rej = dict(data.get("rejected") or {})
    rej.pop(slug, None)                       # approving clears the rejected marker
    if rej:
        data["rejected"] = rej
    else:
        data.pop("rejected", None)
    (od / "approvals.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def unapprove(show: Show, episode: int, slug: str) -> None:
    od = objects_dir(show, episode)
    ap = od / "approvals.json"
    if not ap.exists():
        return
    data = _load(ap, {})
    data["approved"] = [s for s in (data.get("approved") or []) if s != slug]
    ap.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def mark_rejected(show: Show, episode: int, slug: str, notes: str = "") -> None:
    """Mark an object ref REJECTED (awaiting rerender): unapprove + record notes."""
    od = objects_dir(show, episode)
    od.mkdir(parents=True, exist_ok=True)
    data = _load(od / "approvals.json", {})
    data["approved"] = [s for s in (data.get("approved") or []) if s != slug]
    rej = dict(data.get("rejected") or {})
    rej[slug] = notes or ""
    data["rejected"] = rej
    (od / "approvals.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---- generation prompts ----
def prompts(show: Show, episode: int) -> dict[str, str]:
    return _load(objects_dir(show, episode) / "prompts.json", {})


def save_prompt(show: Show, episode: int, slug: str, prompt: str) -> None:
    od = objects_dir(show, episode)
    od.mkdir(parents=True, exist_ok=True)
    data = prompts(show, episode)
    data[slug] = prompt
    (od / "prompts.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
