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


# ---------------------------------------------------------------------------
# Self-healing: resume an interrupted Gate-0 chain (crash / dashboard restart).
# ---------------------------------------------------------------------------

def _interrupted_show(tmp_path, names=("Ryou", "Akane")) -> BootstrapChain:
    """Build a show whose characters step was killed mid-character: the second
    cast member's proposal file exists on disk but was never registered in
    bootstrap.json (the exact failure from a crash between write + save)."""
    chain = _make_chain(tmp_path)
    chain._auto_override = True
    chain.advance()                                  # concept+bible+characters+scenes all complete
    st = chain.show.bootstrap_state()
    assert st["complete"] is True

    # Simulate a crash: first character fully approved, second character's
    # proposal written to disk but bootstrap.json rewritten WITHOUT it.
    first = names[0]
    st = {
        "concept": {"status": "approved"},
        "bible": {"status": "approved"},
        "characters": [
            {"name": first, "proposal": "approved", "refs": "approved", "voice": "approved"},
        ],
        "scenes": {"status": "approved"},
        "complete": False,
    }
    chain.show.set_bootstrap_state(st)
    # Drop Akane's refs + voice dirs so bootstrap_refs/voice regenerate.
    for cid in chain.show.list_characters():
        if chain.show.read_character(cid).get("name") != first:
            rd = chain.show.character_refs_dir(cid)
            for f in list(rd.glob("*")):
                f.unlink()
            vp = chain.show.dir / "assets" / "voice" / f"{cid}_voice.wav"
            if vp.exists():
                vp.unlink()
    return chain


def test_resume_picks_up_orphaned_character(tmp_path):
    chain = _interrupted_show(tmp_path)
    # The stalled show has a proposal file on disk with no bootstrap entry.
    st = chain.show.bootstrap_state()
    assert st["complete"] is False
    assert len(st["characters"]) == 1           # second character lost on crash
    assert len(chain.show.list_characters()) == 2   # but its proposal survived on disk

    # Re-running advance() must resume from disk state: register + generate
    # the orphaned character instead of blocking forever.
    chain._auto_override = True
    log = chain.advance()
    st = chain.show.bootstrap_state()
    assert st["complete"] is True
    assert len(st["characters"]) == 2
    names = {c["name"] for c in st["characters"]}
    assert {"Ryou", "Akane"} <= names
    for c in st["characters"]:
        assert c["proposal"] == "approved"
        assert c["voice"] == "approved"
    # the orphaned character got its full package regenerated
    akane = next(c for c in st["characters"] if c["name"] == "Akane")
    assert akane["refs"] in ("approved", "stub")


def test_needs_reconcile_detects_stall(tmp_path):
    from studio.bootstrap import needs_reconcile
    chain = _interrupted_show(tmp_path)
    # Incomplete + nothing pending + work stranded on disk -> needs reconcile.
    assert needs_reconcile("demo", show=chain.show) is True
    # Once a gate is pending, it is awaiting the human, not stalled.
    chain._auto_override = True
    chain.advance()
    st = chain.show.bootstrap_state()
    assert st["complete"] is True
    assert needs_reconcile("demo", show=chain.show) is False


def test_write_through_survives_mid_character(tmp_path):
    """After refs/voice write-through, a second advance() does NOT regenerate
    the already-generated asset from scratch (idempotent resume)."""
    from studio.bootstrap import BootstrapChain
    chain = _make_chain(tmp_path)
    chain._auto_override = True
    chain.advance()
    st = chain.show.bootstrap_state()
    assert st["complete"] is True
    # simulate crash right after the first character's refs were persisted
    st["characters"] = st["characters"][:1]
    st["complete"] = False
    chain.show.set_bootstrap_state(st)
    # a reconcile pass resumes without error and completes
    log = chain.advance()
    st2 = chain.show.bootstrap_state()
    assert st2["complete"] is True
