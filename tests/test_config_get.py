"""Config.get must support both its 3-arg form and the dict.get default idiom."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.config import Config


def test_get_whole_section_and_dict_default_idiom(tmp_path):
    cfg = Config(tmp_path)
    # dict.get-style: non-string second arg is a DEFAULT, not a key.
    sec = cfg.get("show_profile", {})
    assert isinstance(sec, dict)
    assert sec.get("maturity") == "mature"
    # unknown section + dict default -> the default
    assert cfg.get("no_such_section", {}) == {}


def test_get_key_and_default_forms(tmp_path):
    cfg = Config(tmp_path)
    assert cfg.get("show_profile", "maturity") == "mature"
    assert cfg.get("show_profile", "maturity", "x") == "mature"
    assert cfg.get("show_profile", "nope", 7) == 7
    assert cfg.get("no_such_section", "nope", 7) == 7
