"""The character-NAME plausibility gate (studio/names.py)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.names import looks_like_character_name as ok


def test_real_names_pass():
    for n in ["Mira Kaine", "Test-734", "Test Mech Brute", "Ren Tojo",
              "Chitinous Marauder Pods (x6)", "O'Brien", "Harrow Wing", "Harrow-Wing"]:
        assert ok(n), n


def test_plotline_prose_is_rejected():
    for n in ["Harrow Wing's four duel frames in the televised drop match",
              "Harrow Wing's four live combat frames shutting out Vixen Flight on broadcast",
              "a name, with a comma",
              "one; two",
              "what?",
              "a name that is far too long to ever be a plausible character or entity label",
              "", "   "]:
        assert not ok(n), n
