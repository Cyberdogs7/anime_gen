"""RemoteClient - drives a node's remote-agent from the controller machine."""
from __future__ import annotations

from typing import Any

import httpx


class RemoteError(RuntimeError):
    pass


class RemoteClient:
    def __init__(self, host: str, port: int = 8123, token: str = "", timeout: float = 600.0):
        self.base = f"http://{host}:{port}"
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout, headers=self._headers()) as client:
            resp = client.request(method, f"{self.base}{path}", json=payload)
            if resp.status_code == 401:
                raise RemoteError("unauthorized (agent token mismatch)")
            if resp.status_code >= 400:
                raise RemoteError(f"agent error {resp.status_code}: {resp.text[:200]}")
            return resp.json()

    def health(self) -> dict[str, Any]:
        return self._call("GET", "/health")

    def lms_start(self) -> dict[str, Any]:
        return self._call("POST", "/lms/start")

    def lms_load(self, model: str | None = None, gpu_ratio: str | None = None) -> dict[str, Any]:
        body = {}
        if model:
            body["model"] = model
        if gpu_ratio:
            body["gpu_ratio"] = gpu_ratio
        return self._call("POST", "/lms/load", body)

    def lms_unload(self) -> dict[str, Any]:
        return self._call("POST", "/lms/unload")

    def lms_status(self) -> dict[str, Any]:
        return self._call("GET", "/lms/status")

    def lms_get(self, model: str) -> dict[str, Any]:
        return self._call("POST", "/lms/get", {"model": model})

    def provision_lmstudio(self, model: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/lms/provision", {"model": model} if model else None)

    def comfy_start(self, which: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/comfy/start", {"which": which} if which else None)

    def comfy_stop(self, which: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/comfy/stop", {"which": which} if which else None)

    def generate_image(self, prompt: str, seed: int = 0, aspect_ratio: str = "16:9",
                       out_path: str = "", use_lora: bool = False) -> dict[str, Any]:
        body = {"prompt": prompt, "seed": seed, "aspect_ratio": aspect_ratio,
                "out_path": out_path, "use_lora": use_lora}
        return self._call("POST", "/krea2/generate", body)

    def verify(self) -> dict[str, Any]:
        return self._call("POST", "/verify")


def resolve_client(cfg, remote: str) -> RemoteClient:
    """Build a RemoteClient from a role name ('worker'|'renderer') or host[:port]."""
    if remote in cfg["remotes"]:
        r = cfg.remote(remote)
        return RemoteClient(r["host"], int(r.get("port", 8123)), r.get("token", ""))
    host, _, port = remote.partition(":")
    port = int(port) if port else 8123
    token = cfg.get("remotes", "controller", {}).get("token", "")
    return RemoteClient(host, port, token)
