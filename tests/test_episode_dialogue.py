"""Tests for the prose-first dialogue spine: dialogue is written inside the
episode prose screenplay and distributed verbatim into shots.

These exercise the deterministic parts (no LLM): verbatim injection into shots,
on-camera resolution, and the dialogue-aware pass chain.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import prompts
from studio.config import Config
from studio.planner import (_SCENE_PASSES, _finalize_on_camera, _inject_dialogue,
                            _scene_passes, generate_scene_shots,
                            merge_nickname_ledger, parse_nickname_ledger)
from studio.show import Show


def test_inject_dialogue_places_lines_verbatim():
    spine = [{"char": "A", "line": "one"},
             {"char": "B", "line": "two"},
             {"char": "A", "line": "three"}]
    draft = [
        {"id": "s01_sh01", "lines": [0]},
        {"id": "s01_sh02", "lines": []},
        {"id": "s01_sh03", "lines": [1, 2]},
    ]
    _inject_dialogue(draft, spine)
    assert draft[0]["dialogue"] == [{"char": "A", "line": "one"}]
    assert draft[0]["silence"] is False
    assert draft[1]["dialogue"] == [] and draft[1]["silence"] is True
    assert [d["line"] for d in draft[2]["dialogue"]] == ["two", "three"]
    assert "lines" not in draft[0]                       # index field consumed


def test_inject_dialogue_keeps_unplaced_lines():
    spine = [{"char": "A", "line": "one"}, {"char": "B", "line": "two"}]
    draft = [{"id": "s01_sh01", "lines": [0]}, {"id": "s01_sh02"}]
    _inject_dialogue(draft, spine)
    # the line the blocker never placed is appended, not lost
    assert [d["line"] for d in draft[-1]["dialogue"]] == ["two"]


def test_finalize_on_camera_uses_screen_presence():
    draft = [{"id": "x",
              "references": {"characters": ["A"]},
              "dialogue": [{"char": "A", "line": "hi"},
                           {"char": "B", "line": "off"},
                           {"char": "A", "line": "vo", "on_camera": False}]}]
    _finalize_on_camera(draft)
    dl = draft[0]["dialogue"]
    assert dl[0]["on_camera"] is True        # on screen, unset -> True
    assert dl[1]["on_camera"] is False       # not on screen -> voiceover
    assert dl[2]["on_camera"] is False       # explicit V.O. preserved


def test_scene_passes_with_spine_is_dialogue_aware():
    with_spine = _scene_passes(True)
    names = [p["name"] for p in with_spine]
    assert "dialogue" not in names                        # writer pass dropped
    assert "lines" in with_spine[0]["schema"]["shots"][0]
    # without a spine the legacy chain is unchanged
    assert _scene_passes(False) == _SCENE_PASSES


def test_scene_pass_prompt_carries_numbered_spine():
    blocking = _scene_passes(True)[0]
    p = prompts.scene_pass_prompt(
        blocking, {"title": "Demo"}, "ep summary", {"id": "s01"},
        [{"name": "Ryou", "personality": ["quiet"]}], None, 110,
        dialogue_spine=[{"char": "Ryou", "line": "Hold the line."}])
    assert "DIALOGUE SPINE" in p
    assert "0: Ryou: Hold the line." in p
    assert "never reword" in p


def _show(tmp_path) -> Show:
    for sub in ("characters", "voices", "scenes", "runs", "continuity"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    show = Show("demo", root=tmp_path)
    show.write_character({"id": "ryou", "name": "Ryou", "appearance_canonical": "x",
                          "personality": ["quiet"]})
    show.write_character({"id": "mira", "name": "Mira Kaine", "appearance_canonical": "y",
                          "personality": ["dry"]})
    show.write_bible({"title": "Demo", "content_policy": "mature", "runtime_target_s": 60,
                      "scenes": []})
    return show


def test_parse_nickname_ledger():
    text = ("VOICE: Mira - dry.\n"
            "NICKNAMES:\n"
            "Mira Kaine -> Ren Tojo: property, lucky charm\n"
            "Sable -> Ren Tojo: rookie\n"
            "=== s01 ===\n"
            "MIRA: Hey, property.\n")
    led = parse_nickname_ledger(text, ["Ren Tojo", "Mira Kaine", "Sable Voss"])
    assert led["Mira Kaine"]["Ren Tojo"] == ["property", "lucky charm"]
    assert led["Sable Voss"]["Ren Tojo"] == ["rookie"]   # first-name resolved


def test_merge_nickname_ledger_locks_established_pairs():
    established = {"Mira Kaine": {"Ren Tojo": ["property"]}}
    new = {"Mira Kaine": {"Ren Tojo": ["property", "babe"],   # established -> locked
                          "Sable Voss": ["rookie"]}}          # new pair -> added
    m = merge_nickname_ledger(established, new)
    assert m["Mira Kaine"]["Ren Tojo"] == ["property"]        # not extended
    assert m["Mira Kaine"]["Sable Voss"] == ["rookie"]


def test_prose_prompt_carries_established_nicknames():
    p = prompts.episode_prose_prompt(
        {"title": "Demo"}, [],
        [{"name": "Mira Kaine", "personality": ["dry"]}],
        {"nicknames": {"Mira Kaine": {"Ren Tojo": ["property"]}}}, 1320, [], "")
    assert "NICKNAMES" in p
    assert "Mira Kaine -> Ren Tojo: property" in p
    assert "FIXED" in p


def test_voice_review_prompt_flags_nickname_drift():
    p = prompts.voice_review_prompt(
        {"scenes": []}, [{"name": "Mira Kaine", "personality": ["dry"]}],
        {"Mira Kaine": {"Ren Tojo": ["property"]}})
    assert "nickname" in p.lower()
    assert "Mira Kaine calls Ren Tojo: property" in p


class _ShotLLM:
    """Returns one shot per pass; blocking places spine line 0 in shot 1."""

    def chat_json(self, messages, **kwargs):
        user = messages[-1]["content"]
        if "SCENE BLOCKER" in user:
            return {"shots": [
                {"id": "s01_sh01", "type": "ref2va", "importance": "standard",
                 "duration_s": 10.125, "beat": "Ren speaks", "lines": [0]},
                {"id": "s01_sh02", "type": "fl2va", "importance": "standard",
                 "duration_s": 5.167, "beat": "silent insert", "lines": []},
            ]}
        if "CONTINUITY MANAGER" in user:
            return {"shots": [
                {"id": "s01_sh01", "references": {"characters": ["Ryou"], "scene": "dojo"}},
                {"id": "s01_sh02", "references": {"characters": [], "scene": "dojo"}},
            ]}
        return {"shots": [{"id": "s01_sh01"}, {"id": "s01_sh02"}]}


def test_prose_review_loop_persists_ledger_and_dialogue_txt(tmp_path):
    from studio.prose import write_prose_review_loop
    show = _show(tmp_path)
    prose = ("=== s01 | Dojo | Ryou, Mira Kaine ===\n"
             "Morning light. Ryou stretches.\n\n"
             "RYOU: Hold the line.\n"
             "MIRA: Make me, kid.\n\n"
             "NICKNAMES:\n"
             "Ryou -> Mira Kaine: kid\n")

    class _PassLLM:
        def chat_json(self, messages, **kwargs):
            return {"grade": "Pass", "review": ""}

    _, reviews = write_prose_review_loop(show, 1, prose, llm=_PassLLM(),
                                         cfg=Config(tmp_path), max_revisions=1)
    from studio.review import all_pass
    assert all_pass(reviews)
    # the nickname ledger is persisted to continuity for later episodes
    assert show.read_continuity()["nicknames"]["Ryou"]["Mira Kaine"] == ["kid"]


def test_generate_scene_shots_uses_spine_verbatim(tmp_path):
    show = _show(tmp_path)
    spine = [{"char": "Ryou", "line": "Hold the line."}]
    scene = generate_scene_shots(
        show, 1, {"id": "s01", "location": "dojo", "summary": "x"},
        llm=_ShotLLM(), cfg=Config(tmp_path), dialogue_spine=spine)
    shots = scene["shots"]
    assert shots[0]["dialogue"] == [{"char": "Ryou", "line": "Hold the line.",
                                     "on_camera": True}]
    assert shots[0]["silence"] is False
    assert shots[1]["dialogue"] == [] and shots[1]["silence"] is True
    # only the authored line appears anywhere — no invented filler
    assert all(d["line"] == "Hold the line."
               for sh in shots for d in sh["dialogue"])


def test_prose_prompt_shape():
    # The standalone whole-episode dialogue prompt is gone (single path: the
    # prose screenplay carries the dialogue). Its voice contracts live on in
    # dialogue_system (shot dialogue pass) and episode_prose_prompt.
    p = prompts.episode_prose_prompt(
        {"title": "Demo", "tone": ["witty"], "genre": ["sci-fi"]}, [],
        [{"name": "Ryou", "personality": ["quiet"]}],
        {"episode": 0}, 1320, [], "")
    assert "=== s01" in p
    assert "NO JSON" in p
    assert "NICKNAMES" in p


def test_dialogue_system_forbids_lyrical_prose():
    s = prompts.dialogue_system({"fanservice_baseline": "high-school-dxd"}, "mature")
    assert "Driven by WANT" in s
    assert "narration-style" in s
    assert "poster captions" in s
    assert "real person" in s


def test_scene_detail_prompt_demands_complete_sentences():
    p = prompts.scene_detail_prompt({"title": "Demo"}, {"id": "s01"}, [])
    assert "COMPLETE, natural sentence" in p
    assert "noun list" in p
    assert "Scoot up." in p
