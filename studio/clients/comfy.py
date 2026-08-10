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

    def upload_audio(self, path: str | Path) -> str:
        """Upload a local audio clip into ComfyUI's input dir (voice timbre refs)."""
        return self._upload(path, "audio/wav")

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

    def interrupt(self) -> None:
        """Cancel the running prompt. Best-effort; returns whether it stopped."""
        try:
            with httpx.Client(timeout=10.0, headers=self._headers_with()) as client:
                client.post(f"{self.base_url}/interrupt")
        except httpx.HTTPError:
            pass

    def free_memory(self, unload_models: bool = True, free_memory: bool = True) -> bool:
        """Release VRAM held by loaded models / free ComfyUI's memory cache.

        Called between shots and after an episode so the renderer never leaves
        the H3 checkpoint resident on the GPU. Returns True on success.
        """
        try:
            with httpx.Client(timeout=30.0, headers=self._headers_with()) as client:
                resp = client.post(f"{self.base_url}/free",
                                   json={"unload_models": unload_models,
                                         "free_memory": free_memory})
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def wait(self, prompt_id: str, timeout_s: float = 1800.0, poll_interval: float = 3.0) -> dict:
        """Poll until the prompt finishes or errors. Returns the history entry.

        Queue-aware: the prompt is also polled while it is still sitting in the
        ComfyUI queue (pending/running). A fixed wall-clock deadline would time
        out a job that is legitimately queued behind many others on a slow GPU —
        so instead the wait extends while the prompt is present in the queue, and
        only raises once it has left the queue without completing, or after an
        absolute ``timeout_s`` of no progress.
        """
        import httpx
        deadline = time.monotonic() + timeout_s
        last_status = ""
        last_in_queue = True
        while True:
            entry = self.history(prompt_id)
            if entry:
                status = entry.get("status", {})
                last_status = status.get("status_str", "")
                if last_status in ("success", "error"):
                    return entry
            # Is the prompt still pending or running in the queue?
            in_queue = False
            try:
                with httpx.Client(timeout=5.0) as client:
                    resp = client.get(f"{self.base_url}/queue")
                    resp.raise_for_status()
                    q = resp.json()
                    for job in (q.get("queue_pending") or []) + (q.get("queue_running") or []):
                        if len(job) > 1 and job[1] == prompt_id:
                            in_queue = True
                            break
            except Exception:
                in_queue = last_in_queue   # transient; keep waiting as before
            last_in_queue = in_queue
            if in_queue:
                # Still queued/rendering — keep waiting, no deadline pressure.
                time.sleep(poll_interval)
                continue
            # Not in queue and no history yet, or finished while we were polling:
            if last_status in ("success", "error"):
                return entry
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"ComfyUI prompt {prompt_id} did not finish (last: {last_status})")
            time.sleep(poll_interval)

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
