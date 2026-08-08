"""Qwen3-TTS runner (executed with the LanguageLearner venv).

Synthesizes a WAV from text + a voice description (VoiceDesign) or a named
speaker (CustomVoice) using the Qwen3-TTS 12Hz 1.7B models. qwen_tts hard-
requires sox on PATH, so --sox-dir is added before the package is imported.

The studio's own venv does NOT have qwen_tts; the adapter shells out to this
script with the venv that does (config/env.yaml -> tts.venv).

Usage:
    <tts.venv python> studio/tts_runner.py --text "Hello" \
        --voice-description "deep baritone" --model <VoiceDesign dir> \
        --out out.wav [--sox-dir <sox bin dir>]
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--voice-description", default="")
    ap.add_argument("--speaker", default="")
    ap.add_argument("--language", default="English")
    ap.add_argument("--model", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sox-dir", default="")
    ap.add_argument("--max-new-tokens", type=int, default=2048)
    args = ap.parse_args()

    if args.sox_dir:
        os.environ["PATH"] = args.sox_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ.setdefault("SOX_EXECUTABLE", os.path.join(args.sox_dir, "sox.exe"))

    import torch
    import soundfile as sf
    from qwen_tts import Qwen3TTSModel

    model = Qwen3TTSModel.from_pretrained(
        args.model or "Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        device_map="cuda",
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )

    if args.voice_description:
        wavs, sr = model.generate_voice_design(
            text=args.text, language=args.language, instruct=args.voice_description,
            non_streaming_mode=True, max_new_tokens=args.max_new_tokens,
        )
    else:
        wavs, sr = model.generate_custom_voice(
            text=args.text, language=args.language,
            speaker=(args.speaker or "ryan").lower().replace(" ", "_"),
            instruct="", non_streaming_mode=True, max_new_tokens=args.max_new_tokens,
        )

    if isinstance(wavs, (list, tuple)):
        wavs = wavs[0]
    audio = wavs.detach().cpu().float().numpy() if hasattr(wavs, "detach") else wavs
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    sf.write(args.out, audio, sr)
    print(f"wrote {args.out}: {len(audio) / sr:.2f}s @ {sr}Hz", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
