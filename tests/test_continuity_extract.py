"""The continuity extractor: durable facts from a finished episode are distilled
into the (previously idle) continuity state, idempotently and per-episode.

No LLM: the extraction call is faked; the merge/purge/parse logic is deterministic.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import prompts
from studio.config import Config
from studio.planner import (extract_continuity, purge_episode_continuity)
from studio.show import Show


def _show(tmp_path) -> Show:
    for sub in ("characters", "voices", "scenes", "runs", "continuity"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=tmp_path)
    show.write_character({"id": "ryou", "name": "Ryou", "appearance_canonical": "x",
                          "personality": ["quiet"]})
    show.write_bible({"title": "Demo", "content_policy": "mature", "runtime_target_s": 60,
                      "scenes": []})
    return show


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat_json(self, messages, **kwargs):
        self.calls += 1
        return self.payload


def test_extract_continuity_populates_state(tmp_path):
    show = _show(tmp_path)
    payload = {
        "world_facts": ["Salvage law: restarting a dead Frame transfers the co-pilot "
                        "contract to that Frame's owner."],
        "character_notes": {"Ryou": ["Legally bound to Mira Kaine's flight."]},
        "unresolved_threads": ["Who built Ryou's nape implant?"],
        "continuity_rules": ["A hot sync burns out nerves if held too long."],
    }
    state = extract_continuity(show, 1, {"scenes": []}, llm=_FakeLLM(payload),
                               cfg=Config(tmp_path))
    assert state["world"]["facts"][0]["text"].startswith("Salvage law")
    assert state["world"]["facts"][0]["episode"] == 1
    assert state["characters"]["Ryou"]["notes"][0]["episode"] == 1
    assert state["unresolved_threads"][0]["text"] == "Who built Ryou's nape implant?"
    assert state["continuity_rules"][0]["episode"] == 1


def test_extract_continuity_is_idempotent_and_replaces(tmp_path):
    show = _show(tmp_path)
    llm = _FakeLLM({"world_facts": ["Fact A."], "unresolved_threads": ["Thread A."]})
    extract_continuity(show, 1, {"scenes": []}, llm=llm, cfg=Config(tmp_path))
    extract_continuity(show, 1, {"scenes": []}, llm=llm, cfg=Config(tmp_path))
    st = show.read_continuity()
    assert len(st["world"]["facts"]) == 1          # re-run did not duplicate
    assert len(st["unresolved_threads"]) == 1
    extract_continuity(show, 1, {"scenes": []},
                       llm=_FakeLLM({"world_facts": ["Fact B."]}), cfg=Config(tmp_path))
    st = show.read_continuity()
    assert [f["text"] for f in st["world"]["facts"]] == ["Fact B."]   # replaced


def test_extract_continuity_keeps_other_episodes(tmp_path):
    show = _show(tmp_path)
    extract_continuity(show, 1, {"scenes": []},
                       llm=_FakeLLM({"world_facts": ["From EP1."]}), cfg=Config(tmp_path))
    extract_continuity(show, 2, {"scenes": []},
                       llm=_FakeLLM({"world_facts": ["From EP2."]}), cfg=Config(tmp_path))
    texts = [f["text"] for f in show.read_continuity()["world"]["facts"]]
    assert texts == ["From EP1.", "From EP2."]


def test_purge_episode_continuity_keeps_other_episodes_and_legacy_strings():
    st = {"world": {"facts": [{"text": "e1", "episode": 1},
                              {"text": "e2", "episode": 2}]},
          "characters": {"A": {"notes": [{"text": "e1", "episode": 1},
                                         {"text": "e2", "episode": 2}]}},
          "unresolved_threads": [{"text": "e1", "episode": 1}, "legacy string"],
          "continuity_rules": [{"text": "e1", "episode": 1},
                               {"text": "e2", "episode": 2}]}
    purge_episode_continuity(st, 1)
    assert [f["text"] for f in st["world"]["facts"]] == ["e2"]
    assert [n["text"] for n in st["characters"]["A"]["notes"]] == ["e2"]
    assert st["unresolved_threads"] == ["legacy string"]      # legacy non-dict kept
    assert [r["text"] for r in st["continuity_rules"]] == ["e2"]


def test_continuity_extract_prompt_shape():
    p = prompts.continuity_extract_prompt(
        {"title": "Demo"}, 1, [{"id": "s01", "dialogue": ["Ryou: Hold the line."]}],
        {"world": {}}, [{"name": "Ryou"}])
    for key in ("world_facts", "character_notes", "unresolved_threads",
                "continuity_rules"):
        assert key in p
    assert "do not repeat" in p
