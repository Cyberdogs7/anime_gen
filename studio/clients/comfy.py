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
        except Exception:
            return False

    def queue_busy(self) -> bool:
        """True when the instance has a running or pending job.

        Used to gate interactive renders (e.g. Qwen-Image-Edit) behind long
        H3 video renders on the SAME shared ComfyUI: the Qwen model (19 GB)
        cannot share the 16 GB GPU with an H3 render, and jumping in mid-render
        silently produces a black frame.
        """
        try:
            with httpx.Client(timeout=5.0, headers=self._headers_with()) as client:
                resp = client.get(f"{self.base_url}/queue")
                q = resp.json()
                return bool(q.get("queue_running") or q.get("queue_pending"))
        except Exception:
            return False

    def wait_idle(self, timeout_s: float = 3600.0, poll_interval: float = 5.0) -> bool:
        """Block until the instance queue is empty. Returns False on timeout."""
        import time
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if not self.queue_busy():
                return True
            time.sleep(poll_interval)
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

    def submit(self, workflow: dict, front: bool = False) -> str:
        """POST a workflow graph; returns the prompt_id.

        ``front=True`` jumps the new prompt to the FRONT of the queue, ahead of
        any pending jobs (interactive edits beat long-running video renders).
        """
        payload: dict = {"prompt": workflow}
        if front:
            payload["front"] = True
        with httpx.Client(timeout=self.timeout, headers=self._headers_with()) as client:
            resp = client.post(f"{self.base_url}/prompt", json=payload)
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

    def wait(self, prompt_id: str, timeout_s: float = 1800.0, poll_interval: float = 3.0,
             hard_timeout_s: float | None = None) -> dict:
        """Poll until the prompt finishes or errors. Returns the history entry.

        Queue-aware: while the prompt sits in the ComfyUI queue (pending/running)
        the wait is allowed to exceed ``timeout_s`` — a fixed wall-clock deadline
        would time out a job legitimately queued behind many others on a slow GPU.
        Once the prompt leaves the queue without completing (e.g. it was cleared
        or failed hard), the wait gives up after ``timeout_s`` of no progress.

        ``hard_timeout_s`` is an ABSOLUTE wall-clock cap on the whole wait, applied
        regardless of queue state. A wedged ComfyUI (a job stuck in queue_running
        forever — after an OOM, a node crash, or a hung sampler) would otherwise
        block the caller indefinitely: the prompt never leaves the queue, so the
        queue-aware grace period never expires. On expiry the running prompt is
        interrupted (best-effort) and a TimeoutError raised, so the pipeline's
        self-healing sees a bounded failure and can recover instead of hanging.
        """
        import httpx
        start = time.monotonic()
        deadline = start + timeout_s
        last_status = ""
        while True:
            if hard_timeout_s is not None and time.monotonic() - start >= hard_timeout_s:
                self.interrupt()
                raise TimeoutError(
                    f"ComfyUI prompt {prompt_id} hung in the queue for "
                    f"{hard_timeout_s:.0f}s; interrupted")
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
                in_queue = False   # can't confirm queued -> fall through to deadline
            if in_queue:
                # Still queued/rendering — keep waiting, no deadline pressure.
                time.sleep(poll_interval)
                continue
            # Not in queue and not in history: it was cleared / never ran.
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
