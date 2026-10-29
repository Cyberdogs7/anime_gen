"""Validate a show's ideas archive; name the pitches to rewrite.

Read-only. Prints JSON ``{"ok": bool, "failures": [{"id", "reason"}]}`` and
exits nonzero when any pitch must be rewritten: bad schema, bad slug,
duplicate slug, or a title that duplicates (or near-duplicates) another
pitch in the batch or the archive. The worker rewrites exactly the flagged
ids and re-runs this checker.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED = ("title", "show_id", "logline", "premise", "genre", "tone",
            "hook", "maturity")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _tokens(text: str) -> set[str]:
    return set(_norm(text).split())


def check(archive: dict) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    pitches = archive.get("pitches") or []
    if not isinstance(pitches, list):
        return [{"id": "", "reason": "archive 'pitches' is not a list"}]
    seen_slugs: dict[str, str] = {}
    seen_titles: dict[str, str] = {}
    for p in pitches:
        if not isinstance(p, dict):
            failures.append({"id": "", "reason": "pitch entry is not an object"})
            continue
        pid = str(p.get("id") or "")
        missing = [f for f in REQUIRED if not p.get(f)]
        if missing:
            failures.append({"id": pid,
                             "reason": f"missing fields: {', '.join(missing)}"})
        slug = str(p.get("show_id") or "")
        if slug and (not SLUG_RE.match(slug) or len(slug) > 40):
            failures.append({"id": pid,
                             "reason": f"bad show_id slug {slug!r}"})
        elif slug:
            if slug in seen_slugs:
                failures.append({"id": pid,
                                 "reason": f"duplicate show_id {slug!r} "
                                           f"(also {seen_slugs[slug]})"})
            else:
                seen_slugs[slug] = pid
        norm = _norm(p.get("title"))
        if norm:
            if norm in seen_titles:
                failures.append({"id": pid,
                                 "reason": f"duplicate title (also "
                                           f"{seen_titles[norm]})"})
            else:
                for other_norm, other_id in seen_titles.items():
                    a, b = _tokens(norm), _tokens(other_norm)
                    if not a or not b:
                        continue
                    sim = len(a & b) / len(a | b)
                    if sim >= 0.8:
                        failures.append(
                            {"id": pid,
                             "reason": f"title too similar to {other_id} "
                                       f"(overlap {sim:.0%}); rewrite the angle"})
                        break
                else:
                    seen_titles[norm] = pid
                    continue
                seen_titles.setdefault(norm, pid)
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", required=True)
    args = ap.parse_args()
    path = ROOT / "shows" / args.show / "ideas.json"
    if not path.exists():
        print(json.dumps({"ok": False, "failures": [
            {"id": "", "reason": "ideas.json does not exist yet"}]}))
        return 1
    try:
        archive = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(json.dumps({"ok": False, "failures": [
            {"id": "", "reason": f"ideas.json is not valid JSON: {exc}"}]}))
        return 1
    failures = check(archive)
    print(json.dumps({"ok": not failures, "failures": failures}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
