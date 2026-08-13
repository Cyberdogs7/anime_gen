"""Configuration loader.

Reads ``config/*.yaml`` (one file per top-level section) and deep-merges them
over the built-in defaults below. Unknown keys in YAML are preserved so config
files can carry extra fields without breaking loading.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

DEFAULTS: dict[str, dict[str, Any]] = {
    "pipeline": {
        "mode": "overnight",
        "nightly_time": "02:00",
        "budget_min": 480,
        "shot_workers": 1,
        "max_retakes_per_shot": 2,
        "max_revisions": 2,
        "fallback_local": True,
        "resolution": [1344, 768],
        "fps": 24,
        "default_shot_duration_s": 10.125,
        "insert_shot_duration_s": 5.167,
        "hero_best_of": 2,
        "auto_start_episode": True,  # hands-free: reconcile starts the next episode once the latest is complete
        "pause_storyboard": False,   # true: stop the reconciler from generating/restarting storyboards (debugging)
    },
    "bus": {
        "provider": "memory",  # memory | redis
        "url": "redis://127.0.0.1:6379",
        "password": None,
        "group_prefix": "studio",
    },
    "comfy": {
        "nodes": {
            "worker": {"url": "http://127.0.0.1:8188", "api_key": None},
            "renderer": {"url": "http://127.0.0.1:8188", "api_key": None},
        },
        "checkpoints": {
            "krea2": "krea2TurboNSFWAIO_v10.safetensors",
            "krea2_clip": "qwen3vl_4b_fp8_scaled.safetensors",
            "krea2_clip_type": "krea2",
            "krea2_vae": "qwen_image_vae.safetensors",
            "krea2_lora": "fedor_bypass.safetensors",
            "h3_fl2va": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "h3_ref2va": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
            "h3_clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            "h3_turbo_lora": "minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors",
            "h3_video_vae": "minimax_h3_video_vae_fp16.safetensors",
            "h3_audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
        },
        "manage_lifecycle": True,
        "startup_retries": 30,
        "fp32_vae": True,
    },
    "llm": {
        "base_url": "http://127.0.0.1:1234/v1",
        "fallback_url": "",
        "concurrency_limit": 4,
        "gpu_guard_urls": [],
        "model": "dolphin-2.9.3-mistral-nemo-12b",
        "context": 32768,
        "roles": {
            "showrunner": "dolphin-2.9.3-mistral-nemo-12b",
            "judge": "dolphin-2.9.3-mistral-nemo-12b",
            "slop_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "continuity_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "fan_service_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "structure_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "growth_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "describer": "",
        },
        "gpu_offload": True,
        "evict_before_render": True,
    },
    "qc": {
        "anchor_ssim_min": 0.35,
        "composite_min": 0.60,
        "identity_min": 0.40,
        "artifact_retake": True,
        "judge_frames": 5,
        "lip_sync_check": True,
        "boundary_ssim_min": 0.15,
    },
    "approval": {
        "global": {"auto_approve": False},
        "bootstrap": {"depth": "full"},
        "gates": {
            "show": "gated",
            "character": "gated",
            "voice": "gated",
            "story": "auto",
            "shot": "auto",
            "episode": "gated",
            "costume": "gated",
            "object": "gated",
        },
        "auto_approve_after_hours": 0,
    },
    "reviewers": {
        "max_revisions": 2,
        "roles": ["slop", "continuity", "fan_service"],
        "plan_max_revisions": 4,       # plan-level (outline) review loop cap
        "plan_roles": ["structure"],    # outline reviewers: structure (plot/scene coherence)
        "thresholds": {
            "slop_block": 0.45,
            "continuity_block": True,
            "fanservice_quota_miss": True,
        },
    },
    "growth": {
        "plotline_cadence_episodes": 3,   # introduce a new plotline roughly every N episodes
        "max_new_characters_per_plotline": 2,
    },
    "show_profile": {
        "genre": ["romantic-comedy", "martial-arts"],
        "tone": ["warm", "comedic", "heartfelt"],
        "maturity": "mature",
        "baseline": "ranma-1-2",
        "runtime_target_s": 1320,
    },
    "env": {
        "node": "worker",           # worker (B) | renderer (A)
        "portable_root": "portable",
        "lmstudio": {
            "cli": "lms",
            "server_port": 1234,
            "context": 32768,
            "gpu_ratio": "max",     # 'max' | 'off' | 0..1 (partial offload for small GPUs)
            "models": {"showrunner": "dolphin-2.9.3-mistral-nemo-12b"},
        },
        "comfyui": {
            "krea2": {"dir": "", "run": "run_nvidia_gpu.bat", "port": 8188},
            "h3": {"dir": "", "run": "run_nvidia_gpu.bat", "port": 8188},
        },
        "ffmpeg": "",
        "tts": {"venv": "", "sox": "", "models": {"custom_voice": "", "voice_design": ""}},
    },
    "agent": {
        "bind": "0.0.0.0",          # remote-agent listener (firewall-scope to controller IP)
        "port": 8123,
    },
    "remotes": {
        "controller": {"token": ""},
        "worker": {"host": "127.0.0.1", "port": 8123, "token": ""},
        "renderer": {"host": "127.0.0.1", "port": 8123, "token": ""},
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """Loaded, merged configuration. Sections are accessed as attributes."""

    def __init__(self, root: Path | str = ROOT):
        self.root = Path(root)
        self.data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        cfg_dir = self.root / "config"
        merged: dict[str, dict[str, Any]] = copy.deepcopy(DEFAULTS)
        for name in DEFAULTS:
            path = cfg_dir / f"{name}.yaml"
            if path.exists():
                with open(path, "r", encoding="utf-8") as fh:
                    section = yaml.safe_load(fh) or {}
                if isinstance(section, dict):
                    merged[name] = _deep_merge(merged.get(name, {}), section)
        self.data = merged

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.data[section]

    def get(self, section: str, key: str | None = None, default: Any = None) -> Any:
        sec = self.data.get(section, {})
        if key is None:
            return sec
        return sec.get(key, default)

    # Convenience paths
    @property
    def db_path(self) -> Path:
        return self.root / "studio.db"

    @property
    def shows_dir(self) -> Path:
        return self.root / "shows"

    @property
    def workflows_dir(self) -> Path:
        return self.root / "workflows"

    def show_path(self, show_id: str) -> Path:
        return self.shows_dir / show_id

    def list_shows(self) -> list[str]:
        if not self.shows_dir.exists():
            return []
        return sorted(
            p.name for p in self.shows_dir.iterdir() if p.is_dir() and (p / "bible.yaml").exists()
        )

    # --- machine / portability (DESIGN.md §7.7) ---

    @property
    def node_role(self) -> str:
        return self.get("env", "node", "worker")

    def is_renderer(self) -> bool:
        return self.node_role == "renderer"

    def ffmpeg_bin(self) -> str:
        """Portable ffmpeg path from env.yaml, else the PATH lookup."""
        return self.get("env", "ffmpeg", "") or os.environ.get("STUDIO_FFMPEG", "") or "ffmpeg"

    def lms_cli(self) -> str:
        return self.get("env", "lmstudio", {}).get("cli", "lms") or "lms"

    @property
    def portable_dir(self) -> Path:
        """Project-scoped portable instances root (relative paths resolve against repo root)."""
        raw = self.get("env", "portable_root", "portable")
        p = Path(raw)
        return p if p.is_absolute() else (self.root / p)

    def comfy_instance(self, which: str) -> dict:
        """Portable ComfyUI instance config for role-appropriate use ('krea2' or 'h3')."""
        return self.get("env", "comfyui", {}).get(which, {})

    def remote(self, role: str) -> dict:
        """Control-plane endpoint for a remote node ('worker' | 'renderer')."""
        r = dict(self.get("remotes", role, {}))
        if not r.get("token"):
            r["token"] = self.get("remotes", "controller", {}).get("token", "")
        return r

    def __repr__(self) -> str:
        return f"Config(root={self.root}, sections={list(self.data)})"


_default: Config | None = None


def get_config(root: Path | str = ROOT) -> Config:
    """Return the process-wide default Config (lazy singleton)."""
    global _default
    if _default is None:
        _default = Config(root)
    return _default
