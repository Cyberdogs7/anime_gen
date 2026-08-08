"""Tests for the remote control plane (agent + client + auth)."""
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.remote.agent import _make_handler
from studio.remote.client import RemoteClient, RemoteError, resolve_client
from studio.config import Config


class FakeOps:
    def health(self):
        return {"ok": True, "node": "worker", "shows": []}

    def lms_status(self):
        return {"ok": True, "detail": "fake status"}

    def lms_load(self, model, gpu_ratio):
        return {"ok": True, "detail": f"loaded {model} {gpu_ratio}"}

    def lms_unload(self):
        return {"ok": True, "detail": "unloaded"}

    def lms_start(self):
        return {"ok": True, "detail": "started"}

    def lms_get(self, model):
        return {"ok": True, "detail": f"got {model}"}

    def provision_lmstudio(self, model=None):
        return {"ok": True, "detail": "provisioned"}

    def comfy_start(self, which=None):
        return {"ok": True, "detail": f"started {which}"}

    def comfy_stop(self, which=None):
        return {"ok": True, "detail": f"stopped {which}"}

    def verify(self):
        return {"ok": True, "detail": "all ok"}


def _serve(ops, token, port=0):
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(ops, token))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def test_agent_routes_and_auth():
    httpd = _serve(FakeOps(), token="sekrit")
    port = httpd.server_address[1]
    try:
        client = RemoteClient("127.0.0.1", port, "sekrit")
        assert client.health()["node"] == "worker"
        assert client.lms_load("dolphin-12b", "0.5")["detail"] == "loaded dolphin-12b 0.5"
        assert client.provision_lmstudio()["ok"] is True
        assert client.comfy_start("krea2")["detail"] == "started krea2"
        assert client.verify()["ok"] is True
    finally:
        httpd.shutdown()


def test_agent_rejects_bad_token():
    httpd = _serve(FakeOps(), token="sekrit")
    port = httpd.server_address[1]
    try:
        client = RemoteClient("127.0.0.1", port, "wrong")
        with pytest.raises(RemoteError):
            client.health()
    finally:
        httpd.shutdown()


def test_agent_returns_404_for_unknown_route():
    httpd = _serve(FakeOps(), token="")
    port = httpd.server_address[1]
    try:
        import httpx
        resp = httpx.post(f"http://127.0.0.1:{port}/nope", timeout=5)
        assert resp.status_code == 404
    finally:
        httpd.shutdown()


def test_resolve_client_role():
    cfg = Config(ROOT)
    cfg.data["remotes"]["worker"] = {"host": "192.168.1.50", "port": 8123, "token": "t"}
    client = resolve_client(cfg, "worker")
    assert client.base == "http://192.168.1.50:8123"
    assert client.token == "t"
    raw = resolve_client(cfg, "10.0.0.7:9000")
    assert raw.base == "http://10.0.0.7:9000"
