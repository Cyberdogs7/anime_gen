"""Qwen3-TTS runner (executed with the LanguageLearner venv).

Two modes:

* one-shot (default): synthesize a WAV from text + a voice description
  (VoiceDesign) or a named speaker (CustomVoice).
* --serve: a RESIDENT worker for the Base (clone) model. It loads the model once
  and reads JSON-lines requests from stdin, so dialogue lines never pay a
  model load/unload per call. A character's clone prompt is persisted to disk
  and reused; it is rebuilt only when the model id, reference audio or reference
  text changes.

qwen_tts hard-requires sox on PATH, so --sox-dir is added before the package is
imported.

Usage:
    <tts.venv python> studio/tts_runner.py --text "Hello" \
        --voice-description "deep baritone" --model <VoiceDesign dir> \
        --out out.wav [--sox-dir <sox bin dir>]

    <tts.venv python> studio/tts_runner.py --serve \
        --base-model <Base dir> [--sox-dir <sox bin dir>]

Serve request/response (one JSON object per line):
    {"op":"clone","text":"...","language":"English","out":"line.wav",
     "ref_audio":"voice.wav","ref_text":"...","cache":"voice.pt","model":"<Base dir>"}
    {"op":"ping"}
  -> {"ok":true,"duration":1.23}
  -> {"ok":false,"error":"..."}
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_wav(wavs, sr: int, out: str):
    import soundfile as sf

    if isinstance(wavs, (list, tuple)):
        wavs = wavs[0]
    audio = wavs.detach().cpu().float().numpy() if hasattr(wavs, "detach") else wavs
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    sf.write(out, audio, sr)
    return len(audio) / sr


def _meta_path(cache: str) -> str:
    return cache + ".json"


def _cache_valid(cache: str, ref_audio: str, ref_text: str, model_id: str) -> bool:
    try:
        with open(_meta_path(cache), encoding="utf-8") as f:
            meta = json.load(f)
        return (meta.get("model") == model_id
                and meta.get("ref_sha256") == _sha256(ref_audio)
                and meta.get("ref_text") == ref_text
                and meta.get("x_vector_only") is False)
    except Exception:
        return False


def _save_prompt(cache: str, prompt: dict, ref_audio: str, ref_text: str,
                 model_id: str) -> None:
    import torch

    os.makedirs(os.path.dirname(os.path.abspath(cache)) or ".", exist_ok=True)
    torch.save(prompt, cache)
    with open(_meta_path(cache), "w", encoding="utf-8") as f:
        json.dump({"model": model_id,
                   "ref_sha256": _sha256(ref_audio),
                   "ref_text": ref_text,
                   "x_vector_only": False}, f)


def _load_prompt(cache: str):
    import torch

    return torch.load(cache, map_location="cpu", weights_only=False)


def _serve(args) -> int:
    import gc
    import queue
    import threading

    import torch
    from qwen_tts import Qwen3TTSModel

    loaded: dict[str, object] = {}
    current = {"kind": ""}
    paths = {"base": args.base_model, "design": args.design_model}

    def ensure(kind: str):
        """Load the needed checkpoint, keeping ONE TTS model resident at a time.

        A cast is designed in one session (VoiceDesign stays loaded across every
        character); switching to Base happens once, before cloning lines.
        """
        if current["kind"] == kind and kind in loaded:
            return loaded[kind]
        loaded.clear()
        current["kind"] = ""
        gc.collect()
        torch.cuda.empty_cache()
        path = paths.get(kind) or ""
        if not path:
            raise RuntimeError(f"no {kind} model configured")
        loaded[kind] = Qwen3TTSModel.from_pretrained(
            path, device_map="cuda", dtype=torch.bfloat16,
            attn_implementation="sdpa")
        current["kind"] = kind
        return loaded[kind]

    def reply(obj: dict) -> None:
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    # Read stdin on a thread so an idle worker can exit and release VRAM between
    # pipeline phases instead of pinning ~4 GB forever.
    q: "queue.Queue[str]" = queue.Queue()

    def reader() -> None:
        for raw in sys.stdin:
            q.put(raw)
        q.put(None)  # EOF: let the worker exit immediately (no idle wait)

    threading.Thread(target=reader, daemon=True).start()

    while True:
        try:
            item = q.get(timeout=max(1, int(args.idle_timeout)))
        except queue.Empty:
            return 0
        if item is None:
            return 0
        line = item.strip()
        if not line:
            continue
        # Keep stdout clean for the protocol: model warnings/progress that go to
        # stdout are swallowed, the reply is the only line printed.
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                req = json.loads(line)
                op = req.get("op")
                if op not in ("clone", "design", "ping"):
                    raise ValueError(f"unknown op {op!r}")
                if op == "ping":
                    dur = 0.0
                elif op == "design":
                    m = ensure("design")
                    wavs, sr = m.generate_voice_design(
                        text=req["text"], instruct=req.get("instruct") or "",
                        language=req.get("language") or "English",
                        non_streaming_mode=True)
                    dur = _write_wav(wavs, sr, req["out"])
                else:
                    ref_audio = req["ref_audio"]
                    ref_text = req.get("ref_text") or ""
                    cache = req.get("cache") or ""
                    model_id = req.get("model") or args.base_model
                    if not ref_audio or not os.path.isfile(ref_audio):
                        raise FileNotFoundError(f"reference audio not found: {ref_audio}")
                    m = ensure("base")
                    prompt = None
                    if cache and _cache_valid(cache, ref_audio, ref_text, model_id):
                        try:
                            prompt = _load_prompt(cache)
                        except Exception:
                            prompt = None
                    if prompt is None:
                        # Keep the ITEM list (not the dict form): ICL needs each
                        # item's ref_text, which the dict conversion drops.
                        prompt = m.create_voice_clone_prompt(
                            ref_audio=ref_audio, ref_text=ref_text or None,
                            x_vector_only_mode=False)
                        if cache:
                            _save_prompt(cache, prompt, ref_audio, ref_text, model_id)
                    wavs, sr = m.generate_voice_clone(
                        text=req["text"], language=req.get("language") or "English",
                        voice_clone_prompt=prompt, non_streaming_mode=True)
                    dur = _write_wav(wavs, sr, req["out"])
            reply({"ok": True, "duration": round(float(dur), 3)})
        except Exception as exc:  # noqa: BLE001 - report, keep serving
            reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="")
    ap.add_argument("--voice-description", default="")
    ap.add_argument("--speaker", default="")
    ap.add_argument("--language", default="English")
    ap.add_argument("--model", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--sox-dir", default="")
    ap.add_argument("--max-new-tokens", type=int, default=2048)
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--base-model", default="")
    ap.add_argument("--design-model", default="")
    ap.add_argument("--idle-timeout", type=int, default=900)
    args = ap.parse_args()

    if args.sox_dir:
        os.environ["PATH"] = args.sox_dir + os.pathsep + os.environ.get("PATH", "")
        os.environ.setdefault("SOX_EXECUTABLE", os.path.join(args.sox_dir, "sox.exe"))

    if args.serve:
        return _serve(args)

    if not args.text or not args.out:
        ap.error("--text and --out are required unless --serve")

    import torch
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

    dur = _write_wav(wavs, sr, args.out)
    print(f"wrote {args.out}: {dur:.2f}s @ {sr}Hz", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
