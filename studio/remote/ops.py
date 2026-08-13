"""Remote control plane for per-node services (DESIGN.md §7.7).

ServiceOps runs the local service commands (LM Studio via `lms`, portable
ComfyUI via its run script). It is used by:
  - the remote-agent (HTTP) on each node,
  - the controller CLI (`studio.py lms/comfy/setup --remote`),
  - and shares the same subprocess helpers as gpu_manager.
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..config import get_config

log = logging.getLogger(__name__)


def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


class ServiceOps:
    def __init__(self, cfg=None):
        self.cfg = cfg or get_config()

    # ---- LM Studio ----

    @property
    def lms(self) -> str:
        return self.cfg.lms_cli()

    def _lms_port(self) -> int:
        return int(self.cfg.get("env", "lmstudio", {}).get("server_port", 1234))

    def _lms_ctx(self) -> int:
        return int(self.cfg.get("env", "lmstudio", {}).get("context", 32768))

    def _lms_ratio(self) -> str:
        ratio = self.cfg.get("env", "lmstudio", {}).get("gpu_ratio", "max")
        return "max" if ratio in (None, "", "max", "auto") else str(ratio)

    def lms_start(self) -> dict[str, Any]:
        port = self._lms_port()
        if _port_open(port):
            return {"ok": True, "detail": f"server already up on :{port}"}
        rc, out = _run([self.lms, "server", "start", "--port", str(port), "--cors"], timeout=120)
        time.sleep(2)
        return {"ok": rc == 0 or _port_open(port), "detail": out or f"started on :{port}"}

    def lms_load(self, model: str | None = None, gpu_ratio: str | None = None) -> dict[str, Any]:
        model = (model
                 or self.cfg.get("llm", "roles", {}).get("showrunner")
                 or self.cfg.get("llm", "model")
                 or self.cfg.get("env", "lmstudio", {}).get("models", {}).get("showrunner"))
        if not model:
            return {"ok": False, "detail": "no model configured (env.yaml lmstudio.models)"}
        gpu = gpu_ratio or self._lms_ratio()
        # LM Studio can keep multiple models resident. That is never valid on
        # this node: managed loads always replace the previous model.
        self.lms_unload()
        rc, out = _run([self.lms, "load", model, "--gpu", gpu, "-c", str(self._lms_ctx()),
                        "-p", "1", "-y"], timeout=600)
        time.sleep(2)
        return {"ok": rc == 0, "detail": out or f"load issued: {model} (gpu={gpu})"}

    def lms_unload(self) -> dict[str, Any]:
        rc, out = _run([self.lms, "unload", "--all"], timeout=120)
        return {"ok": rc == 0, "detail": out or "unloaded all"}

    def lms_status(self) -> dict[str, Any]:
        rc, out = _run([self.lms, "ps"], timeout=60)
        port = self._lms_port()
        return {"ok": rc == 0 and _port_open(port),
                "detail": out or "nothing loaded",
                "server_up": _port_open(port),
                "port": port}

    def lms_get(self, model: str) -> dict[str, Any]:
        if not model:
            return {"ok": False, "detail": "--model required"}
        rc, out = _run([self.lms, "get", model, "--gguf"], timeout=1800)
        return {"ok": rc == 0, "detail": out or f"download issued: {model}"}

    def provision_lmstudio(self, model: str | None = None) -> dict[str, Any]:
        """End-to-end: ensure server, model on disk, model loaded, API healthy."""
        steps: list[str] = []
        if not (shutil.which(self.lms) or Path(self.lms).exists()):
            return {"ok": False, "detail": f"LM Studio CLI not found: {self.lms}"}
        steps.append("lms-found")
        s = self.lms_start()
        steps.append("server:" + ("ok" if s["ok"] else "fail"))

        model = model or (self.cfg.get("env", "lmstudio", {}).get("models", {}) or {}).get(
            "showrunner")
        if model:
            rc, ls = _run([self.lms, "ls"], timeout=120)
            if model not in ls:
                g = self.lms_get(model)
                steps.append("get:" + ("ok" if g["ok"] else "fail"))
            l = self.lms_load(model)
            steps.append("load:" + ("ok" if l["ok"] else "fail"))

        port = self._lms_port()
        healthy = _port_open(port)
        steps.append("api:" + ("ok" if healthy else "down"))
        self._write_env_model(model)
        return {"ok": healthy, "detail": "; ".join(steps), "model": model, "port": port}

    def _write_env_model(self, model: str | None) -> None:
        if not model:
            return
        path = self.cfg.root / "config" / "env.yaml"
        if not path.exists():
            return
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.setdefault("lmstudio", {}).setdefault("models", {})["showrunner"] = model
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")

    # ---- ComfyUI (portable tree, role-appropriate) ----

    def _comfy_cfg(self, which: str | None = None) -> dict:
        which = which or ("krea2" if not self.cfg.is_renderer() else "h3")
        return self.cfg.comfy_instance(which)

    def comfy_start(self, which: str | None = None) -> dict[str, Any]:
        inst = self._comfy_cfg(which)
        port = int(inst.get("port", 8188))
        if _port_open(port):
            return {"ok": True, "detail": f"already up on :{port}"}
        tree = inst.get("dir", "")
        if not tree:
            return {"ok": False, "detail": "no portable tree configured (env.yaml comfyui)"}
        bat = Path(tree) / inst.get("run", "run_nvidia_gpu.bat")
        if not bat.exists():
            return {"ok": False, "detail": f"run script missing: {bat}"}
        subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(bat.parent),
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        for _ in range(int(self.cfg.get("comfy", "startup_retries", 30))):
            time.sleep(2)
            if _port_open(port):
                return {"ok": True, "detail": f"up on :{port} ({bat.name})"}
        return {"ok": False, "detail": f"startup on :{port} not confirmed"}

    def comfy_stop(self, which: str | None = None) -> dict[str, Any]:
        inst = self._comfy_cfg(which)
        port = int(inst.get("port", 8188))
        if not _port_open(port):
            return {"ok": True, "detail": f"nothing on :{port}"}
        proc = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=30)
        killed = False
        for line in proc.stdout.splitlines():
            if f":{port} " in line and "LISTENING" in line.upper():
                pid = line.strip().split()[-1]
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=30)
                killed = True
                break
        time.sleep(1)
        return {"ok": killed, "detail": f"killed PID on :{port}" if killed else f"nothing on :{port}"}

    # ---- diagnostics ----

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "node": self.cfg.node_role,
            "lms_port": self._lms_port(),
            "lms_up": _port_open(self._lms_port()),
            "shows": self.cfg.list_shows(),
        }

    # ---- Krea 2 keyframe generation (node B local ComfyUI) ----

    def _comfy_idle(self, client) -> bool:
        """True when the ComfyUI instance has an empty queue (no H3/render job running)."""
        try:
            import httpx
            resp = httpx.get(f"{client.base_url}/queue", timeout=5)
            q = resp.json()
            return not (q.get("queue_running") or q.get("queue_pending"))
        except Exception:
            return False

    def _comfy_up(self, url: str) -> bool:
        try:
            from ..clients.comfy import ComfyClient
            return ComfyClient(url).health()
        except Exception:
            return False

    def _launch_krea2(self) -> bool:
        """Start the krea2 comfy on the project's anime-h3 instance (krea2.port).

        Transient: caller must call _stop_krea2() once generation is done.
        Returns True once the instance answers on /system_stats.
        """
        inst = self._comfy_cfg("krea2")
        tree = inst.get("dir", "")
        port = int(inst.get("port", 8188) or 8188)
        if not tree or not os.path.isdir(tree):
            log.warning("krea2 comfy dir not configured (env.yaml comfyui.krea2.dir); using fallback")
            return False
        # Prefer a venv layout (D:/anime-h3), fall back to portable python_embeded.
        py = Path(tree) / "venv" / "Scripts" / "python.exe"
        if not py.exists():
            py = Path(tree) / "python_embeded" / "python.exe"
        if not py.exists():
            log.warning("krea2 comfy python not found under %s; using fallback", tree)
            return False
        cmd = [str(py), "-s", "ComfyUI/main.py", "--windows-standalone-build",
               "--listen", "127.0.0.1", "--port", str(port), "--disable-auto-launch"]
        try:
            self._krea2_proc = subprocess.Popen(
                cmd, cwd=tree, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "DETACHED_PROCESS", 0x8))
        except Exception as exc:
            log.warning("failed to launch krea2 comfy: %s", exc)
            return False
        url = f"http://127.0.0.1:{port}"
        for _ in range(90):
            time.sleep(1)
            if self._comfy_up(url):
                log.info("krea2 comfy up on :%s", port)
                return True
        log.warning("krea2 comfy did not come up on :%s; shutting down", port)
        self._stop_krea2()
        return False

    def _stop_krea2(self) -> None:
        """Stop the transient krea2 comfy we started (taskkill /T kills the tree)."""
        proc = getattr(self, "_krea2_proc", None)
        if proc is not None:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=20)
            except Exception:
                pass
            self._krea2_proc = None
            log.info("transient krea2 comfy stopped")

    def _krea2_client(self):
        """Pick the krea2 instance to use for one generation.

        Returns (ComfyClient, stop_fn | None):
          - primary (BEAST5 LanguageLearner krea2, 16 GB) is used whenever it is
            up, or started on demand when H3 (idle_guard_url) isn't rendering.
          - only falls back to Beast3 when the primary is down/unusable or a
            render (H3) is occupying the local GPU.
        """
        from ..clients.comfy import ComfyClient
        inst = self._comfy_cfg("krea2")
        primary, fallback = inst.get("url", ""), inst.get("fallback_url", "")
        guard = inst.get("idle_guard_url", "")
        fb = ComfyClient(fallback or primary or "http://192.168.50.173:8189")

        if primary and self._comfy_up(primary):
            # Prefer the 16 GB primary when it is answering.
            log.info("krea2 primary up (%s) -> using primary", primary)
            return ComfyClient(primary), None
        if guard and not self._comfy_idle(ComfyClient(guard)):
            log.info("H3 comfy (%s) is rendering -> using krea2 fallback", guard)
            return fb, None
        if primary and self._launch_krea2():
            return ComfyClient(primary), self._stop_krea2
        return fb, None

    def _beast3_llm_host(self) -> str:
        """Host:port of the fallback LLM (Beast3), from llm.fallback_url."""
        url = self.cfg.get("llm", "fallback_url", "") or ""
        return url.rstrip("/") if url else ""

    def _evict_llm(self, host: str | None = None) -> bool:
        """Unload the LLM on a node so image/video gen owns the GPU.

        Mutually-exclusive-GPU rule: no LLM while krea2/H3 renders. When ``host``
        is the local LM Studio the ``lms`` CLI unloads it directly; the Beast3
        fallback LLM (which shares Beast3's GPU with the krea2 fallback) is
        unloaded over WinRM. Best-effort: a failure only logs and continues.
        """
        target = host or "local"
        try:
            if target == "local":
                rc, out = _run([self.lms, "unload", "--all"], timeout=120)
                return rc == 0
            # Remote (Beast3) — use the cached WinRM credential, mirroring
            # how the handoff drives Beast3's LM Studio.
            import subprocess as _sp
            cred_xml = os.environ.get("BEAST3_CRED_XML") or os.path.join(
                os.environ.get("TEMP", ""), "beast3-cred.xml")
            if not os.path.isfile(cred_xml):
                log.warning("[gpu] LLM evict: no Beast3 credential at %s", cred_xml)
                return False
            ps = ("$c = Import-Clixml '%s'; $s = New-PSSession -ComputerName "
                  "192.168.50.173 -Credential $c -ErrorAction Stop; "
                  "Invoke-Command -Session $s -ScriptBlock { "
                  "& 'C:\\anime-studio\\portable\\lmstudio\\resources\\app\\.webpack\\lms.exe' "
                  "unload --all 2>&1 }; Remove-PSSession $s" % cred_xml.replace("'", "''"))
            _sp.run(["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True, timeout=180)
            return True
        except Exception as exc:
            log.warning("[gpu] LLM evict failed on %s: %s", target, exc)
            return False

    def _reload_llm(self, host: str | None = None) -> bool:
        """Reload the showrunner model on a node after image/video gen (best-effort)."""
        target = host or "local"
        model = (self.cfg.get("env", "lmstudio", {}).get("models", {}) or {}).get(
            "showrunner") or (self.cfg.get("llm", "model") or "")
        if not model:
            return True
        try:
            if target == "local":
                rc, _ = _run([self.lms, "load", model, "--gpu", "max",
                              "-c", str(self._lms_ctx()), "-y"], timeout=600)
                return rc == 0
            import subprocess as _sp
            cred_xml = os.environ.get("BEAST3_CRED_XML") or os.path.join(
                os.environ.get("TEMP", ""), "beast3-cred.xml")
            if not os.path.isfile(cred_xml):
                return False
            ps = ("$c = Import-Clixml '%s'; $s = New-PSSession -ComputerName "
                  "192.168.50.173 -Credential $c -ErrorAction Stop; "
                  "Invoke-Command -Session $s -ScriptBlock { "
                  "& 'C:\\anime-studio\\portable\\lmstudio\\resources\\app\\.webpack\\lms.exe' "
                  "load '%s' --gpu max -c %d -y 2>&1 }; Remove-PSSession $s"
                  % (cred_xml.replace("'", "''"), model.replace("'", "''"), self._lms_ctx()))
            _sp.run(["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True, timeout=600)
            return True
        except Exception as exc:
            log.warning("[gpu] LLM reload failed on %s: %s", target, exc)
            return False

    @contextmanager
    def krea2_gpu(self, evict_remote: bool = True) -> Iterator[ComfyClient]:
        """Exclusive-GPU context for one krea2 image-generation session.

        Picks the krea2 instance (primary BEAST5 or Beast3 fallback) and, before
        yielding the client, unloads the LLM that shares the target GPU so image
        gen owns all VRAM. On exit the LLM is reloaded. Yields the ComfyClient.
        """
        from ..clients.comfy import ComfyClient as _CC
        from ..gpu_manager import ServiceType, get_gpu_manager
        client, stop = self._krea2_client()
        remote = evict_remote and "127.0.0.1" not in (client.base_url or "127.0.0.1")
        if remote:
            self._evict_llm("beast3")
        with get_gpu_manager(self.cfg).acquire(ServiceType.COMFYUI):
            try:
                yield client
            finally:
                if stop:
                    stop()
                if remote:
                    self._reload_llm("beast3")

    @contextmanager
    def krea2_batch(self, evict_remote: bool = True) -> Iterator[None]:
        """Exclusive-GPU context spanning a whole batch of krea2 generations.

        Unloads the LLM on the target GPU once up front (so a long batch owns
        all VRAM) and reloads it when the batch finishes. ``evict_remote=False``
        skips the Beast3 fallback eviction for callers that stay local.
        """
        from ..gpu_manager import ServiceType, get_gpu_manager
        # The GPU manager serialises ALL local GPU users (LLM / ComfyUI / TTS).
        # Its load/unload hooks handle LLM eviction on the local node. Keep
        # the remote (Beast3) LLM eviction which the manager doesn't cover.
        k = self._comfy_cfg("krea2")
        primary = k.get("url", "")
        remote = False
        try:
            if primary and self._comfy_up(primary):
                remote = False
            else:
                remote = bool(k.get("fallback_url"))
        except Exception:
            remote = bool(k.get("fallback_url"))
        if remote and evict_remote:
            self._evict_llm("beast3")
        with get_gpu_manager(self.cfg).acquire(ServiceType.COMFYUI):
            try:
                yield
            finally:
                if remote and evict_remote:
                    self._reload_llm("beast3")

    def generate_image(self, prompt: str, seed: int = 0, aspect_ratio: str = "16:9",
                       out_path: str = "", use_lora: bool = False) -> dict[str, Any]:
        import random

        from ..comfy_workflows import generate_keyframe, load_workflow

        wf_path = self.cfg.workflows_dir / "image_keyframe.json"
        if not wf_path.exists():
            return {"ok": False, "detail": f"workflow not found: {wf_path}"}
        out = Path(out_path) if out_path else self.cfg.root / "cache" / "krea2.png"
        try:
            with self.krea2_gpu() as client:
                seed = seed or random.randint(0, 2**63)
                final = generate_keyframe(client, load_workflow(wf_path), prompt, seed, out,
                                          aspect_ratio=aspect_ratio, use_lora=use_lora)
                return {"ok": True, "detail": f"generated {final} (seed={seed}) via {client.base_url}",
                        "path": str(final), "seed": seed, "host": client.base_url}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    def verify(self) -> dict[str, Any]:
        from ..verify import run_checks
        checks = run_checks(self.cfg)
        return {"ok": all(not c.required or c.ok for c in checks),
                "detail": "\n".join(
                    f"{'[ OK ]' if c.ok else '[FAIL]'} {c.name}: {c.detail}" for c in checks)}
