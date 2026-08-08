"""`studio.py setup` - per-machine provisioning checklist (DESIGN.md §7.7).

Everything portable, nothing system-wide. Idempotent.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import get_config


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = False


def _which(name: str) -> str | None:
    return shutil.which(name)


def run_setup(cfg=None, create_venv: bool = False, install_deps: bool = False) -> int:
    cfg = cfg or get_config()
    checks: list[Check] = []

    # Python
    py = sys.version_info
    checks.append(Check("python", (py.major, py.minor) >= (3, 11),
                        f"{py.major}.{py.minor}.{py.micro}"))

    # venv + deps
    venv_py = cfg.root / ".venv" / "Scripts" / "python.exe"
    checks.append(Check("venv", venv_py.exists(),
                        str(venv_py) if venv_py.exists() else "missing - run --venv"))
    deps_missing = [m for m in ("yaml", "httpx") if _import(m) is False]
    checks.append(Check("deps", not deps_missing,
                        "ok" if not deps_missing else f"missing: {deps_missing}"))

    # ffmpeg (portable path or PATH)
    ff = cfg.ffmpeg_bin()
    ok_ff = ff != "ffmpeg" and Path(ff).exists() or (ff == "ffmpeg" and _which("ffmpeg"))
    checks.append(Check("ffmpeg", bool(ok_ff), ff))

    # LM Studio (portable `lms`)
    lms = cfg.lms_cli()
    lms_ok = _which(lms) or Path(lms).exists()
    checks.append(Check("lmstudio-cli", bool(lms_ok), lms if lms_ok else f"{lms} not found"))
    model = (cfg.get("env", "lmstudio", {}).get("models", {}) or {}).get("showrunner", "")
    checks.append(Check("lmstudio-model", bool(model), model or "no showrunner model in env.yaml"))

    # Portable ComfyUI for this node's role
    which = "krea2" if not cfg.is_renderer() else "h3"
    inst = cfg.comfy_instance(which)
    tree = Path(inst.get("dir", "")) if inst.get("dir") else None
    bat = (tree / inst.get("run", "run_nvidia_gpu.bat")) if tree else None
    ok_tree = bool(tree and bat and bat.exists())
    checks.append(Check(f"comfyui:{which}",
                        ok_tree,
                        str(bat) if ok_tree else (inst.get("dir") or "not configured in env.yaml")))

    # Broker
    provider = cfg.get("bus", "provider", "memory")
    if provider == "redis":
        import socket
        port = 6379
        with socket.socket() as s:
            s.settimeout(1.0)
            checks.append(Check("broker(redis)", s.connect_ex(("127.0.0.1", port)) == 0,
                                "redis :6379 reachable"))
    else:
        checks.append(Check("broker", True, "memory (no broker needed)"))

    # Shows
    checks.append(Check("shows", True, f"{len(cfg.list_shows())} show(s)"))

    failed = 0
    for c in checks:
        print(f"{'[ OK ]' if c.ok else '[FAIL]'} {c.name:22s} {c.detail}")
        if not c.ok and c.required:
            failed += 1

    if create_venv:
        print("\n[setup] creating .venv ...")
        subprocess.run([sys.executable, "-m", "venv", str(cfg.root / ".venv")], check=True)
        checks.append(Check("venv", True, "created"))
    if install_deps:
        print("[setup] pip install -e .[dev] ...")
        pip = cfg.root / ".venv" / "Scripts" / "python.exe"
        subprocess.run([str(pip), "-m", "pip", "install", "-e", str(cfg.root) + "[dev]"],
                       check=True)
        print("[setup] deps installed (run --check to re-verify)")

    print(f"\n{'Setup complete.' if not failed else f'{failed} required item(s) missing.'} "
          f"Node role: {cfg.node_role}")
    return 1 if failed else 0


def _import(name: str) -> bool | None:
    try:
        __import__(name)
        return True
    except ImportError:
        return False
