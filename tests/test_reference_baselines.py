"""Style references (baseline / fanservice_baseline) are PER-SHOW facts.

They live in each show's bible.yaml and are resolved by ``Show.style_profile``;
no show title is baked into the config defaults or the prompt builders.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import prompts
from studio.config import Config
from studio.show import Show


def _show_with_bible(tmp_path, bible):
    (tmp_path / "characters").mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=tmp_path)
    show.write_bible(bible)
    return show


def test_config_no_longer_ships_a_show_baseline(tmp_path):
    # A different show's title must never leak in as a global default.
    cfg = Config(tmp_path)
    assert cfg.get("show_profile", "baseline", "") == ""
    assert cfg.get("show_profile", "fanservice_baseline", "") == ""


def test_style_profile_reads_baseline_from_the_show(tmp_path):
    show = _show_with_bible(tmp_path, {"title": "Demo",
                                       "baseline": "some-anime",
                                       "fanservice_baseline": "other-anime"})
    prof = show.style_profile(Config(tmp_path))
    assert prof.get("baseline") == "some-anime"
    assert prof.get("fanservice_baseline") == "other-anime"


def test_style_profile_reads_skeleton_series_block(tmp_path):
    show = _show_with_bible(tmp_path, {"series": {
        "title": "Demo", "quality_baseline": "some-anime"}})
    assert show.style_profile(Config(tmp_path)).get("baseline") == "some-anime"


def test_style_profile_empty_when_show_declares_none(tmp_path):
    show = _show_with_bible(tmp_path, {"title": "Demo"})
    prof = show.style_profile(Config(tmp_path))
    assert not prof.get("baseline")
    assert not prof.get("fanservice_baseline")


def test_showrunner_system_uses_the_shows_baseline():
    text = prompts.showrunner_system({"fanservice_baseline": "other-anime"},
                                     "mature", "some-anime")
    assert "some-anime" in text
    # The showrunner prompt carries the production baseline; the fan-service bar
    # is authored per stage (dialogue / reviewer) rather than duplicated here.
    assert "other-anime" not in text


def test_showrunner_system_neutral_without_references():
    text = prompts.showrunner_system({}, "mature", "")
    assert prompts.NEUTRAL_BASELINE in text
    # No show title leaks in as a code default.
    assert "ranma" not in text.lower()
    assert "dxd" not in text.lower()


def test_fanservice_reviewer_uses_provided_or_neutral_bar():
    assert "some-anime" in prompts.fanservice_review_prompt({}, {}, "some-anime")
    assert prompts.NEUTRAL_MATURE in prompts.fanservice_review_prompt({}, {})
