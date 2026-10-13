"""Shot QC: STT-based dialogue verification for rendered clips (garbled-audio
detection).

H3 renders video AND audio jointly, and its known failure mode is synthesizing
gibberish speech or no speech at all instead of the script's lines. This module
transcribes each rendered clip with faster-whisper and compares the result to
the shot's EXPECTED dialogue (from the script). A clip whose transcription
doesn't match the intended lines — or that stays silent when lines are expected
— is flagged so a human (or the retake loop) can act on it.

Design notes:

- STT runs on **CPU** (int8), never on the GPU, so it can run inline while H3
  owns the GPU for the next shot without any VRAM contention.
- The whisper model is loaded **once per process** (lazily, keyed by
  model+device) and reused across shots — loading per shot would dominate.
- Comparison is word-level and fuzzy on purpose: whisper rarely transcribes a
  line verbatim, but it preserves the *words*. We measure, per expected line,
  the best ordered-window overlap against the transcript, then require a
  minimum fraction of lines covered.
- A silent shot must produce (nearly) no transcript; substantial unexpected
  words mean H3 invented speech and the shot is flagged too.
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from .config import get_config
from .show import Show

# Whisper model cache: (model, device, compute_type) -> WhisperModel
_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_MODEL_LOCK = threading.Lock()

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def normalize(text: str) -> str:
    """Lowercase, strip parenthetical notes + punctuation, collapse whitespace.

    ``(Grunting)``, ``(Internal)`` and other stage directions carry no spoken
    words and must not pollute the comparison.
    """
    body = re.sub(r"\([^)]*\)", "", text or "")
    return " ".join(_TOKEN_RE.findall(body.lower()))


def _tokens(text: str) -> list[str]:
    return normalize(text).split()


def _best_window_overlap(line: str, transcript: str) -> float:
    """Fraction of the line's words matched in ORDER anywhere in the transcript.

    Uses SequenceMatcher's matching blocks (order-preserving, non-crossing), so
    an insertion/drop in the transcription doesn't defeat the match and extra
    transcript words don't dilute the score. Scrambled order scores much lower
    than an in-order match. 1.0 = every line word matched in order.
    """
    import difflib
    lt = _tokens(line)
    if not lt:
        return 1.0
    tt = _tokens(transcript)
    if not tt:
        return 0.0
    matched = sum(blk.size for blk in
                  difflib.SequenceMatcher(None, lt, tt).get_matching_blocks())
    return min(1.0, matched / len(lt))


def _explained_fraction(lines: list[str], transcript: str) -> float:
    """Fraction of the transcript's words covered by any expected line's matched
    blocks. Low = H3 padded the clip with invented/garbled speech (the prompt
    guarantees no ambience chatter, so unexplained words are exactly that)."""
    import difflib
    tt = _tokens(transcript)
    if not tt:
        return 1.0
    covered: set[int] = set()
    for line in lines:
        lt = _tokens(line)
        if not lt:
            continue
        for blk in difflib.SequenceMatcher(None, lt, tt).get_matching_blocks():
            if blk.size:
                covered.update(range(blk.b, blk.b + blk.size))
    return len(covered) / len(tt)


def verify_dialogue(shot: dict[str, Any], transcript: str,
                    min_line_sim: float = 0.55,
                    min_lines_covered: float = 0.5,
                    min_explained: float = 0.45) -> dict[str, Any]:
    """Compare the transcribed audio to the shot's expected spoken lines.

    Returns a report dict:
      {
        "verdict": "pass" | "fail" | "skip",
        "score": 0.0-1.0,
        "expected": [normalized lines...],
        "transcript": raw transcript text,
        "line_matches": [{line, score}...],
        "explained": 0.0-1.0,
        "issues": [reason...],
        "silent_shot": bool,
      }

    verdict semantics:
      - "skip": shot has no expected spoken lines AND the transcript is empty —
        nothing to verify.
      - "pass": the required fraction of expected lines match the transcript,
        AND the transcript is largely explained by those lines (no invented
        speech padding).
      - "fail": garbled / missing / invented speech.
    """
    lines = [normalize(d.get("line") or "")
             for d in (shot.get("dialogue") or [])
             if _has_speech(d.get("line") or "")]
    lines = [l for l in lines if l]
    spoken_tokens = sum(len(_tokens(l)) for l in lines)
    transcript_norm = normalize(transcript)
    transcript_words = _tokens(transcript_norm)
    issues: list[str] = []
    silent_shot = not lines

    if silent_shot:
        if len(transcript_words) >= 2:
            return {
                "verdict": "fail", "score": 0.0, "expected": [], "transcript": transcript,
                "line_matches": [], "silent_shot": True, "explained": 0.0,
                "issues": ["silent shot has unexpected speech "
                           f"({len(transcript_words)} words)"],
            }
        return {"verdict": "skip", "score": 1.0, "expected": [], "transcript": transcript,
                "line_matches": [], "silent_shot": True, "explained": 1.0, "issues": []}

    if spoken_tokens and not transcript_words:
        issues.append("shot has dialogue but the clip is silent / no speech transcribed")

    matches = [{"line": l, "score": _best_window_overlap(l, transcript_norm)} for l in lines]
    covered = sum(1 for m in matches if m["score"] >= min_line_sim)
    fraction = covered / len(lines) if lines else 1.0
    explained = _explained_fraction(lines, transcript_norm) if transcript_words else 0.0
    # Score is the weaker of the two axes: lines present AND nothing extra.
    score = min(fraction, explained) if transcript_words else fraction
    if fraction < min_lines_covered:
        issues.append(f"only {covered}/{len(lines)} expected lines matched "
                      f"the transcription ({fraction:.0%} < "
                      f"{min_lines_covered:.0%})")
    if explained < min_explained:
        issues.append(f"{explained:.0%} of the transcript is unexplained speech "
                      f"(invented/garbled audio; < {min_explained:.0%} expected)")
    verdict = "pass" if not issues else "fail"
    return {"verdict": verdict, "score": score, "expected": lines,
            "transcript": transcript, "line_matches": matches,
            "silent_shot": False, "explained": explained, "issues": issues}


def _has_speech(line: str) -> bool:
    body = re.sub(r"\([^)]*\)", "", line or "")
    return bool(re.search(r"\w", body))


def extract_audio(clip: Path | str, cfg=None) -> str | None:
    """Extract mono 16 kHz WAV from the clip (whisper's preferred input)."""
    import subprocess
    cfg = cfg or get_config()
    ffmpeg = cfg.ffmpeg_bin()
    tmpdir = tempfile.mkdtemp(prefix="qc_")
    wav = str(Path(tmpdir) / "audio.wav")
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(clip),
             "-ac", "1", "-ar", "16000", str(wav)],
            capture_output=True, timeout=300)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        log.warning("audio extraction failed for %s", clip, exc_info=True)
        return None
    if proc.returncode != 0 or not Path(wav).exists():
        log.warning("ffmpeg audio extract returned %d for %s: %s",
                    proc.returncode, clip, proc.stderr[-300:])
        return None
    return wav


def _load_model(cfg=None) -> Any:
    """Lazily load + cache a faster-whisper model (once per (model, device, ct))."""
    cfg = cfg or get_config()
    stt = cfg.get("qc", "stt", {})
    model_name = stt.get("model", "small") or "small"
    device = stt.get("device", "cpu") or "cpu"
    compute_type = stt.get("compute_type", "int8") or "int8"
    key = (model_name, device, compute_type)
    with _MODEL_LOCK:
        if key in _MODEL_CACHE:
            return _MODEL_CACHE[key]
        from faster_whisper import WhisperModel
        log.info("[qc] loading faster-whisper %s (%s, %s)…",
                 model_name, device, compute_type)
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _MODEL_CACHE[key] = model
        return model


def transcribe(wav: str | Path, cfg=None) -> list[dict[str, Any]]:
    """Transcribe a WAV and return segments [{start, end, text}].

    First call downloads the model (once, into the HF cache). Returns [] on
    failure so QC degrades gracefully instead of failing the render.
    """
    cfg = cfg or get_config()
    stt = cfg.get("qc", "stt", {})
    language = stt.get("language", "en") or None
    try:
        model = _load_model(cfg)
        segments, _info = model.transcribe(str(wav), language=language,
                                           beam_size=1, vad_filter=True)
        out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
               for s in segments]
        return out
    except Exception as exc:
        log.warning("[qc] transcription failed: %s", exc, exc_info=True)
        return []


def _qc_dir(show: Show, episode: int) -> Path:
    return show.dir / "runs" / f"EP{episode:02d}" / "qc"


def _qc_summary(show: Show, episode: int) -> dict[str, Any]:
    p = _qc_dir(show, episode) / "summary.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    return {}


def _save_summary(show: Show, episode: int, entry: dict[str, Any]) -> None:
    d = _qc_dir(show, episode)
    d.mkdir(parents=True, exist_ok=True)
    summary = _qc_summary(show, episode)
    summary[entry["shot_id"]] = entry
    (d / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


def run_shot_qc(show: Show, episode: int, clip_path: Path, shot: dict[str, Any],
                cfg=None) -> dict[str, Any]:
    """Transcribe one rendered clip and verify it against the shot's dialogue.

    Extracts the audio to a temp WAV, runs faster-whisper on CPU, compares
    against the script, and persists the per-shot report to
    runs/EP##/qc/summary.json + a qc_results DB row. Never raises: on any STT
    failure it records a ``fail`` report so a silent/garbled clip is visible.
    Returns the report dict.
    """
    cfg = cfg or get_config()
    stt = cfg.get("qc", "stt", {})
    sid = shot.get("id", "shot")
    report = {
        "shot_id": sid,
        "verdict": "fail", "score": 0.0,
        "expected": [], "transcript": "", "line_matches": [],
        "silent_shot": False, "issues": ["STT unavailable"], "ts": time.time(),
    }
    if not stt.get("enabled", True):
        return report
    try:
        wav = extract_audio(clip_path, cfg)
        if not wav:
            report["issues"] = ["could not extract audio from clip"]
        else:
            try:
                segments = transcribe(wav, cfg)
                text = " ".join(s["text"] for s in segments if s.get("text"))
                report = verify_dialogue(
                    shot, text,
                    min_line_sim=float(stt.get("min_line_sim", 0.55) or 0.55),
                    min_lines_covered=float(stt.get("min_lines_covered", 0.5) or 0.5),
                    min_explained=float(stt.get("min_explained", 0.45) or 0.45))
                report["shot_id"] = sid
                report["ts"] = time.time()
            finally:
                import shutil
                shutil.rmtree(Path(wav).parent, ignore_errors=True)
    except Exception as exc:
        report["issues"] = [f"STT failed: {exc}"]
        log.warning("[qc] shot QC failed for %s: %s", sid, exc, exc_info=True)

    _save_summary(show, episode, report)
    _persist_db(show, sid, report)
    return report


def _persist_db(show: Show, sid: str, report: dict[str, Any]) -> None:
    try:
        from .db import open_db
        db = open_db()
        db.conn.execute(
            "INSERT OR REPLACE INTO qc_results(id, shot_id, checks_json, composite, "
            "verdict, passed, created_at) VALUES(?,?,?,?,?,?,datetime('now'))",
            (f"{show.show_id}:{sid}", sid,
             json.dumps({k: v for k, v in report.items()
                         if k not in ("shot_id", "ts")}),
             float(report.get("score") or 0.0),
             report.get("verdict", "fail"),
             1 if report.get("verdict") == "pass" else 0))
        db.conn.commit()
        db.close()
    except Exception:
        log.debug("[qc] db persist failed", exc_info=True)


def clear_shot_qc(show: Show, episode: int, sid: str) -> None:
    """Drop a shot's stored QC report (used when a rejected clip is re-rendered).

    Removes the shot from runs/EP##/qc/summary.json and deletes its qc_results
    row so a re-render doesn't show the stale verdict. Never raises.
    """
    try:
        summary = _qc_summary(show, episode)
        if sid in summary:
            summary.pop(sid)
            (_qc_dir(show, episode) / "summary.json").write_text(
                json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.debug("[qc] clear summary failed for %s", sid, exc_info=True)
    try:
        from .db import open_db
        db = open_db()
        db.conn.execute("DELETE FROM qc_results WHERE shot_id = ?", (sid,))
        db.conn.commit()
        db.close()
    except Exception:
        log.debug("[qc] db clear failed for %s", sid, exc_info=True)


def episode_qc_summary(show: Show, episode: int) -> dict[str, Any]:
    """Roll-up counts over every shot's stored QC report."""
    summary = _qc_summary(show, episode)
    counts = {"pass": 0, "fail": 0, "skip": 0, "total": 0}
    for entry in summary.values():
        v = entry.get("verdict", "fail")
        counts["total"] += 1
        if v in counts:
            counts[v] += 1
        else:
            counts["fail"] += 1
    return {"counts": counts, "shots": summary}
