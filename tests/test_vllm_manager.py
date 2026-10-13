"""Tests for the vLLM GPU-manager lifecycle (start/stop the WSL Qwen3-VL
server, staged so it only runs in the post-render window).

All `subprocess`/`wsl` calls are mocked — nothing touches a real GPU or WSL.
"""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.config import Config
from studio.gpu_manager import ServiceType, GPUManager, get_gpu_manager
from studio.vcheck import _make_vcheck_client, _vcheck_model


def _cfg(server="vllm"):
    cfg = copy.deepcopy(Config())
    cfg.data["qc"]["vcheck"]["server"] = server
    return cfg


def test_vllm_service_type_registered():
    assert ServiceType.VLLM.value == "vllm"
    assert ServiceType.VLLM in ServiceType


def test_vlm_service_type_registered():
    """VLM (LM Studio vision model) is a distinct, dispatched service type."""
    assert ServiceType.VLM.value == "vlm"
    assert ServiceType.VLM in ServiceType
    assert ServiceType.VLM in ServiceType
    assert ServiceType.VLM not in (ServiceType.LLM, ServiceType.VLLM)


def test_load_vlm_lms_loads_vcheck_model(monkeypatch):
    """_load_vlm issues `lms load <vcheck model>` (after evicting the LLM+Comfy)."""
    cfg = _cfg("lmstudio")
    cfg.data["qc"]["vcheck"]["model"] = "qwen/qwen3-vl-8b"
    vm = GPUManager(cfg)

    calls = []
    popens = []
    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return type("R", (), {"returncode": 0, "stdout": "qwen/qwen3-vl-8b  IDLE", "stderr": ""})()
    monkeypatch.setattr("studio.gpu_manager._run", fake_run)
    monkeypatch.setattr("studio.gpu_manager.subprocess.Popen",
                        lambda args, **kw: popens.append(list(args)) or type("P", (), {"poll": lambda: None})())
    monkeypatch.setattr("studio.gpu_manager._port_open", lambda port: True)
    monkeypatch.setattr("studio.gpu_manager.time.sleep", lambda *a: None)
    # CPU money-shots: evict comfy (already closed -> no-op) and skip readiness loop
    monkeypatch.setattr(vm, "_unload_comfyui", lambda: None)

    vm._load_vlm()
    loads = [p for p in popens if p and p[0:2] == ["lms", "load"]]
    assert loads, "expected an `lms load <model>` call (Popen)"
    assert "qwen/qwen3-vl-8b" in loads[0]
    # eviction: showrunner LLM unloaded before the VLM is loaded
    assert any(c and c[0:2] == ["lms", "unload"] for c in calls)


def test_unload_vlm_releases_vram(monkeypatch):
    cfg = _cfg("lmstudio")
    vm = GPUManager(cfg)
    calls = []
    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    monkeypatch.setattr("studio.gpu_manager._run", fake_run)
    monkeypatch.setattr(vm, "_wait_vram_released", lambda timeout=90.0: None)
    vm._unload_vlm()
    assert any(c and c[0:2] == ["lms", "unload"] and "--all" in c for c in calls)


def test_vcheck_client_points_at_vllm_url():
    cfg = _cfg()
    c = _make_vcheck_client(cfg)
    assert c.base_url == "http://127.0.0.1:8001/v1"


def test_vcheck_model_uses_vllm_served_name():
    cfg = _cfg()
    assert _vcheck_model(cfg) == "Qwen3-VL-8B-Instruct-FP8"


def test_load_vllm_builds_wsl_launch(monkeypatch, tmp_path):
    cfg = _cfg()
    cfg.data["qc"]["vcheck"]["vllm"]["venv_python"] = "~/vllm-venv/bin/python"
    cfg.data["qc"]["vcheck"]["vllm"]["model_dir"] = "~/models/Qwen3-VL-8B-Instruct-FP8"
    vm = GPUManager(cfg)

    launches = []
    real_popen = __import__("subprocess").Popen
    def fake_popen(args, **kw):
        if args and any("vllm.entrypoints" in str(a) for a in args):
            launches.append(args)
            return type("P", (), {"poll": lambda: None})()
        return real_popen(args, **kw)
    monkeypatch.setattr("studio.gpu_manager._run",
                        lambda cmd: type("R", (), {"returncode": 0})())
    monkeypatch.setattr("studio.gpu_manager.subprocess.Popen", fake_popen)
    # skip VRAM polling noise
    import studio.gpu_manager as gm
    monkeypatch.setattr(gm.GPUManager, "_gpu_free_mib", staticmethod(lambda: 16380))
    monkeypatch.setattr(gm.GPUManager, "_gpu_total_mib", staticmethod(lambda: 16380))
    # port closed for the early-exit check, open after the launch probe
    seq = [False, True]
    monkeypatch.setattr("studio.gpu_manager._port_open", lambda port: seq.pop(0))
    vm._comfy_procs = {}
    vm._load_vllm()
    assert len(launches) == 1
    blob = " ".join(launches[0])
    assert "vllm.entrypoints.openai.api_server" in blob
    assert "api_server ~/models/Qwen3-VL-8B-Instruct-FP8" in blob
    assert "serve ~/models" not in blob      # vLLM module takes <model> positionally
    assert "--port 8001" in blob
    assert "Qwen3-VL-8B-Instruct-FP8" in blob
    assert "--limit-mm-per-prompt" in blob


def test_load_vllm_unconfigured_is_stub(monkeypatch):
    cfg = _cfg()
    cfg.data["qc"]["vcheck"]["vllm"] = {"venv_python": "", "model_dir": ""}
    vm = GPUManager(cfg)
    monkeypatch.setattr("studio.gpu_manager._port_open", lambda port: False)
    called = []
    monkeypatch.setattr("studio.gpu_manager.subprocess.Popen",
                        lambda args, **kw: called.append(args))
    vm._load_vllm()
    assert called == []          # no launch without config


def test_unload_vllm_reaches_wsl_after_port_closes(monkeypatch):
    cfg = _cfg()
    vm = GPUManager(cfg)
    seq = [False, True, True]   # port initially closed -> first check exits
    monkeypatch.setattr("studio.gpu_manager._port_open", lambda port: seq.pop(0))
    unloads = []
    monkeypatch.setattr("studio.gpu_manager.subprocess.run",
                        lambda cmd, **kw: unloads.append(cmd) or type("R", (), {})())
    import studio.gpu_manager as gm
    monkeypatch.setattr(gm.GPUManager, "_wait_vram_released",
                        lambda self, **kw: None)
    vm._unload_vllm()
    pk = [c for c in unloads if any("pkill" in str(x) for x in c)]
    assert len(pk) == 1
    assert any("Ubuntu-22.04" in str(x) for x in pk[0])