"""Tests for the mature-service spec: floor/ceiling + varied reward scheduler."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import service


def _bible(**ms):
    return {"content_policy": "explicit", "mature_spec": ms}


def test_is_mature_and_level():
    assert service.is_mature(_bible(quotient="explicit_ecchi"))
    assert service.is_mature({"content_policy": "explicit"})
    assert not service.is_mature({"content_policy": "safe"})
    assert service.explicit_level(_bible(quotient="explicit_ecchi")) == "explicit_ecchi"
    assert service.explicit_level({"content_policy": "xxx"}) == "explicit"


def test_floor_reads_new_spec_then_legacy_quota():
    b = _bible(per_episode_floor={"panty_shots": 2, "bare_chest_reveals": 1})
    fl = service.floor(b)
    assert fl["panty_shots"] == 2 and fl["bare_chest_reveals"] == 1
    # Legacy quota still yields a service-scene floor.
    b2 = _bible(quotas={"service_scenes_per_episode": [3, 5]})
    assert service.floor(b2)["service_scenes"] == 3


def test_ceiling_from_spec_and_legacy():
    c = service.ceiling(_bible(explicit_sex_scenes={"per_arc": 2}))
    assert c["per_arc"] == 2 and c["enabled"] is True
    c2 = service.ceiling(_bible(quotas={"reward_scenes_per_arc": 1}))
    assert c2["per_arc"] == 1


def test_reward_episodes_count_range_and_spread():
    picks = service.reward_episodes("sync-break", 1, 12, 2)
    assert len(picks) == 2
    assert all(1 <= p <= 12 for p in picks)
    assert picks == sorted(picks) and len(set(picks)) == len(picks)
    # One per equal band (spread), not two adjacent.
    assert picks[0] <= 6 and picks[1] >= 7


def test_reward_episodes_vary_across_seasons_but_are_deterministic():
    # Same inputs reproduce (stable season plan)...
    assert service.reward_episodes("sync-break", 3, 12, 2) == \
        service.reward_episodes("sync-break", 3, 12, 2)
    # ...but different seasons do NOT all reuse the same slot.
    sets = {tuple(service.reward_episodes("sync-break", s, 12, 2))
            for s in range(1, 7)}
    assert len(sets) > 1


def test_reward_episodes_zero_and_clamped():
    assert service.reward_episodes("s", 1, 12, 0) == []
    assert len(service.reward_episodes("s", 1, 12, 99)) == 12


def test_ledger_and_shortfalls():
    scenes = [
        {"service": {"panty_shots": 1}},
        {"shots": [{"service": {"bare_chest_reveals": 1}},
                   {"service": {"panty_shots": 1}}]},
        {"summary": "no service"},
    ]
    led = service.service_ledger(scenes)
    assert led["panty_shots"] == 2 and led["bare_chest_reveals"] == 1
    assert led["service_scenes"] == 2
    b = _bible(per_episode_floor={"panty_shots": 3, "bare_chest_reveals": 1},
               explicit_sex_scenes={"per_arc": 1})
    short = service.ledger_shortfalls(b, led, reward=True)
    assert any("panty shots" in s for s in short)
    assert any("explicit" in s for s in short)
    # A cleared ledger returns nothing.
    assert service.ledger_shortfalls(_bible(per_episode_floor={"panty_shots": 1}),
                                     led) == []


def test_ceiling_per_arc_range_yields_varying_count():
    b = _bible(explicit_sex_scenes={"per_arc": [2, 4]})
    c = service.ceiling(b)
    assert c["per_arc_min"] == 2 and c["per_arc_max"] == 4
    counts = {service.reward_count("sync-break", s, b) for s in range(1, 9)}
    assert counts and all(2 <= n <= 4 for n in counts)
    assert len(counts) > 1                       # the NUMBER varies per season
    # A single int still behaves exactly.
    assert service.reward_count("x", 1, _bible(explicit_sex_scenes={"per_arc": 3})) == 3
