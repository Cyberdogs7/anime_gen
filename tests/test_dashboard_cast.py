"""Tests for the dashboard 'episode cast' ref cards.

Regression guard: the 'action needed - approve reference images' banner lists
EVERY pending ref (show-global), so the cast card must render a matching approve
control for each one - including episode-supporting characters when the episode
has no script yet.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.dashboard import episode_cast
from studio.show import Show


def _show(tmp_path) -> Show:
    d = tmp_path / "shows" / "demo"
    (d / "characters").mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=d)
    show.write_bible({"title": "Demo", "content_policy": "mature"})
    show.set_bootstrap_state({"complete": True,
                              "characters": [{"name": "Ren", "refs": "approved"}]})
    _char_with_ref(show, "ren", "Ren")
    _char_with_ref(show, "harrow-wing", "Harrow Wing", role="episode supporting")
    return show


def _char_with_ref(show: Show, cid: str, name: str, role: str = "") -> None:
    show.write_character({"id": cid, "name": name, "role": role,
                          "appearance_canonical": "x"})
    rd = show.character_refs_dir(cid)
    rd.mkdir(parents=True, exist_ok=True)
    ref = f"{cid}_ref_01.png"
    (rd / ref).write_bytes(b"png")
    (rd / "refs.json").write_text(json.dumps(
        {"status": "real", "refs": [ref], "variants": {"base": ref}}),
        encoding="utf-8")


def test_pending_supporting_ref_has_a_card_without_script(tmp_path):
    show = _show(tmp_path)
    cast = episode_cast(show, None)
    names = [c["name"] for c in cast]
    # The episode-supporting pending ref is surfaced so it can be approved...
    assert "Harrow Wing" in names
    # ...but the approved bootstrap char isn't dragged in when it's not in the cast.
    assert "Ren" not in names
    hw = next(c for c in cast if c["name"] == "Harrow Wing")
    assert hw["variants"] and hw["variants"][0]["approved"] is False


def test_script_cast_still_leads_and_pending_is_appended(tmp_path):
    show = _show(tmp_path)
    cast = episode_cast(show, {"cast": ["Ren"]})
    names = [c["name"] for c in cast]
    assert names[0] == "Ren"
    assert "Harrow Wing" in names
    assert names.count("Harrow Wing") == 1


def test_approved_supporting_ref_drops_out_when_not_in_cast(tmp_path):
    from studio.storyboard import mark_base_ref_approved
    show = _show(tmp_path)
    mark_base_ref_approved(show, "harrow-wing")
    assert episode_cast(show, None) == []
