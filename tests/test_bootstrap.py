"""M1a tests: the Gate-0 bootstrap chain driven by a fake Showrunner LLM."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.bootstrap import BootstrapChain
from studio.clients.lmstudio import LMStudioClient
from studio.config import Config
from studio.show import Show


class FakeLLM:
    """Deterministic LM Studio stand-in keyed on the user prompt."""

    def __init__(self, script: dict):
        self.script = script

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "Propose an anime series concept" in user:
            return self.script.get("concept", {"title": "T", "logline": "L",
                                               "genre": ["martial-arts"],
                                               "tone": ["warm"], "maturity": "mature"})
        if "CAST MEMBER TO WRITE" in user:
            name = user.split("CAST MEMBER TO WRITE:")[1].splitlines()[0].strip()
            return {"name": name, "role": "protagonist",
                    "appearance_canonical": "a vivid description",
                    "personality": ["loyal"], "traits_for_llm": "terse",
                    "voice": {"mode": "designed", "voice_description": "warm low voice"}}
        if "series bible" in user:
            return self.script.get("bible", {
                "title": "Demo", "logline": "L", "genre": ["martial-arts"], "tone": ["warm"],
                "world": {"setting": "s", "rules": [], "established_facts": []},
                "arcs": [{"id": "a1", "name": "Arc", "beats_total": 6,
                          "beats": [{"id": "b1", "summary": "beat"}]}],
                "style_guide": "90s cel line art",
                "runtime_target_s": 1320,
                "cast": [{"name": "Ryou", "role": "blacksmith"},
                         {"name": "Akane", "role": "scholar"}],
            })
        if "INITIAL scene registry" in user:
            return {"locations": [{"id": "dojo", "name": "Dojo",
                                   "description": "x", "setting_prompt": "y"}]}
        return {}


def _make_chain(tmp_path) -> BootstrapChain:
    for sub in ("characters", "voices", "scenes"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    # Config rooted at tmp_path: isolates the TTS cache + any state per test.
    cfg = Config(tmp_path)
    show = Show("demo", root=tmp_path)
    show.write_concept({"title": "Demo"})
    return BootstrapChain(show, cfg=cfg, llm=FakeLLM({}), bus=None)


def test_bootstrap_auto_completes(tmp_path):
    chain = _make_chain(tmp_path)
    chain._auto_override = True
    log = chain.advance()
    state = chain.show.bootstrap_state()
    assert state["complete"] is True
    assert state["bible"]["status"] == "approved"
    assert state["scenes"]["status"] == "approved"
    assert chain.show.read_bible()["title"] == "Demo"
    # both cast members proposed + voices written
    chars = chain.show.list_characters()
    assert len(chars) == 2
    for cid in chars:
        assert chain.show.read_character(cid)["name"]
        assert chain.show.read_voice(f"{cid}_voice")["voice_description"]
        assert (chain.show.dir / "assets" / "voice" / f"{cid}_voice.wav").exists()
    assert (chain.show.scenes_dir / "dojo.yaml").exists()


def test_bootstrap_gated_stops_at_concept(tmp_path):
    chain = _make_chain(tmp_path)  # auto_override off; config approval.show default is gated
    log = chain.advance()
    state = chain.show.bootstrap_state()
    assert state["concept"]["status"] == "pending"
    assert not state["complete"]
    assert "concept: generated -> pending" in log


def test_bootstrap_reject_regenerates(tmp_path):
    chain = _make_chain(tmp_path)
    chain._auto_override = True
    chain.advance()
    # simulate a reject at bible: reset bible status -> next advance regenerates it
    st = chain.show.bootstrap_state()
    st["bible"] = {"status": ""}
    chain.show.set_bootstrap_state(st)
    chain.advance()
    assert chain.show.bootstrap_state()["bible"]["status"] == "approved"


class FlakyLMStudio(LMStudioClient):
    """chat() returns garbage once, then valid JSON - tests the retry path."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return "no json here"
        return '{"ok": true}'


def test_chat_json_retries_on_parse_failure():
    client = FlakyLMStudio()
    assert client.chat_json([{"role": "user", "content": "x"}], retries=3) == {"ok": True}
    assert client.calls == 2
