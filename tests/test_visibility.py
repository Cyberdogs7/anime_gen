"""Tests for maximum visibility: no truncation of user-facing outputs."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import studio.config as _cfg
from studio.show import Show


def _make_show(tmp_path, monkeypatch) -> Show:
    # Point the process-wide config at tmp_path so Show("demo") inside
    # dashboard/CLI resolves to the same dir the test writes.
    monkeypatch.setattr(_cfg, "_default", _cfg.Config(tmp_path))
    show = Show("demo")
    for sub in ("characters", "voices", "scenes"):
        (show.dir / sub).mkdir(parents=True, exist_ok=True)
    show.write_character({"id": "ryou", "name": "Ryou", "appearance_canonical": "x"})
    show.write_bible({"title": "Demo", "content_policy": "mature",
                      "runtime_target_s": 30,
                      "mature_spec": {"quotient": "ecchi", "quotas": {},
                                      "escalation": {}, "tone_boundaries": [],
                                      "characters": {}, "scene_types": []}})
    return show


def test_reject_stores_every_note_structured(tmp_path, monkeypatch):
    from studio.planner import read_script_rejection, reject_script_review
    show = _make_show(tmp_path, monkeypatch)
    reviews = {f"r{i}": {"pass": False, "notes": [
        {"severity": "HIGH", "scene": "s01", "note": f"blocking note {i}-{k}"}
        for k in range(5)]} for i in range(3)}
    reject_script_review(show, 1, reviews)
    rej = read_script_rejection(show, 1)
    # All 15 notes land in the regen string (was [:3] per reviewer = 9 lost).
    assert rej["notes"].count("blocking note") == 15
    # And the full structured set persists for debugging.
    assert len(rej["notes_full"]) == 15
    assert all("reviewer" in n for n in rej["notes_full"])


def test_episode_payload_carries_full_story_text(tmp_path, monkeypatch):
    from studio.dashboard import episode_payload
    show = _make_show(tmp_path, monkeypatch)
    d = show.dir / "runs" / "EP01"
    d.mkdir(parents=True, exist_ok=True)
    (d / "episode_prose.txt").write_text("=== s01 | Dojo | Ryou ===\n" + "x" * 5000,
                                         encoding="utf-8")
    (d / "dialogue.txt").write_text("RYOU: " + "y" * 5000, encoding="utf-8")
    (d / "plan.json").write_text(json.dumps({"episode": 1, "status": "approved",
                                             "scenes": []}), encoding="utf-8")
    (d / "scene_details.json").write_text(json.dumps({"status": "approved",
                                                      "scenes": []}), encoding="utf-8")
    payload = episode_payload("demo", "EP01")
    # Full text, byte-for-byte — never truncated for display.
    assert payload["episode_prose"].endswith("x" * 100)
    assert len(payload["episode_prose"]) > 5000
    assert payload["dialogue_txt"].endswith("y" * 100)


def test_dump_command_prints_everything(tmp_path, monkeypatch, capsys):
    import argparse
    from studio.cli import _cmd_dump
    show = _make_show(tmp_path, monkeypatch)
    d = show.dir / "runs" / "EP01"
    d.mkdir(parents=True, exist_ok=True)
    (d / "episode_prose.txt").write_text("=== s01 | Dojo | Ryou ===\nFull prose here.",
                                         encoding="utf-8")
    (d / "reviews").mkdir(parents=True, exist_ok=True)
    (d / "reviews" / "slop.r1.json").write_text(json.dumps(
        {"round": 1, "pass": False, "score": 0.9,
         "notes": [{"severity": "HIGH", "scene": "s01",
                    "note": "the complete untruncated reviewer sentence"}]}),
        encoding="utf-8")
    rc = _cmd_dump(argparse.Namespace(show="demo", episode=1))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Full prose here." in out
    assert "the complete untruncated reviewer sentence" in out


def test_dashboard_file_paths_have_no_double_ep_prefix():
    # Regression guard: d.episode is the full label ("EP01"), so building
    # `runs/EP${d.episode}/...` yields "runs/EPEP01/..." and the file endpoint
    # answers "file not found or outside show dir". All file buttons must go
    # through epRel().
    html = (ROOT / "studio" / "dashboard.html").read_text(encoding="utf-8")
    assert "runs/EP${" not in html
    assert "function epRel(d, rest)" in html


def test_delete_episode_is_idempotent_and_purges_continuity(tmp_path, monkeypatch):
    # The delete button must never error on an already-gone episode (e.g. the
    # folder was removed by hand), and it must purge the episode's continuity
    # facts so the regen starts clean instead of inheriting rejected canon.
    from studio.episode_repair import delete_episode
    show = _make_show(tmp_path, monkeypatch)
    d = show.dir / "runs" / "EP01"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.json").write_text("{}", encoding="utf-8")
    show.write_continuity({"episode": 0,
                           "world": {"facts": [{"text": "bad rejected canon",
                                                "episode": 1}]},
                           "characters": {}})

    msgs = delete_episode("demo", 1)
    assert not d.exists()
    assert any("deleted" in m for m in msgs)
    assert any("continuity" in m for m in msgs)

    # Second delete (and the "EP01" label form) must succeed, not 500.
    msgs2 = delete_episode("demo", "EP01")
    assert any("already gone" in m for m in msgs2)

    import json as _json
    state = _json.loads(show.continuity_path.read_text(encoding="utf-8"))
    assert state.get("world", {}).get("facts", []) == []


def test_promptlog_captures_full_input(tmp_path, monkeypatch):
    from studio import promptlog
    show = _make_show(tmp_path, monkeypatch)
    system = "SYSTEM " + "s" * 3000
    user = "USER " + "u" * 5000

    class FakeLLM:
        def __init__(self):
            self.kwargs = None
        def chat_json(self, messages, **kwargs):
            self.kwargs = kwargs
            assert messages[0]["content"] == system
            return {"ok": True}

    llm = FakeLLM()
    out = promptlog.logged_chat_json(llm, show, 1, "blocking-s01",
                                     [{"role": "system", "content": system},
                                      {"role": "user", "content": user}],
                                     model="m", temperature=0.8, role="showrunner")
    assert out == {"ok": True}
    assert llm.kwargs["role"] == "showrunner"  # kwargs pass through untouched
    logged = json.loads((show.dir / "runs" / "EP01" / "prompts"
                         / "blocking-s01.json").read_text(encoding="utf-8"))
    # Full prompt text persisted byte-for-byte — the director reviews inputs.
    assert logged["system"] == system
    assert logged["user"] == user
    assert logged["model"] == "m" and logged["temperature"] == 0.8


def test_episode_payload_lists_every_file(tmp_path, monkeypatch):
    from studio.dashboard import episode_payload
    show = _make_show(tmp_path, monkeypatch)
    d = show.dir / "runs" / "EP01"
    (d / "prompts").mkdir(parents=True, exist_ok=True)
    (d / "episode_prose.txt").write_text("prose", encoding="utf-8")
    (d / "prompts" / "blocking-s01.json").write_text("{}", encoding="utf-8")
    (d / "plan.json").write_text(json.dumps({"episode": 1, "status": "approved",
                                             "scenes": []}), encoding="utf-8")
    (d / "scene_details.json").write_text(json.dumps({"status": "approved",
                                                      "scenes": []}), encoding="utf-8")
    payload = episode_payload("demo", "EP01")
    paths = {f["path"] for f in payload["files"]}
    assert "runs/EP01/episode_prose.txt" in paths
    assert "runs/EP01/prompts/blocking-s01.json" in paths
    assert all(f["bytes"] >= 0 for f in payload["files"])
