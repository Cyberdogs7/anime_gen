"""Opencode worker: tool endpoint, finish verdict, CLI bridge, spawn contract."""
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import studio.board.opencode as oc
from studio.board.api import make_server
from studio.board.cards import new_card
from studio.board.daemon import BoardDaemon
from studio.board.opencode import OpenCodeAgent, _model_id
from studio.board.roles import get_role, load_roles
from studio.board.store import BoardStore
from studio.config import Config
from studio.prompts import BOARD_AGE_GATE


def _call(port, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _server(tmp_path):
    cfg = Config(ROOT)
    store = BoardStore(tmp_path / "b.db")
    card = new_card("c1", kind="outline", title="t", role="showrunner",
                    lane="llm", show_id="demo", episode=1)
    store.create(card)
    assert store.claim("c1", "run1", "planned", "in_work")
    store.start_run("run1", "c1", "showrunner", "produce", "llm", "m")
    httpd = make_server(store, BoardDaemon(store, cfg), cfg, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return cfg, store, httpd, httpd.server_address[1]


def test_tool_endpoint_runs_for_the_owning_run(tmp_path):
    cfg, store, httpd, port = _server(tmp_path)
    try:
        res = _call(port, "/api/tools/read_card",
                    {"run_id": "run1", "card_id": "c1", "args": {}})
        assert res["result"]["id"] == "c1"
    finally:
        httpd.shutdown()
        store.close()


def test_tool_endpoint_rejects_a_foreign_run(tmp_path):
    cfg, store, httpd, port = _server(tmp_path)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/tools/read_card",
            data=json.dumps({"run_id": "nope", "card_id": "c1", "args": {}}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as exc:
            assert exc.code in (404, 409)
        else:
            raise AssertionError("foreign run was accepted")
    finally:
        httpd.shutdown()
        store.close()


def test_finish_endpoint_stores_the_verdict(tmp_path):
    cfg, store, httpd, port = _server(tmp_path)
    try:
        _call(port, "/api/runs/finish",
              {"run_id": "run1", "status": "verified", "summary": "done"})
        assert store.run("run1")["result"]["final"]["status"] == "verified"
    finally:
        httpd.shutdown()
        store.close()


def test_model_id_and_result_mapping(tmp_path):
    cfg = Config(ROOT)
    assert _model_id(cfg, get_role(cfg, "showrunner")).startswith("lm_studio_local/")
    producer = OpenCodeAgent(role=get_role(cfg, "showrunner"), card={}, store=None,
                             phase="produce", run_id="r", cfg=cfg)
    assert producer._to_result({"status": "verified", "summary": "x"}).status == "verified"
    assert producer._to_result({"status": "retake"}).status == "retake"
    reviewer = OpenCodeAgent(role=get_role(cfg, "outline_reviewer"), card={},
                             store=None, phase="review", run_id="r", cfg=cfg)
    assert reviewer._to_result({"verdict": "fail", "notes": "n"}).status == "retake"
    assert reviewer._to_result({"verdict": "pass"}).verdict == "pass"


class _FakeProc:
    returncode = 0

    def __init__(self):
        self.stdin = io.StringIO()

    def poll(self):
        return 0

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_central_gate_in_every_agent_prompt(tmp_path):
    cfg = Config(ROOT)
    card = {"id": "c1", "kind": "outline", "title": "t", "show_id": "demo",
            "episode": 1, "state": "planned", "payload": {}, "notes": []}
    checked = 0
    for role in load_roles(cfg).values():
        if role.kind == "exec" or role.name == "human" or not role.skill:
            continue
        producer = OpenCodeAgent(role=role, card=dict(card), store=None,
                                 phase="produce", run_id="r", cfg=cfg)
        assert BOARD_AGE_GATE in producer._skill_prompt(), role.name
        checked += 1
        if role.kind == "reviewer":
            reviewer = OpenCodeAgent(role=role, card=dict(card), store=None,
                                     phase="review", run_id="r", cfg=cfg)
            assert BOARD_AGE_GATE in reviewer._skill_prompt(), role.name
            checked += 1
    # Every producer + reviewer role, both phases where applicable.
    assert checked > 20


def test_agent_spawns_opencode_in_the_worker_root(tmp_path, monkeypatch):
    cfg, store, httpd, port = _server(tmp_path)
    cfg.data["agents"]["board_url"] = f"http://127.0.0.1:{port}"
    card = store.get("c1")
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        seen["env"] = kwargs.get("env")
        store.set_run_final("run1", {"status": "verified", "summary": "fake"})
        return _FakeProc()

    monkeypatch.setattr(oc.shutil, "which", lambda name: "opencode")
    monkeypatch.setattr(oc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr("studio.gpu_manager.preload_model", lambda *a, **k: True)
    agent = OpenCodeAgent(role=get_role(cfg, "showrunner"), card=card, store=store,
                          phase="produce", run_id="run1", cfg=cfg)
    result = agent.run()
    assert result.status == "verified"
    assert seen["cmd"][:4] == ["opencode", "run", "--model", "lm_studio_local/gemma4-12b-qat-uncensored-hauhaucs-balanced"]
    assert "--agent" in seen["cmd"] and seen["cmd"][seen["cmd"].index("--agent") + 1] == "studio"
    assert Path(seen["cwd"]) == ROOT / "board"
    assert seen["env"]["STUDIO_RUN_ID"] == "run1"
    assert seen["env"]["STUDIO_CARD_ID"] == "c1"
    httpd.shutdown()
    store.close()
