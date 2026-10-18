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

#: Where the opencode CLI stores its auth (per-account API keys). The Zen/GO
#: key lives here under the "opencode-go" account by default.
DEFAULT_ZEN_AUTH_FILE = "~/.local/share/opencode/auth.json"
DEFAULT_ZEN_AUTH_ACCOUNT = "opencode-go"

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
        "auto_start_episode": True,  # hands-free: reconcile starts the next episode once the latest is complete
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
        "model": "dolphin-2.9.3-mistral-nemo-12b",
        "context": 32768,
        "roles": {
            "showrunner": "dolphin-2.9.3-mistral-nemo-12b",
            "judge": "dolphin-2.9.3-mistral-nemo-12b",
            "slop_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "continuity_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "fan_service_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "structure_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "voice_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "exposition_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "pacing_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "stakes_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "visual_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "agency_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "serial_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "growth_reviewer": "dolphin-2.9.3-mistral-nemo-12b",
            "describer": "",
        },
        "gpu_offload": True,
        "evict_before_render": True,
        # OpenCode Zen remote models (https://opencode.ai/docs/zen). When
        # enabled AND an API key is available, text prompts go remote-first and
        # fall back to the local instance on transport errors AND on
        # content-policy refusals (explicit scenes remote models won't write).
        # Without a key the studio is exactly today's local-only setup.
        "zen": {
            "enabled": True,
            "base_url": "https://opencode.ai/zen/go/v1",
            # The key is read from the opencode auth.json (account
            # "opencode-go"); the environment variable is never consulted.
            # Empty path = the standard ~/.local/share/opencode/auth.json;
            # $OPENCODE_AUTH_FILE overrides the path.
            "auth_file": "",
            "auth_account": "opencode-go",
            "model": "muse-spark-1.3-contributor",
            "model_map": {},
            # Models served on the /responses endpoint (Spark/GPT/Grok) vs
            # /chat/completions (Kimi/DeepSeek/GLM/...). Matched with fnmatch
            # so new models don't need code changes. Claude/Qwen /messages
            # models are not spoken by this client (falls back local).
            "responses_models": ["muse-spark-*", "gpt-*", "grok-*"],
            # Per-role remote models, keyed by llm role name
            # (showrunner, script, concept, slop_reviewer, ...). A call with
            # role="script" uses roles["script"]; without an explicit role the
            # client reverse-matches the requested LOCAL model name to a
            # uniquely configured role. Empty = everything uses model/model_map.
            "roles": {},
            # Remote vision: image_url parts forward as-is; vLLM video_url
            # parts are resampled to frames_per_video stills first. Disabled or
            # undecodable clips fall back to the local vision stack.
            "vision": {
                "enabled": True,
                "model": "muse-spark-1.3-contributor",
                "frames_per_video": 6,
                "frame_dim": 512,
            },
            "timeout": 180,
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
            "model": "qwen/qwen3-vl-8b",      # LM Studio model id (frame-sampling fallback path)
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
        # and live in each show's bible.yaml (see Show.style_profile).
        "genre": ["romantic-comedy", "martial-arts"],
        "tone": ["warm", "comedic", "heartfelt"],
        "maturity": "mature",
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


def zen_auth_file(auth_file: str = "") -> Path:
    """Path to the opencode auth.json holding the Zen/GO key.

    Precedence: explicit ``auth_file`` arg (config) -> ``$OPENCODE_AUTH_FILE``
    -> the standard ``~/.local/share/opencode/auth.json``. ``~`` is expanded.
    """
    raw = (auth_file or os.environ.get("OPENCODE_AUTH_FILE")
           or DEFAULT_ZEN_AUTH_FILE)
    return Path(raw).expanduser()


def read_zen_key_from_auth_file(auth_file: str = "",
                                account: str = DEFAULT_ZEN_AUTH_ACCOUNT) -> str:
    """Read the Zen/GO API key from the opencode auth.json, or "" if absent.

    Never raises: a missing / malformed file or a missing account returns ""
    so callers fall back to local-only instead of crashing.
    """
    path = zen_auth_file(auth_file)
    try:
        if not path.is_file():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    entry = data.get(account) or {}
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("key") or "").strip()


def resolve_zen_api_key(cfg: "Config | None" = None,
                        override: str | None = None) -> str:
    """Resolve the Zen/GO API key from the opencode auth.json.

    The key ALWAYS comes from the auth file — the environment variable is
    deliberately NOT consulted, so a stray ``OPENCODE_API_KEY`` can never
    shadow (or silently substitute) the signed-in key. ``override`` is only for
    explicit programmatic use (tests, one-off scripts). The auth.json
    path/account come from ``llm.zen.auth_file`` / ``llm.zen.auth_account``.
    Returns "" when the file/account has no key, keeping the studio local-only.
    """
    if override:
        return override
    cfg = cfg or get_config()
    z = cfg.get("llm", "zen", {}) or {}
    return read_zen_key_from_auth_file(
        z.get("auth_file", ""),
        z.get("auth_account", DEFAULT_ZEN_AUTH_ACCOUNT))
