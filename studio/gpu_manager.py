"""Per-node exclusive-GPU manager.

Only one GPU-heavy service is resident at a time on a node. Lifecycle is
portable and config-driven (DESIGN.md §7.7): LM Studio via the `lms` CLI,
ComfyUI via its portable run script, TTS via a configured venv. If a service
is not configured or its binary is missing, acquire() still works (stub mode)
so the pipeline is testable before hardware is provisioned.

Pattern adapted from LanguageLearner's gpu_manager.py. See DESIGN.md §15.4.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from enum import Enum
from typing import Iterator

from .config import get_config

log = logging.getLogger(__name__)


class ServiceType(Enum):
    LLM = "llm"
    TTS = "tts"
    COMFYUI = "comfyui"
    STT = "stt"
    VLLM = "vllm"
    VLM = "vlm"          # LM Studio vision model (e.g. qwen/qwen3-vl-8b) for frame-based QC


# Estimated VRAM budgets (MB) - used for scheduling decisions later.
VRAM_BUDGETS = {
    ServiceType.LLM: 24000,
    ServiceType.TTS: 7000,
    ServiceType.COMFYUI: 12000,
    ServiceType.STT: 3000,
    ServiceType.VLLM: 15000,
    ServiceType.VLM: 12000,
}


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20)
    except subprocess.TimeoutExpired:
        # A hung `lms`/launcher subprocess must not block a render or storyboard
        # thread forever (it holds the exclusive GPU lock while it waits).
        log.warning("[gpu] subprocess timed out: %s", " ".join(str(c) for c in cmd))
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="timeout")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr="not found")


class _ProcessGpuLock:
    """Cross-process lock for the machine's single shared GPU."""

    def __init__(self, path):
        self.path = path
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        if self._file.seek(0, 2) == 0:
            self._file.write(b"0")
            self._file.flush()
        self._file.seek(0)
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.5)
        else:
            import fcntl
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class GPUManager:
    def __init__(self, cfg=None):
        self.cfg = cfg or get_config()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._current: ServiceType | None = None
        self._current_model: str | None = None
        self._holders = 0
        self._comfy_procs: dict[str, subprocess.Popen] = {}
        self._process_lock = _ProcessGpuLock(self.cfg.root / ".gpu-manager.lock")
        # Per-thread re-entrancy depth: a thread that already holds the GPU (e.g. a
        # storyboard ref pass holding the COMFYUI lock) and then makes an LLM call
        # must NOT acquire again — it would wait on its own condition forever.
        self._local = threading.local()

    @property
    def current(self) -> ServiceType | None:
        with self._lock:
            return self._current

    # ---- lifecycle stubs (subclass/override for real loaders) ----

    def _load(self, service: ServiceType, model: str | None = None) -> None:
        if service == ServiceType.LLM:
            self._load_llm(model)
        elif service == ServiceType.COMFYUI:
            self._load_comfyui()
        elif service == ServiceType.VLLM:
            self._load_vllm()
        elif service == ServiceType.VLM:
            self._load_vlm()
        else:
            log.info("[gpu] load %s (stub; VRAM ~%d MB)", service.value, VRAM_BUDGETS[service])

    def _unload(self, service: ServiceType | None) -> None:
        if service is None:
            return
        if service == ServiceType.LLM:
            self._unload_llm()
        elif service == ServiceType.COMFYUI:
            self._unload_comfyui()
        elif service == ServiceType.VLLM:
            self._unload_vllm()
        elif service == ServiceType.VLM:
            self._unload_vlm()
        else:
            log.info("[gpu] unload %s (stub)", service.value)

    # ---- LM Studio (portable `lms` CLI) ----

    def _load_llm(self, model: str | None = None) -> None:
        # Safety eject: evict any ComfyUI models from VRAM before loading LLM.
        inst = self._comfy_cfg()
        port = int(inst.get("port", 8188))
        if _port_open(port):
            try:
                url = inst.get("url") or f"http://127.0.0.1:{port}"
                import httpx
                with httpx.Client(timeout=30.0) as client:
                    client.post(f"{url}/free",
                                json={"unload_models": True, "free_memory": True})
            except Exception:
                pass
        lms = self.cfg.lms_cli()
        port = self.cfg.get("env", "lmstudio", {}).get("server_port", 1234)
        ctx = self.cfg.get("env", "lmstudio", {}).get("context", 32768)
        ratio = self.cfg.get("env", "lmstudio", {}).get("gpu_ratio", "max")
        gpu = "max" if ratio in (None, "", "max", "auto") else str(ratio)
        if not _port_open(port):
            _run([lms, "server", "start", "--port", str(port), "--cors"])
            time.sleep(4)
        model = (model
                 or self.cfg.get("llm", "roles", {}).get("showrunner")
                 or self.cfg.get("llm", "model")
                 or self.cfg.get("env", "lmstudio", {}).get("models", {}).get("showrunner", ""))
        if model:
            _run([lms, "unload", "--all"])
            time.sleep(1)
            subprocess.Popen([lms, "load", model, "--gpu", gpu, "-c", str(ctx),
                              "--parallel", "1", "-y"])
            # wait for the model to be resident: poll `lms ps` for the model id
            # (port alone is insufficient — server is already listening while load at 0-34%)
            for _ in range(60):
                time.sleep(3)
                if _port_open(port):
                    cur = _run([lms, "ps"]).stdout or ""
                    if model in cur and "Loading" not in cur:
                        # extra settle: ensure /v1/models lists it
                        try:
                            import httpx
                            with httpx.Client(timeout=3.0) as _c:
                                if _c.get(f"http://127.0.0.1:{port}/v1/models").status_code == 200:
                                    return
                        except Exception:
                            return
                        return
            log.warning("[gpu] LLM load: API open but model readiness not confirmed for %s", model)
        else:
            log.info("[gpu] LLM: no showrunner model configured in env.yaml; using existing server")

    def _unload_llm(self) -> None:
        _run([self.cfg.lms_cli(), "unload", "--all"])
        self._wait_vram_released()

    @staticmethod
    def _gpu_free_mib() -> int | None:
        """Current free GPU memory in MiB via nvidia-smi (None if unavailable)."""
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace").stdout.strip()
            if not out:
                return None
            return int(out.splitlines()[0].strip())
        except Exception:
            return None

    @staticmethod
    def _gpu_total_mib() -> int | None:
        """Total GPU memory in MiB via nvidia-smi (None if unavailable)."""
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace").stdout.strip()
            if not out:
                return None
            return int(out.splitlines()[0].strip())
        except Exception:
            return None

    def _wait_vram_released(self, timeout: float = 90.0,
                            min_free_mib: int = 4096) -> None:
        """Block until the GPU actually has enough free VRAM for ComfyUI.

        `lms unload --all` is asynchronous: the LLM's VRAM can take many
        seconds to release. Proceeding while the LLM still holds the GPU
        starves ComfyUI's disk offload, which surfaces as the comfy_aimdo
        `hostbuf_file_reader_read failed` during H3 generation. Only returns
        once free VRAM comfortably exceeds the resident-LLM footprint.
        """
        total = self._gpu_total_mib()
        if self._gpu_free_mib() is None:
            # No nvidia-smi (non-NVIDIA node) — fall back to a fixed settle delay.
            time.sleep(5)
            return
        target = max(min_free_mib, int(total * 0.6)) if total else min_free_mib
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            free = self._gpu_free_mib()
            if free is not None and free >= target:
                log.info("[gpu] VRAM free after LLM unload: %d MiB (target %d)", free, target)
                return
            time.sleep(1)
        log.warning("[gpu] VRAM still constrained after LLM unload "
                    "(free=%s MiB, target=%d); proceeding anyway",
                    self._gpu_free_mib(), target)

    # ---- LM Studio VLM (Qwen3-VL frame-based visual QC) ----

    def _vlm_model(self) -> str:
        """The LM Studio vision model id for the frame-sampling vcheck path."""
        return (self.cfg.get("qc", "vcheck", {}).get("model")
                or "qwen/qwen3-vl-8b")

    def _load_vlm(self) -> None:
        """Load the vcheck vision model (Qwen3-VL) into LM Studio under the lock.

        Loads AFTER evicting the showrunner LLM and any ComfyUI models, so the
        VLM gets the full VRAM budget. Model id comes from qc.yaml vcheck.model;
        context/gpu_ratio reuse the env.lmstudio settings (a vision model needs
        far less context than the showrunner). Never raises if unconfigured.
        """
        model = self._vlm_model()
        if not model:
            log.info("[gpu] vlm: no vcheck.model configured (stub)")
            return
        port = self.cfg.get("env", "lmstudio", {}).get("server_port", 1234)
        # Evict ComfyUI + showrunner so the VLM has the GPU to itself.
        try:
            self._unload_comfyui()
        except Exception:
            log.debug("[gpu] vlm pre-eject comfyui failed", exc_info=True)
        if not _port_open(port):
            _run([self.cfg.lms_cli(), "server", "start", "--port", str(port), "--cors"])
            time.sleep(4)
        ctx = int(self.cfg.get("qc", "vcheck", {}).get("context")
                  or self.cfg.get("env", "lmstudio", {}).get("context", 32768))
        ratio = self.cfg.get("qc", "vcheck", {}).get("gpu_ratio",
                       self.cfg.get("env", "lmstudio", {}).get("gpu_ratio", "max"))
        gpu = "max" if ratio in (None, "", "max", "auto") else str(ratio)
        _run([self.cfg.lms_cli(), "unload", "--all"])
        time.sleep(1)
        log.info("[gpu] loading VLM %s for visual QC", model)
        subprocess.Popen([self.cfg.lms_cli(), "load", model, "--gpu", gpu,
                          "-c", str(ctx), "--parallel", "1", "-y"])
        # Wait for the API to serve the vision model (it appears in `lms ps`
        # once resident; a competing unload could otherwise publish an empty
        # model list while the load is still racing in).
        for _ in range(60):
            time.sleep(3)
            if _port_open(port):
                cur = _run([self.cfg.lms_cli(), "ps"]).stdout or ""
                if model in cur:
                    return
        log.warning("[gpu] vlm: API open but model readiness not confirmed for %s", model)

    def _unload_vlm(self) -> None:
        """Unload the VLM (and any resident LM Studio model) to free VRAM."""
        _run([self.cfg.lms_cli(), "unload", "--all"])
        self._wait_vram_released()

    # ---- vLLM (Qwen3-VL native-video VLM, served inside WSL2) ----

    def _vllm_cfg(self) -> dict:
        return dict(self.cfg.get("qc", "vcheck", {}).get("vllm", {}) or {})

    @staticmethod
    def _vllm_fps(max_frames: int) -> float:
        """FPS that samples ~max_frames from the studio's ~10s clips.

        Qwen3-VL samples video at ``fps`` (its processor kwarg), so we derive it
        from the target frame budget instead of pinning frame counts per clip.
        """
        return round(max(0.5, min(8.0, int(max_frames) / 10.0)), 2)

    def _vllm_port_open(self) -> bool:
        cfg = self._vllm_cfg()
        port = int(cfg.get("port", 8001) or 8001)
        return _port_open(port)

    def _load_vllm(self) -> None:
        """Start the WSL2 vLLM server (Qwen3-VL-8B) for native-video checks.

        Runs only in the post-render window (the render has already released
        COMFYUI). Safety-ejects the LM Studio LLM first, then boots `vllm serve`
        inside WSL and waits for the API on :port. Never raises if unconfigured
        (stub mode keeps the pipeline testable).
        """
        cfg = self._vllm_cfg()
        if not cfg.get("venv_python") or not cfg.get("model_dir"):
            log.info("[gpu] vLLM: not configured in qc.yaml vcheck.vllm (stub)")
            return
        # Safety eject: leave the GPU entirely free before vLLM claims it.
        try:
            self._unload_llm()
        except Exception:
            log.debug("[gpu] vLLM pre-eject llm failed", exc_info=True)
        if self._vllm_port_open():
            log.info("[gpu] vLLM already up on :%s", cfg.get("port", 8001))
            return
        distro = cfg.get("distro", "Ubuntu-22.04")
        port = int(cfg.get("port", 8001) or 8001)
        model_arg = cfg.get("model", "Qwen3-VL-8B-Instruct-FP8")
        model_dir = cfg.get("model_dir")
        venv_py = cfg.get("venv_python")
        gpu_mem = float(cfg.get("gpu_memory_utilization", 0.9) or 0.9)
        max_len = int(cfg.get("max_model_len", 32768) or 32768)
        max_frames = int(cfg.get("max_video_frames", 32) or 32)
        args = [
            "wsl", "-d", distro, "--", "bash", "-lc",
            # vLLM's module launcher takes the model as a POSITIONAL arg (no
            # `serve` subcommand keyword) — `... api_server serve <model>` is
            # rejected: "unrecognized arguments: <model>".
            f"nohup {venv_py} -m vllm.entrypoints.openai.api_server "
            f"{model_dir} --port {port} --host 0.0.0.0 "
            f"--served-model-name {model_arg} --max-model-len {max_len} "
            f"--gpu-memory-utilization {gpu_mem} "
            f"--limit-mm-per-prompt '{{\"video\": 1}}' "
            f"--mm-processor-kwargs '{{\"fps\": {self._vllm_fps(max_frames)}}}' "
            f"> /tmp/vllm_qwen3vl.log 2>&1 & echo started > /tmp/vllm_started.flag",
        ]
        try:
            subprocess.Popen(args, creationflags=getattr(subprocess,
                                       "CREATE_NEW_CONSOLE", 0))
        except Exception as exc:
            log.warning("[gpu] vLLM launch failed: %s", exc)
            return
        for _ in range(int(self.cfg.get("comfy", "startup_retries", 30)) * 2):
            time.sleep(3)
            if self._vllm_port_open():
                log.info("[gpu] vLLM up on :%s", port)
                return
        log.warning("[gpu] vLLM startup on :%s not confirmed (log: /tmp/vllm_qwen3vl.log)", port)

    def _unload_vllm(self) -> None:
        """Stop the WSL vLLM server and wait for its VRAM to release."""
        cfg = self._vllm_cfg()
        if not cfg.get("venv_python"):
            return
        distro = cfg.get("distro", "Ubuntu-22.04")
        port = int(cfg.get("port", 8001) or 8001)
        subprocess.run(["wsl", "-d", distro, "--", "bash", "-lc",
                        "pkill -f vllm.entrypoints.openai.api_server || true"],
                       capture_output=True, timeout=60)
        for _ in range(20):
            if not _port_open(port):
                self._wait_vram_released(timeout=30)
                return
            time.sleep(2)
        # force: the server ignored the graceful kill; hard-kill its process
        subprocess.run(["wsl", "-d", distro, "--", "bash", "-lc",
                        "pkill -9 -f vllm.entrypoints.openai.api_server || true"],
                       capture_output=True, timeout=60)
        self._wait_vram_released(timeout=30)

    # ---- ComfyUI (portable tree per role) ----

    def _comfy_cfg(self) -> dict:
        which = "krea2" if not self.cfg.is_renderer() else "h3"
        return self.cfg.comfy_instance(which)

    def _load_comfyui(self) -> None:
        # Safety eject: if a previous process or a manual load left an LLM
        # model resident in LM Studio, evict it before ComfyUI claims VRAM.
        self._unload_llm()
        inst = self._comfy_cfg()
        port = int(inst.get("port", 8188))
        if _port_open(port):
            log.info("[gpu] ComfyUI already up on :%s", port)
            return
        tree = inst.get("dir", "")
        run_script = inst.get("run", "run_nvidia_gpu.bat")
        if not tree:
            log.info("[gpu] ComfyUI: no portable tree configured in env.yaml (stub)")
            return
        import os
        from pathlib import Path
        tree_p = Path(tree)
        bat = tree_p / run_script
        if not bat.exists():
            log.warning("[gpu] ComfyUI run script missing: %s", bat)
            return
        proc = subprocess.Popen(
            ["cmd", "/c", str(bat)],
            cwd=str(tree_p),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        self._comfy_procs[str(port)] = proc
        for _ in range(int(self.cfg.get("comfy", "startup_retries", 30))):
            time.sleep(2)
            if _port_open(port):
                return
        log.warning("[gpu] ComfyUI startup on :%s not confirmed", port)

    def _unload_comfyui(self) -> None:
        inst = self._comfy_cfg()
        port = int(inst.get("port", 8188))
        # First: tell ComfyUI to evict its models from VRAM. taskkill alone
        # doesn't reclaim GPU memory; the models stay leaked. POST /free
        # unloads them cleanly so the next tenant gets the full VRAM budget.
        if _port_open(port):
            try:
                url = inst.get("url") or f"http://127.0.0.1:{port}"
                import httpx
                with httpx.Client(timeout=30.0) as client:
                    client.post(f"{url}/free",
                                json={"unload_models": True, "free_memory": True})
            except Exception:
                log.debug("[gpu] ComfyUI /free failed (already gone?)", exc_info=True)
        proc = self._comfy_procs.pop(str(port), None)
        if proc and proc.poll() is None:
            # Kill the whole tree: the `cmd /c run_*.bat` wrapper spawns python
            # as a child, so a bare terminate() leaves ComfyUI alive on the GPU.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        # With manage_lifecycle we own the instance: also stop one we didn't
        # start (e.g. a manual launch), so release() really frees the GPU.
        if bool(self.cfg.get("comfy", "manage_lifecycle", True)) and _port_open(port):
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                  text=True, timeout=30).stdout
            for line in out.splitlines():
                if f":{port} " in line and "LISTENING" in line.upper():
                    pid = line.strip().split()[-1]
                    subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                                   capture_output=True, timeout=30)
                    break
        time.sleep(1)
        # Wait for VRAM to actually be released (Comfy's VRAM lingers after taskkill)
        self._wait_vram_released(timeout=30, min_free_mib=4096)

    # ---- acquire / release ----

    @contextmanager
    def acquire(self, service: ServiceType, model: str | None = None) -> Iterator[None]:
        """Exclusive GPU access for `service`. Blocks until the GPU is free.

        Re-entrant per thread: when the calling thread already holds the GPU for
        another service (e.g. a storyboard ref pass holds the COMFYUI lock for
        krea2 and then calls the LLM), a nested acquire would wait on its own
        condition forever (self-deadlock). The outer holder already owns VRAM, so
        the nested call simply runs without re-acquiring.
        """
        if getattr(self._local, "depth", 0) > 0:
            self._local.depth += 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        with self._condition:
            while self._holders and not (
                    service == ServiceType.LLM
                    and self._current == ServiceType.LLM
                    and self._current_model == model):
                self._condition.wait()
            first_holder = self._holders == 0
            if first_holder:
                # The manager is used by multiple CLI/server processes. The
                # in-memory lock alone cannot serialize those processes.
                self._process_lock.acquire()
                try:
                    self._load(service, model)
                    self._current = service
                    self._current_model = model
                except Exception:
                    self._process_lock.release()
                    raise
            self._holders += 1
        self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            with self._condition:
                self._holders -= 1
                if self._holders == 0:
                    try:
                        self._unload(self._current)
                    finally:
                        self._current = None
                        self._current_model = None
                        self._process_lock.release()
                        self._condition.notify_all()


_default: GPUManager | None = None


def get_gpu_manager(cfg=None) -> GPUManager:
    global _default
    if _default is None:
        _default = GPUManager(cfg)
    return _default
