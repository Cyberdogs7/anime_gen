"""TTS adapter interface + engines. See DESIGN.md §12.

Concrete engine for v0: Qwen3-TTS 12Hz 1.7B (CustomVoice + VoiceDesign), the
user's proven setup. A NullTTS adapter is provided so the pipeline runs and is
testable before the models are wired to node B.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import get_config

logger = logging.getLogger(__name__)


@dataclass
class VoiceConfig:
    """Mirror of shows/<id>/voices/<id>.yaml (§8.3)."""

    id: str
    engine: str = "qwen3_tts"
    mode: str = "designed"          # preset | designed
    speaker: str | None = None      # preset: one of the Qwen3-TTS speaker pool
    voice_description: str = ""     # designed: free-text voice spec
    speed: float = 1.0
    pitch: float = 0.0

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            f"{self.speaker}|{self.voice_description}".encode()
        ).hexdigest()


class TTSAdapter(Protocol):
    def synthesize(self, text: str, voice: VoiceConfig, out_path: Path) -> float:
        """Render text -> wav at out_path; return duration in seconds."""


class Qwen3TTSAdapter:
    """Synthesize via studio/tts_runner.py using the Qwen3-TTS 12Hz 1.7B models.

    Runs with the venv configured in config/env.yaml -> tts.venv (the venv that
    has qwen_tts + torch). The VoiceDesign model drives 'designed' voices
    (voice_description), the CustomVoice model drives named presets (speaker).
    On any failure it falls back to a silent sample so the approval chain never
    blocks on the audio stack.
    """

    def __init__(self, runner: str = "tts_runner.py", python: str = "",
                 config=None):
        self.runner = runner
        self.python = python
        self.config = config

    def health(self) -> bool:
        cfg = self.config or get_config()
        return bool(cfg.get("env", "tts", {}).get("venv", ""))

    def synthesize(self, text: str, voice: VoiceConfig, out_path: Path) -> float:
        cfg = self.config or get_config()
        env_tts = cfg.get("env", "tts", {}) or {}
        python = self.python or env_tts.get("venv", "") or ""
        runner = Path(__file__).resolve().parent.parent / self.runner
        model = (env_tts.get("models", {}) or {}).get(
            "voice_design" if voice.mode == "designed" else "custom_voice", "")
        sox = env_tts.get("sox", "")
        if not python:
            # No TTS venv configured (tests / isolated config): stay silent.
            logger.info("no tts venv configured; writing silent sample for %s", voice.id)
            return NullTTS().synthesize(text, voice, out_path)
        cmd = [python, str(runner), "--text", text, "--out", str(out_path)]
        if voice.mode == "designed":
            cmd += ["--voice-description", voice.voice_description or ""]
        else:
            cmd += ["--speaker", voice.speaker or ""]
        if model:
            cmd += ["--model", model]
        if sox:
            cmd += ["--sox-dir", sox]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=900)
        except Exception as exc:
            logger.warning("Qwen3-TTS synthesis failed (%s); writing silent sample", exc)
            return NullTTS().synthesize(text, voice, out_path)
        return _duration(out_path)


class NullTTS:
    """Placeholder engine: writes a valid silent WAV in pure Python (no ffmpeg)."""

    def health(self) -> bool:
        return True

    def synthesize(self, text: str, voice: VoiceConfig, out_path: Path) -> float:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = 24000
        duration = max(0.5, min(5.0, 0.25 + len(text) * 0.06))
        _write_silent_wav(out_path, sample_rate, duration)
        return duration


def _write_silent_wav(path: Path, sample_rate: int, duration: float) -> None:
    """Write a minimal valid 16-bit mono PCM WAV of silence."""
    import struct

    n_samples = int(sample_rate * duration)
    data = b"\x00\x00" * n_samples
    byte_rate = sample_rate * 2
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" \
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16) \
        + b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)


class TTSService:
    """Registry: engine id -> adapter. VoiceConfig.from_file(path) loads §8.3 YAML."""

    def __init__(self, config: dict | None = None):
        self.config = config or get_config()
        self._adapters = {
            "qwen3_tts": Qwen3TTSAdapter(config=self.config),
            "null": NullTTS(),
        }

    def register(self, engine: str, adapter: TTSAdapter) -> None:
        self._adapters[engine] = adapter

    def get(self, engine: str) -> TTSAdapter:
        return self._adapters.get(engine, self._adapters["null"])

    def synthesize(self, text: str, voice: VoiceConfig, out_path: Path) -> float:
        """Pick the adapter by voice.engine, cache by fingerprint (idempotent)."""
        cache_dir = self.config.root / "cache" / "tts"
        cache_path = cache_dir / f"{voice.fingerprint}-{hashlib.sha256(text.encode()).hexdigest()[:12]}.wav"
        if cache_path.exists():
            return _duration(cache_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        duration = self.get(voice.engine).synthesize(text, voice, out_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            cache_path.write_bytes(out_path.read_bytes())
        return duration

    @staticmethod
    def load_voice(path: Path) -> VoiceConfig:
        import yaml
        data = json.loads(json.dumps(yaml.safe_load(path.read_text(encoding="utf-8")) or {}))
        return VoiceConfig(
            id=data.get("id", path.stem),
            engine=data.get("engine", "qwen3_tts"),
            mode=data.get("mode", "designed"),
            speaker=data.get("speaker"),
            voice_description=data.get("voice_description", ""),
            speed=data.get("speed", 1.0),
            pitch=data.get("pitch", 0.0),
        )

    def health(self) -> bool:
        return True  # null adapter always works; real engines report at M1


def _duration(path: Path) -> float:
    from .ffmpeg import ffprobe
    info = ffprobe(path)
    return float(info.get("format", {}).get("duration", 0.0)) if info else 0.0
