"""ComfyUI client (REST + history polling). See DESIGN.md §7.1."""
from __future__ import annotations

import time
from pathlib import Path

import httpx


class ComfyClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8188", api_key: str | None = None,
                 timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._headers = {}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    def _headers_with(self) -> dict:
        return dict(self._headers)

    def health(self) -> bool:
        try:
            with httpx.Client(timeout=3.0, headers=self._headers_with()) as client:
                resp = client.get(f"{self.base_url}/system_stats")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def upload_image(self, path: str | Path) -> str:
        """Upload a local image into ComfyUI's input dir; returns the filename for LoadImage."""
        return self._upload(path, "image/png")

    def upload_video(self, path: str | Path) -> str:
        """Upload a local video into ComfyUI's input dir; returns the filename (for retake)."""
        return self._upload(path, "video/mp4")

    def _upload(self, path: str | Path, content_type: str) -> str:
        path = Path(path)
        data = {"overwrite": "true", "type": "input"}
        files = {"image": (path.name, path.read_bytes(), content_type)}
        with httpx.Client(timeout=self.timeout, headers=self._headers_with()) as client:
            resp = client.post(f"{self.base_url}/upload/image", data=data, files=files)
            resp.raise_for_status()
            return resp.json().get("name") or path.name

    def submit(self, workflow: dict) -> str:
        """POST a workflow graph; returns the prompt_id."""
        with httpx.Client(timeout=self.timeout, headers=self._headers_with()) as client:
            resp = client.post(f"{self.base_url}/prompt", json={"prompt": workflow})
            resp.raise_for_status()
            return resp.json()["prompt_id"]

    def history(self, prompt_id: str) -> dict:
        with httpx.Client(timeout=self.timeout, headers=self._headers_with()) as client:
            resp = client.get(f"{self.base_url}/history/{prompt_id}")
            resp.raise_for_status()
            return resp.json().get(prompt_id, {})

    def wait(self, prompt_id: str, timeout_s: float = 1800.0, poll_interval: float = 3.0) -> dict:
        """Poll until the prompt finishes or errors. Returns the history entry."""
        deadline = time.monotonic() + timeout_s
        last_status = ""
        while time.monotonic() < deadline:
            entry = self.history(prompt_id)
            if entry:
                status = entry.get("status", {})
                last_status = status.get("status_str", "")
                if last_status in ("success", "error"):
                    return entry
            time.sleep(poll_interval)
        raise TimeoutError(f"ComfyUI prompt {prompt_id} did not finish (last: {last_status})")

    def download(self, filename: str, subfolder: str = "", node_type: str = "output",
                 dest: str | None = None) -> bytes:
        """Fetch a generated file from ComfyUI /view into bytes (or write to dest)."""
        params = {"filename": filename, "type": node_type}
        if subfolder:
            params["subfolder"] = subfolder
        with httpx.Client(timeout=self.timeout, headers=self._headers_with()) as client:
            resp = client.get(f"{self.base_url}/view", params=params)
            resp.raise_for_status()
            data = resp.content
        if dest:
            from pathlib import Path
            Path(dest).write_bytes(data)
        return data
