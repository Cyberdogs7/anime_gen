"""`studio.py verify` - health-check every backend + the board store.

Reports each check as ok / warn / fail without raising; a failed hard check
makes the command exit non-zero.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from .clients import ComfyClient, LMStudioClient, NullTTS, ffmpeg_version
from .config import get_config
from .db import DB


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = False


def run_checks(cfg=None) -> list[Check]:
    cfg = cfg or get_config()
    checks: list[Check] = []

    # 1. Config
    try:
        _ = cfg["pipeline"]["mode"]
        checks.append(Check("config", True, f"loaded {len(cfg.data)} sections"))
    except Exception as exc:
        checks.append(Check("config", False, str(exc), required=True))

    # 2. DB schema
    try:
        db = DB(cfg.db_path)
        tables = {r[0] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        need = {"runs", "stages", "shots", "assets", "events", "continuity",
                "qc_results", "script_reviews", "policy_events"}
        missing = need - tables
        checks.append(Check("db", not missing,
                            f"schema ready ({len(tables)} tables)" + (f"; missing {missing}" if missing else ""),
                            required=True))
        db.close()
    except Exception as exc:
        checks.append(Check("db", False, str(exc), required=True))

    # 3. Board store
    try:
        from .board.store import BoardSet
        store = BoardSet(cfg)
        cards = store.all()
        checks.append(Check("board", True, f"{len(cards)} card(s)"))
        store.close()
    except Exception as exc:
        checks.append(Check("board", False, str(exc), required=True))

    # 4. ComfyUI (both nodes)
    for node, nc in cfg["comfy"]["nodes"].items():
        client = ComfyClient(nc["url"], nc.get("api_key"))
        ok = client.health()
        checks.append(Check(f"comfyui:{node}", ok,
                            nc["url"] if ok else "unreachable"))

    # 5. LM Studio
    llm = cfg["llm"]
    client = LMStudioClient(llm["base_url"])
    ok = client.health()
    checks.append(Check("lmstudio", ok,
                        f"{llm['base_url']} ({llm.get('model')})" if ok else "unreachable"))

    # 6. TTS (null adapter always works; real engine reports at M1)
    tts = NullTTS()
    checks.append(Check("tts", tts.health() is not False, "null adapter (real engines at M1)"))

    # 7. ffmpeg
    version = ffmpeg_version()
    checks.append(Check("ffmpeg", version is not None,
                        f"v{version}" if version else "not found on PATH", required=True))

    # 8. Shows
    shows = cfg.list_shows()
    checks.append(Check("shows", True, f"{len(shows)} show(s): {', '.join(shows) or 'none'}"))

    return checks


def run_verify(cfg=None) -> int:
    checks = run_checks(cfg)
    failed = 0
    for c in checks:
        mark = "[ OK ]" if c.ok else "[FAIL]"
        print(f"{mark} {c.name:18s} {c.detail}")
        if not c.ok and c.required:
            failed += 1
    if failed:
        print(f"\n{failed} required check(s) failed.")
        return 1
    print("\nAll required checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run_verify())
