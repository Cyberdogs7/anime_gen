"""Content-policy judge hook. See DESIGN.md §13.

The judge is a local LLM that enforces the hard floor (what must never appear).
This is a stub at M0: when no judge model is configured, artifacts pass with a
note; wiring the real judge call is part of M1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BLOCKLIST_DEFAULT = [
    "real identifiable persons",
    "minors",
    "non-consensual framing",
    "real brands/trademarks in keyframes",
]


@dataclass
class Verdict:
    artifact_hash: str
    verdict: str                 # pass | block
    reason: str = ""
    stage: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


class PolicyJudge:
    def __init__(self, client=None, maturity: str = "mature", blocklist: list[str] | None = None):
        self.client = client     # LMStudioClient or None (stub)
        self.maturity = maturity
        self.blocklist = blocklist or BLOCKLIST_DEFAULT

    def scan(self, artifacts: list[str], stage: int = 0, run_id: int = 0,
             show_id: str = "") -> list[Verdict]:
        """Scan text artifacts. Returns one Verdict per artifact.

        Hard-block keywords (minors, real-person names, etc.) are caught by a
        cheap local matcher even when no judge LLM is configured.
        """
        import hashlib
        from . import db as dbmod

        verdicts: list[Verdict] = []
        db: dbmod.DB | None = None
        hard_blocks = {
            "minor", "child", "underage", "teen", "adolescent", "kid",
            "real person", "celebrity", "non-consensual", "real brand",
        }
        for text in artifacts:
            h = hashlib.sha256(text.encode()).hexdigest()
            low = text.lower()
            blocked = [b for b in hard_blocks if b in low]
            if blocked:
                v = Verdict(h, "block", reason=f"hard-block terms matched: {blocked}", stage=stage)
            else:
                v = Verdict(h, "pass", reason="no hard-block match (M0 stub; LLM judge at M1)", stage=stage)
            verdicts.append(v)
        # Persist (best-effort; no DB at unit-test time)
        try:
            from .config import get_config
            db = dbmod.open_db(get_config().db_path)
        except Exception:
            db = None
        if db is not None:
            for v in verdicts:
                db.log_policy(run_id, show_id, stage, v.artifact_hash, v.verdict, v.reason)
            db.close()
        return verdicts
