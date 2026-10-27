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
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import get_config

logger = logging.getLogger(__name__)


@dataclass
class VoiceConfig:
    """Mirror of shows/<id>/voices/<id>.yaml (§8.3).

    ``mode``:
      designed -> VoiceDesign from ``voice_description`` (creates a voice ONCE)
      clone    -> Base model, cloning the approved ``clone_ref`` sample; this is
                  the per-line mode (a fixed identity, no re-design)
      preset   -> CustomVoice named speaker (legacy/fallback)
    """

    id: str
    engine: str = "qwen3_tts"
    mode: str = "designed"
    speaker: str | None = None
    voice_description: str = ""
    clone_ref: str = ""             # approved reference WAV
    clone_text: str = ""            # exact transcript of that WAV (ICL)
    clone_cache: str = ""           # persisted clone prompt (.pt)
    speed: float = 1.0
    pitch: float = 0.0

    @classmethod
    def from_file(cls, path: str | Path) -> "VoiceConfig":
        """Load a voice YAML; relative clone paths resolve against the SHOW dir."""
        import yaml

        p = Path(path).resolve()
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        show_dir = p.parent.parent
        for key in ("clone_ref", "clone_cache"):
            val = d.get(key) or ""
            if val and not Path(val).is_absolute():
                d[key] = str(show_dir / val)
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in fields})

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            f"{self.mode}|{self.speaker}|{self.voice_description}|{self.clone_ref}"
            f"|{self.clone_text}".encode()
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
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def health(self) -> bool:
        cfg = self.config or get_config()
        return bool(cfg.get("env", "tts", {}).get("venv", ""))

    def _worker(self, python: str, runner: Path, env_tts: dict) -> subprocess.Popen:
        """The RESIDENT Base-model worker (loads once, serves many lines)."""
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        base = (env_tts.get("models", {}) or {}).get("base", "")
        if not base:
            raise RuntimeError("no TTS base model configured (env.yaml tts.models.base)")
        design = (env_tts.get("models", {}) or {}).get("voice_design", "")
        cmd = [python, str(runner), "--serve", "--base-model", base]
        if design:
            cmd += ["--design-model", design]
        idle = env_tts.get("idle_timeout_s", 300)
        try:
            cmd += ["--idle-timeout", str(int(idle))]
        except (TypeError, ValueError):
            cmd += ["--idle-timeout", "300"]
        if env_tts.get("sox"):
            cmd += ["--sox-dir", env_tts["sox"]]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        return self._proc

    def _request(self, req: dict, python: str, runner: Path, env_tts: dict) -> float:
        with self._lock:
            proc = self._worker(python, runner, env_tts)
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            # Startup warnings may be printed to stdout; skip anything that is not
            # a protocol reply.
            resp = None
            while True:
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("TTS worker exited")
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and "ok" in obj:
                    resp = obj
                    break
        if resp is None:
            raise RuntimeError("TTS worker gave no reply")
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "tts failed")
        return float(resp.get("duration") or 0.0)

    def _clone(self, text: str, voice: VoiceConfig, out_path: Path,
               python: str, runner: Path, env_tts: dict) -> float:
        req = {"op": "clone", "text": text, "language": "English",
               "out": str(out_path), "ref_audio": voice.clone_ref,
               "ref_text": voice.clone_text, "cache": voice.clone_cache or "",
               "model": (env_tts.get("models", {}) or {}).get("base", "")}
        return self._request(req, python, runner, env_tts)

    def _design(self, text: str, voice: VoiceConfig, out_path: Path,
                python: str, runner: Path, env_tts: dict) -> float:
        req = {"op": "design", "text": text, "language": "English",
               "instruct": voice.voice_description or "", "out": str(out_path)}
        return self._request(req, python, runner, env_tts)

    def synthesize(self, text: str, voice: VoiceConfig, out_path: Path) -> float:
        cfg = self.config or get_config()
        env_tts = cfg.get("env", "tts", {}) or {}
        python = self.python or env_tts.get("venv", "") or ""
        runner = Path(__file__).resolve().parent.parent / self.runner
        if not python:
            # No TTS venv configured (tests / isolated config): stay silent.
            logger.info("no tts venv configured; writing silent sample for %s", voice.id)
            return NullTTS().synthesize(text, voice, out_path)

        # Per-line path: clone the approved identity on the resident Base model.
        if voice.mode == "clone" and voice.clone_ref:
            try:
                return self._clone(text, voice, out_path, python, runner, env_tts)
            except Exception as exc:
                logger.warning("voice clone failed (%s); writing silent sample", exc)
                return NullTTS().synthesize(text, voice, out_path)

        # Voice creation: one resident VoiceDesign session designs the whole cast
        # (the model is loaded once and reused across characters).
        if voice.mode == "designed":
            try:
                return self._design(text, voice, out_path, python, runner, env_tts)
            except Exception as exc:
                logger.warning("voice design failed (%s); writing silent sample", exc)
                return NullTTS().synthesize(text, voice, out_path)

        # Legacy preset (one-shot CustomVoice).
        model = (env_tts.get("models", {}) or {}).get("custom_voice", "")
        sox = env_tts.get("sox", "")
        cmd = [python, str(runner), "--text", text, "--out", str(out_path),
               "--speaker", voice.speaker or ""]
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
