"""Character/entity NAME plausibility — a dependency-free leaf module.

The writers' room sometimes emits plotline PROSE where a name belongs
("Harrow Wing's four duel frames in the televised drop match"). Any string that
fails this gate must never become a character sheet, ref, or cast entry. Kept in
its own module so bootstrap/casting/growth can all use it without import cycles.
"""
from __future__ import annotations

import re

_MAX_NAME_LEN = 48
_MAX_NAME_WORDS = 6
_SENTENCE_PUNCT = re.compile(r"[.,;:!?\u2013\u2014\u2026\u00ab\u00bb\"]")


def looks_like_character_name(name: str) -> bool:
    """True for a plausible character/entity NAME, False for a sentence fragment.

    Names are short labels: "Jane Doe", "Unit-734", "Marauder Pods (x6)". Rejects
    long multi-word prose and any sentence punctuation (``.``,
    ``,``, ``;``, ``:``, ``!``, ``?``, dash-as-break, ellipsis) so plotline
    summaries and object descriptions can't be promoted into canon characters.
    """
    s = (name or "").strip()
    if not s or len(s) > _MAX_NAME_LEN:
        return False
    if _SENTENCE_PUNCT.search(s):
        return False
    if "'s " in s.lower():          # possessive prose, e.g. "Harrow Wing's ..."
        return False
    return len(s.split()) <= _MAX_NAME_WORDS
