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


# Estimated VRAM budgets (MB) - used for scheduling decisions later.
VRAM_BUDGETS = {
    ServiceType.LLM: 24000,
    ServiceType.TTS: 7000,
    ServiceType.COMFYUI: 12000,
    ServiceType.STT: 3000,
}


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr="not found")


class GPUManager:
    def __init__(self, cfg=None):
        self.cfg = cfg or get_config()
        self._lock = threading.Lock()
        self._current: ServiceType | None = None
        self._holders = 0
        self._comfy_procs: dict[str, subprocess.Popen] = {}

    @property
    def current(self) -> ServiceType | None:
        with self._lock:
            return self._current

    # ---- lifecycle stubs (subclass/override for real loaders) ----

    def _load(self, service: ServiceType) -> None:
        if service == ServiceType.LLM:
            self._load_llm()
        elif service == ServiceType.COMFYUI:
            self._load_comfyui()
        else:
            log.info("[gpu] load %s (stub; VRAM ~%d MB)", service.value, VRAM_BUDGETS[service])

    def _unload(self, service: ServiceType | None) -> None:
        if service is None:
            return
        if service == ServiceType.LLM:
            self._unload_llm()
        elif service == ServiceType.COMFYUI:
            self._unload_comfyui()
        else:
            log.info("[gpu] unload %s (stub)", service.value)

    # ---- LM Studio (portable `lms` CLI) ----

    def _load_llm(self) -> None:
        lms = self.cfg.lms_cli()
        port = self.cfg.get("env", "lmstudio", {}).get("server_port", 1234)
        ctx = self.cfg.get("env", "lmstudio", {}).get("context", 32768)
        ratio = self.cfg.get("env", "lmstudio", {}).get("gpu_ratio", "max")
        gpu = "max" if ratio in (None, "", "max", "auto") else str(ratio)
        if not _port_open(port):
            _run([lms, "server", "start", "--port", str(port), "--cors"])
            time.sleep(4)
        model = (self.cfg.get("env", "lmstudio", {}).get("models", {}) or {}).get("showrunner", "")
        if model:
            _run([lms, "unload", "--all"])
            time.sleep(1)
            subprocess.Popen([lms, "load", model, "--gpu", gpu, "-c", str(ctx), "-y"])
            # wait for the API to answer (LM Studio model load can take a while)
            for _ in range(60):
                time.sleep(3)
                if _port_open(port):
                    return
            log.warning("[gpu] LLM load: API port open but model readiness not confirmed")
        else:
            log.info("[gpu] LLM: no showrunner model configured in env.yaml; using existing server")

    def _unload_llm(self) -> None:
        _run([self.cfg.lms_cli(), "unload", "--all"])
        time.sleep(1)

    # ---- ComfyUI (portable tree per role) ----

    def _comfy_cfg(self) -> dict:
        which = "krea2" if not self.cfg.is_renderer() else "h3"
        return self.cfg.comfy_instance(which)

    def _load_comfyui(self) -> None:
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

    # ---- acquire / release ----

    @contextmanager
    def acquire(self, service: ServiceType) -> Iterator[None]:
        """Exclusive GPU access for `service`. Blocks until the GPU is free."""
        with self._lock:
            if self._current != service:
                self._unload(self._current)
                self._load(service)
                self._current = service
            self._holders += 1
        try:
            yield
        finally:
            with self._lock:
                self._holders -= 1
                if self._holders == 0:
                    self._unload(self._current)
                    self._current = None


_default: GPUManager | None = None


def get_gpu_manager(cfg=None) -> GPUManager:
    global _default
    if _default is None:
        _default = GPUManager(cfg)
    return _default
