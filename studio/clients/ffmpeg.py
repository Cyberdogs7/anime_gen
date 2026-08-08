"""ffmpeg/ffprobe helpers. See DESIGN.md §7.4 / Stage 7."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_FFMPEG: str | None = None
_FFPROBE: str | None = None


def _resolve() -> tuple[str, str]:
    """Resolve portable ffmpeg/ffprobe once (env.yaml path > STUDIO_FFMPEG > PATH)."""
    global _FFMPEG, _FFPROBE
    if _FFMPEG is not None:
        return _FFMPEG, _FFPROBE or "ffprobe"
    base = os.environ.get("STUDIO_FFMPEG", "")
    if not base:
        try:
            from ..config import get_config
            base = get_config().ffmpeg_bin() or ""
        except Exception:
            base = ""
    if base and base != "ffmpeg":
        p = Path(base)
        ffmpeg = str(p)
        ffprobe = str(p.with_name("ffprobe.exe")) if p.suffix.lower() == ".exe" else str(p.with_name("ffprobe"))
        if Path(ffprobe).exists():
            _FFPROBE = ffprobe
        _FFMPEG = ffmpeg
        return _FFMPEG, _FFPROBE or "ffprobe"
    _FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
    _FFPROBE = shutil.which("ffprobe") or "ffprobe"
    return _FFMPEG, _FFPROBE


def _run(args: list[str], probe: bool = False) -> subprocess.CompletedProcess:
    ffmpeg, ffprobe = _resolve()
    bin_path = ffprobe if probe else ffmpeg
    try:
        return subprocess.run([bin_path, *args], capture_output=True, text=True)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, returncode=127, stdout="", stderr="binary not found")


def ffmpeg_version() -> str | None:
    proc = _run(["-version"])
    if proc.returncode != 0:
        return None
    return proc.stdout.splitlines()[0].split(" ")[2] if proc.stdout else "?"


def ffprobe(path: Path | str) -> dict[str, Any] | None:
    """Return the JSON probe dict, or None if ffprobe is unavailable/fails."""
    proc = _run(["-v", "error", "-print_format", "json", "-show_format",
                 "-show_streams", str(path)], probe=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def duration_s(path: Path | str) -> float | None:
    info = ffprobe(path)
    if not info:
        return None
    return float(info.get("format", {}).get("duration", 0.0))


def normalize_loudness(path: Path | str, out_path: Path | str,
                       target_lufs: float = -16.0, tp: float = -1.5,
                       lra: float = 11.0) -> Path | None:
    """Two-pass EBU R128 loudness normalization of a video's audio.

    Pass 1 measures the integrated loudness, pass 2 applies a linear gain so the
    output sits at `target_lufs` with true-peak limiting (video stream is copied,
    audio re-encoded to AAC). Returns the output path, or None if ffmpeg failed.
    """
    import re as _re
    import tempfile
    path, out_path = Path(path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meas = _run([
        "-i", str(path),
        "-af", f"loudnorm=I={target_lufs}:TP={tp}:LRA={lra}:print_format=json",
        "-f", "null", "-",
    ])
    if meas.returncode != 0:
        return None
    m = _re.search(r"\{.*\}", meas.stderr, _re.S)
    if not m:
        return None
    try:
        stats = json.loads(m.group(0))
        args = (f"loudnorm=I={target_lufs}:TP={tp}:LRA={lra}:"
                f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
                f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
                f"linear=true")
    except (KeyError, json.JSONDecodeError):
        args = f"loudnorm=I={target_lufs}:TP={tp}:LRA={lra}"
    tmp = out_path.with_suffix(out_path.suffix + ".tmp.mp4")
    proc = _run(["-y", "-i", str(path), "-af", args, "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "192k", str(tmp)])
    if proc.returncode != 0 or not tmp.exists():
        if tmp.exists():
            tmp.unlink()
        return None
    if out_path.exists():
        out_path.unlink()
    tmp.rename(out_path)
    return out_path


def normalize_spliced_loudness(path: Path | str, out_path: Path | str,
                               splices_sec: list[float],
                               target_lufs: float = -16.0) -> Path | None:
    """Loudness-normalize each source segment of a spliced video to the same target.

    `splices_sec` are the timestamps between the source pieces (e.g. where a retake
    splices new content into a base video). Each segment is measured and normalized
    independently (so the pieces match instead of a single global gain), then re-joined
    and remuxed with the original video stream. Returns the output path or None.
    """
    info = ffprobe(path)
    if not info:
        return None
    dur = float(info.get("format", {}).get("duration", 0.0))
    tmp = Path(tempfile.mkdtemp(prefix="lnorm_"))
    try:
        full = tmp / "full.wav"
        if _run(["-y", "-i", str(path), "-map", "0:a:0", "-ar", "48000", "-ac", "2",
                 str(full)]).returncode != 0:
            return None
        bounds = [0.0] + [float(s) for s in splices_sec if 0.0 < float(s) < dur] + [dur]
        normed = []
        for i in range(len(bounds) - 1):
            a, b = bounds[i], bounds[i + 1]
            seg = tmp / f"seg{i}.wav"
            if _run(["-y", "-i", str(full), "-ss", str(a), "-t", str(b - a),
                     str(seg)]).returncode != 0:
                return None
            args = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
            meas = _run(["-i", str(seg), "-af", f"{args}:print_format=json",
                         "-f", "null", "-"])
            m = re.search(r"\{.*\}", meas.stderr or "", re.S)
            if m:
                try:
                    st = json.loads(m.group(0))
                    args += (f":measured_I={st['input_i']}:measured_TP={st['input_tp']}:"
                             f"measured_LRA={st['input_lra']}:"
                             f"measured_thresh={st['input_thresh']}:linear=true")
                except (KeyError, json.JSONDecodeError):
                    pass
            norm = tmp / f"seg{i}_n.wav"
            if _run(["-y", "-i", str(seg), "-af", args, "-ar", "48000", "-ac", "2",
                     str(norm)]).returncode != 0:
                return None
            normed.append(str(norm))
        merged = tmp / "merged.wav"
        cmd = ["-y"]
        for n in normed:
            cmd += ["-i", n]
        cmd += ["-filter_complex", f"concat=n={len(normed)}:v=0:a=1", str(merged)]
        if _run(cmd).returncode != 0:
            return None
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmpout = out_path.with_suffix(out_path.suffix + ".tmp.mp4")
        if _run(["-y", "-i", str(path), "-i", str(merged), "-map", "0:v", "-map", "1:a",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(tmpout)]).returncode != 0:
            return None
        if out_path.exists():
            out_path.unlink()
        tmpout.rename(out_path)
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def concat_clips(clip_paths: list[Path], out_path: Path) -> None:
    """Concat clips with the demuxer into out_path (no re-encode of video)."""
    list_file = out_path.with_suffix(".txt")
    list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in clip_paths), encoding="utf-8")
    proc = _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(out_path),
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {proc.stderr[-500:]}")
