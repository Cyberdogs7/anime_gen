"""Per-image assets on a review card.

A Gate 0 image card is a review surface: its artifacts are individual images.
This module names those images, records their review state and keeps a rejected
image from silently disappearing into a batch-level retry.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

CHARACTER_RETAKE_LABELS = frozenset({"nude", "base"})


def _relative(show, path: str | Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = show.dir.parents[1] / p
    try:
        return str(p.resolve().relative_to(show.dir.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def artifact_path(show, asset: dict[str, Any]) -> str:
    if asset.get("type") == "voice":
        folder = "assets/voice" if str(asset.get("file") or "").endswith(".wav") else "voices"
        rel = f"{folder}/{asset.get('file') or ''}"
    elif asset.get("type") == "scene":
        rel = f"scenes/{asset['id']}/refs/{asset.get('file') or ''}"
    else:
        rel = f"characters/{asset['id']}/refs/{asset.get('file') or ''}"
    return f"shows/{show.show_id}/{rel}"


def parse_asset(show, path: str | Path) -> dict[str, Any] | None:
    """Describe a reviewable file as a character, scene or voice asset."""
    rel = _relative(show, path)
    parts = rel.split("/")
    filename = parts[-1] if parts else ""
    full = f"shows/{show.show_id}/{rel}"
    if ((len(parts) == 2 and parts[0] == "voices")
            or (len(parts) == 3 and parts[0] == "assets" and parts[1] == "voice")):
        suffix = Path(filename).suffix.lower()
        if filename.endswith("_voice.wav") or filename.endswith("_voice.yaml"):
            cid = filename.split("_voice.")[0]
            char = show.read_character(cid) or {}
            return {"type": "voice", "id": cid, "file": filename,
                    "path": full, "label": char.get("name") or cid,
                    "retakeable": suffix == ".wav"}
        return None
    if len(parts) < 4 or parts[0] not in ("characters", "scenes"):
        return None
    if Path(rel).suffix.lower() not in IMAGE_EXTS:
        return None
    kind, ident, refs_dir = parts[0], parts[1], parts[2]
    if refs_dir != "refs":
        return None
    filename = "/".join(parts[3:])
    if kind == "scenes":
        return {"type": "scene", "id": ident, "file": filename,
                "path": full, "label": ident}
    from .. import refs
    data = refs.load_refs(show, ident)
    label = ""
    for name, fn in (data.get("variants") or {}).items():
        if fn == filename:
            label = str(name)
            break
    if not label:
        stem = Path(filename).stem
        prefix = f"{ident}_"
        label = stem[len(prefix):] if stem.startswith(prefix) else stem
        label = re.sub(r"_(?:ref_)?\d+$", "", label) or label
    return {"type": "character", "id": ident, "file": filename,
            "path": full, "label": label}


def asset_for_artifact(show, artifact: dict[str, Any]) -> dict[str, Any] | None:
    asset = artifact.get("asset")
    if isinstance(asset, dict) and asset.get("type") in ("scene", "character"):
        asset = dict(asset)
        asset.setdefault("path", artifact.get("path"))
        return asset
    return parse_asset(show, str(artifact.get("path") or ""))


def asset_key(asset: dict[str, Any]) -> str:
    if asset.get("type") == "scene":
        return f"scene:{asset.get('id')}"
    if asset.get("type") == "voice":
        return f"voice:{asset.get('id')}"
    return f"character:{asset.get('id')}:{asset.get('label')}"


def asset_title(asset: dict[str, Any]) -> str:
    if asset.get("type") == "scene":
        return f"location reference: {asset.get('id')}"
    if asset.get("type") == "voice":
        return f"voice sample: {asset.get('label') or asset.get('id')}"
    return f"character reference: {asset.get('id')} ({asset.get('label') or 'ref'})"


def is_retakeable(asset: dict[str, Any] | None) -> bool:
    if not asset:
        return False
    if asset.get("type") == "scene":
        return True
    if asset.get("type") == "voice":
        return bool(asset.get("retakeable"))
    return str(asset.get("label") or "") in CHARACTER_RETAKE_LABELS


def _char_entry(state: dict[str, Any], char_id: str) -> dict[str, Any] | None:
    for entry in state.get("characters") or []:
        if str(entry.get("id") or "") == char_id:
            return entry
        if str(entry.get("name") or "").lower().replace(" ", "-") == char_id.lower():
            return entry
    return None


def mark_rejected(show, asset: dict[str, Any], notes: str = "") -> None:
    from .. import refs, scenes
    if asset.get("type") == "scene":
        sid = str(asset.get("id") or "")
        data = scenes.load_refs(show, sid)
        data.update({"status": "rejected", "approved": False,
                     "rejected_notes": notes or ""})
        scenes.save_refs(show, sid, data)
        return
    if asset.get("type") == "voice":
        # Voice approvals live on the roster entry, not in refs.json: without
        # this branch a voice retake decision silently rewrote image flags and
        # the voice flag stayed stale forever.
        state = show.bootstrap_state()
        entry = _char_entry(state, str(asset.get("id") or ""))
        if entry is not None:
            entry["voice"] = "rejected"
            entry["voice_notes"] = notes or ""
            show.set_bootstrap_state(state)
        return
    cid = str(asset.get("id") or "")
    label = str(asset.get("label") or "")
    refs.mark_rejected(show, cid, label, notes)
    state = show.bootstrap_state()
    entry = _char_entry(state, cid)
    if entry is not None:
        entry["refs"] = "rejected"
        entry["rejected_notes"] = notes or ""
        show.set_bootstrap_state(state)


def mark_pending(show, asset: dict[str, Any], notes: str = "") -> None:
    from .. import scenes
    if asset.get("type") == "scene":
        sid = str(asset.get("id") or "")
        data = scenes.load_refs(show, sid)
        data.update({"status": "pending", "approved": False,
                     "rejected_notes": notes or ""})
        scenes.save_refs(show, sid, data)
        return
    state = show.bootstrap_state()
    entry = _char_entry(state, str(asset.get("id") or ""))
    if entry is not None:
        entry["refs"] = "rejected"
        entry["rejected_notes"] = notes or ""
        show.set_bootstrap_state(state)


def mark_approved(show, asset: dict[str, Any]) -> None:
    from .. import refs, scenes
    if asset.get("type") == "scene":
        sid = str(asset.get("id") or "")
        data = scenes.load_refs(show, sid)
        data.update({"status": "approved", "approved": True})
        data.pop("rejected_notes", None)
        scenes.save_refs(show, sid, data)
        return
    if asset.get("type") == "voice":
        # See mark_rejected: the roster voice flag is the source of truth.
        state = show.bootstrap_state()
        entry = _char_entry(state, str(asset.get("id") or ""))
        if entry is not None:
            entry["voice"] = "approved"
            entry.pop("voice_notes", None)
            show.set_bootstrap_state(state)
        return
    cid = str(asset.get("id") or "")
    label = str(asset.get("label") or "")
    data = refs.load_refs(show, cid)
    if data:
        data["approved"] = sorted(set(data.get("approved") or []) | {label})
        rejected = dict(data.get("rejected") or {})
        rejected.pop(label, None)
        if rejected:
            data["rejected"] = rejected
        else:
            data.pop("rejected", None)
        refs.save_refs(show, cid, data)
    state = show.bootstrap_state()
    entry = _char_entry(state, cid)
    if entry is not None:
        entry["refs"] = "approved"
        entry.pop("rejected_notes", None)
        show.set_bootstrap_state(state)


def open_children(store, parent_id: str) -> list[dict[str, Any]]:
    return [card for card in store.all()
            if str((card.get("payload") or {}).get("parent") or "") == parent_id
            and card.get("state") != "accepted"]


def open_retakes(store, parent_id: str) -> list[dict[str, Any]]:
    return [card for card in open_children(store, parent_id)
            if card.get("kind") == "image_retake"]


def update_artifact(store, card_id: str, path: str,
                    **fields: Any) -> dict[str, Any] | None:
    card = store.get(card_id)
    if not card:
        return None
    artifacts = list(card.get("artifacts") or [])
    changed = False
    target = str(path).replace("\\", "/")
    for artifact in artifacts:
        if str(artifact.get("path") or "").replace("\\", "/") == target:
            if target != artifact.get("path"):
                artifact["path"] = target
            artifact.update(fields)
            changed = True
            break
    if changed:
        return store.update(card_id, artifacts=artifacts)
    return card


def enrich_artifacts(show, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in artifacts or []:
        artifact = dict(raw)
        asset = asset_for_artifact(show, artifact)
        if asset:
            artifact.setdefault("asset", asset)
            artifact.setdefault("retakeable", bool(asset.get(
                "retakeable", is_retakeable(asset))))
        out.append(artifact)
    return out
