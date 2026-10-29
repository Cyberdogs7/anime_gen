"""Configuration loader.

Reads ``config/*.yaml`` (one file per top-level section) and deep-merges them
over the built-in defaults below. Unknown keys in YAML are preserved so config
files can carry extra fields without breaking loading.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

#: Default episode runtime (22:00) in seconds, used when a show declares none.
DEFAULT_RUNTIME_TARGET_S = 1320

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
        "auto_start_episode": False,  # the agent board starts episodes; legacy reconciler stays off
        "legacy_reconciler": False,   # true: re-enable the old stage walker (pre-board system)
        "pause_storyboard": False,   # true: stop the reconciler from generating/restarting storyboards (debugging)
        "stale_job_s": 1800,         # storyboard/render job with no progress this long is declared hung and restarted
        "multishot": {  # H3 Multishot seamless chaining (joeygambino v2.6.3)
            "enabled": False,
            "continuity": "context_pin",
            "frames_per_shot": 192,
            "steps": 14,
            "take_seconds": 0,
            "chain_gain_control": "flatten",
            "master_normalize": "luma+contrast",
            "seed_per_shot": True,
            "preview_first_shot": True,
            "low_ram_master": False,
            "remote_encoder": False,
        },
        # Chunked episode-outline generation: the outline is written in two passes
        # so the scene count driven by the story fits the model's output budget.
        # Phase 1 produces the skeleton (one-line scene summaries); Phase 2 expands
        # each scene's Setup/Change/Consequence beats in batches.
        "plan_beats_batch": 2,        # scenes expanded per beat call (1-4)
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
            "h3_fused": "minimax_h3_fused_refdelta_r1024_turbo8_mystic07_int8_convrot.safetensors",
            "h3_clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
            "h3_turbo_lora": "minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors",
            "h3_ref2v_turbo_lora": "minimax_h3_turbo_4step_ckpt500_pruned_comfyui.safetensors",
            "h3_video_vae": "minimax_h3_video_vae_fp16.safetensors",
            "h3_video_vae_int8": "minimax_h3_video_vae_int8_convrot.safetensors",
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
        "model": "",
        "context": 80000,
        # Role -> model assignments are PER-SHOW/PER-STUDIO facts (config/llm.yaml);
        # the code ships no model names of its own.
        "roles": {},
        "gpu_offload": True,
        "evict_before_render": True,
    },
    "agents": {
        # Agent-first board. Lanes are concurrency resources; roles name the
        # lane, model and tool allowlist they run with. Per-role model
        # overrides live in config/agents.yaml.
        "lanes": {
            "llm": {"capacity": 2},
            "image": {"capacity": 1},
            "video": {"capacity": 1},
            "audio": {"capacity": 1},
            "io": {"capacity": 1},
            "vision": {"capacity": 1},
        },
        "roles": {},
        "tick_s": 5.0,
        "run_timeout_s": 14400.0,
        "port": 8130,
        "bind": "127.0.0.1",
        "board_url": "http://127.0.0.1:8130",
        "opencode": {
            "enabled": True,
            "provider": "lm_studio_local",
        },
    },
    "qc": {
        "anchor_ssim_min": 0.35,
        "composite_min": 0.60,
        "identity_min": 0.40,
        "artifact_retake": True,
        "judge_frames": 5,
        "lip_sync_check": True,
        "boundary_ssim_min": 0.15,
        "stt": {
            "enabled": True,
            "model": "small",
            "device": "cpu",
            "compute_type": "int8",
            "language": "en",
            "min_line_sim": 0.55,
            "min_lines_covered": 0.5,
            "min_explained": 0.70,
        },
        # Post-render visual check (VLM on the GPU): sample frames from each
        # rendered clip and have a vision-language model verify the characters
        # match their approved refs and the scripted action actually plays out.
        # Runs AFTER an episode render completes (the GPU is free again), never
        # inline with H3. Failures feed the shot gate as auto-rejects (capped).
        "vcheck": {
            "enabled": True,
            "server": "vllm",                 # vllm (native video via WSL) | lmstudio (frame sampling)
            "model": "gemma4-12b-qat-uncensored-hauhaucs-balanced",      # LM Studio model id (frame-sampling fallback path)
            "frames": 8,                      # frames sampled per clip (evenly spread)
            "frame_sample_dim": 512,          # short-side downscale for sampled frames (lmstudio path)
            "max_refs": 4,                    # up to this many character refs per call
            "temperature": 0.2,
            "max_tokens": 3500,
            "timeout_s": 300,
            "auto_feed": True,                # fail -> auto-reject into the shot gate (capped)
            "max_auto_retakes": 2,            # auto-reject cap per shot; beyond this a human decides
            "schedule": "post_render",        # post_render | manual
            # Native-video path: Qwen3-VL-8B served by vLLM inside WSL2. Only
            # started (via the GPU manager) in the post-render window, then
            # stopped before the next H3 render.
            "vllm": {
                "url": "http://127.0.0.1:8001/v1",
                "model": "Qwen3-VL-8B-Instruct-FP8",
                "distro": "Ubuntu-22.04",
                "venv_python": "~/vllm-venv/bin/python",
                "model_dir": "~/models/Qwen3-VL-8B-Instruct-FP8",
                "port": 8001,
                "gpu_memory_utilization": 0.9,
                "max_model_len": 32768,
                "max_video_frames": 32,
            },
        },
    },
    "approval": {
        "global": {"auto_approve": False},
        "bootstrap": {"depth": "full"},
        "gates": {
            "show": "gated",
            "character": "gated",
            "voice": "gated",
            "plan": "gated",   # episode outline — human approves the plot first
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
        "roles": ["slop", "continuity", "fan_service", "voice", "exposition",
                  "pacing", "stakes", "visual", "agency", "serial"],
        "plan_max_revisions": 6,       # plan-level (outline) review loop cap
        "plan_roles": ["structure"],    # outline reviewers: structure (plot/scene coherence)
        # Writers'-room reviewers that also run on the OUTLINE (before it locks the
        # script): length/pacing, location variety, fan-service delivery, stakes,
        # agency, serial progression, continuity, slop, and deterministic repetition.
        # voice/exposition are EXCLUDED - they judge spoken lines that do not exist
        # at outline stage; they still run on the prose/script.
        "plan_room_roles": ["slop", "continuity", "fan_service", "pacing",
                            "stakes", "visual", "agency", "serial", "repetition"],
        "thresholds": {
            "slop_block": 0.45,
            "continuity_block": True,
            "fanservice_quota_miss": True,
            "structure_block": "any",
            "voice_block": "high",
            "exposition_block": "high",
            "pacing_block": "high",
            "stakes_block": "high",
            "visual_block": "high",
            "agency_block": "high",
            "serial_block": "high",
        },
    },
    "growth": {
        "season_episodes": 12,            # episodes per season (season-first canon)
        "plotline_cadence_episodes": 3,   # legacy roll cadence (deprecated fallback only)
        "max_new_characters_per_plotline": 2,
        "battle_cadence_episodes": 3,      # allow a battle episode at most once every N episodes
        "battle_plotline_kinds": ["battle"],  # plotline.kind values treated as battle/combat threads
        # Per-show entity names. These are SHOW FACTS, not global defaults: they
        # live empty here and each show carries its own. The main cast comes from
        # bible.cast, and threat threads are derived from the show's cast (see
        # development._plotline_kind), so no show-specific names belong in code.
        "enemy_entity_names": [],
        "main_cast_names": [],
        # Arc roll: how many episodes a newly-rolled plotline carries, and how long
        # a battle plotline's arc may run (battles are kept short).
        "arc_length_min": 1,
        "arc_length_max": 6,
        "battle_arc_length_min": 1,
        "battle_arc_length_max": 3,
        "plotlines_per_episode_min": 1,
        "plotlines_per_episode_max": 4,
        "cooldown_episodes": 4,            # episodes a finished arc is unavailable
    },
    "show_profile": {
        # Bootstrap defaults for zero-input `init-show`. Style references
        # (baseline / fanservice_baseline) are NOT here: they are per-show facts
        # and live in each show's bible.yaml (see Show.style_profile). Maturity is
        # also a show fact: leave it blank here unless the studio wants a default.
        "genre": ["romantic-comedy", "martial-arts"],
        "tone": ["warm", "comedic", "heartfelt"],
        "maturity": "",
        "runtime_target_s": DEFAULT_RUNTIME_TARGET_S,
    },
    "env": {
        "node": "worker",           # worker (B) | renderer (A)
        "portable_root": "portable",
        "lmstudio": {
            "cli": "lms",
            "server_port": 1234,
            "context": 32768,
            "gpu_ratio": "max",     # 'max' | 'off' | 0..1 (partial offload for small GPUs)
            "models": {"showrunner": ""},
        },
        "comfyui": {
            "krea2": {"dir": "", "run": "run_nvidia_gpu.bat", "port": 8188},
            "h3": {"dir": "", "run": "run_nvidia_gpu.bat", "port": 8188},
        },
        "ffmpeg": "",
        "tts": {"venv": "", "sox": "", "models": {"base": "", "custom_voice": "",
                                                   "voice_design": ""}},
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
        """Read a config value: ``get(section)`` -> whole section dict,
        ``get(section, key)`` -> value, ``get(section, key, default)`` -> with default.

        Also supports the standard ``dict.get`` idiom ``get(section, some_dict)``:
        a NON-string second argument is treated as the default (so the whole
        section is returned), instead of being silently used as a key name.
        """
        sec = self.data.get(section, {})
        if not isinstance(sec, dict):
            return default
        if key is None:
            return sec
        if not isinstance(key, str):     # cfg.get("section", {}) dict-default idiom
            return sec if default is None else default
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


def reload_config(root: Path | str = ROOT) -> Config:
    """Drop the cached default Config and re-read ``config/*.yaml``.

    Called after the dashboard writes a settings change so the next
    ``get_config()`` (and every ``cfg.get(...)`` behind it) sees the new value
    without a process restart.
    """
    global _default
    _default = None
    return get_config(root)
