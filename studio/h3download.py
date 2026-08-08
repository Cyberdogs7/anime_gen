"""H3 model download tracking (node A / D:\\anime-h3)."""
from __future__ import annotations

from pathlib import Path

MODELS_DIR = Path("D:/anime-h3/ComfyUI/models")

# Expected sizes (GB) for the int8_convrot checkpoints ComfyUI loads.
EXPECTED_GB = {
    "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors": 19.53,
    "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors": 18.31,
    "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors": 23.70,
    "vae/minimax_h3_video_vae_fp16.safetensors": 4.84,
    "vae/minimax_h3_audio_vae_fp32.safetensors": 0.577,
}

ETA_LOGS = [
    Path(r"C:\Users\Chad\AppData\Local\Temp\opencode\i8_1.log"),
    Path(r"C:\Users\Chad\AppData\Local\Temp\opencode\i8_2.log"),
    Path(r"C:\Users\Chad\AppData\Local\Temp\opencode\i8_3.log"),
]


def _last_eta() -> str:
    for log in ETA_LOGS:
        if log.exists():
            for line in reversed(log.read_text(encoding="utf-8", errors="replace").splitlines()):
                parts = line.split()
                # curl default columns end: ... time-total time-spent time-left speed
                if len(parts) >= 11 and parts[0].rstrip("%").isdigit():
                    eta = parts[-2]
                    if ":" in eta and eta != "--:--":
                        return eta
    return "?"


def download_status() -> tuple[list[str], bool, float]:
    lines: list[str] = []
    total_pct = 0.0
    count = 0
    for rel, expected_gb in EXPECTED_GB.items():
        f = MODELS_DIR / rel
        if f.exists():
            cur_gb = f.stat().st_size / (1024**3)
        else:
            cur_gb = 0.0
        pct = min(100.0, cur_gb / expected_gb * 100)
        total_pct += pct
        count += 1
        state = "DONE" if pct >= 99.9 else ("downloading" if pct > 0 else "queued")
        lines.append(f"[{'DONE' if state=='DONE' else ' ...'}] {rel.split('/')[-1]:60s} "
                     f"{cur_gb:6.2f}/{expected_gb:6.2f} GB  {pct:5.1f}%  {state}")
    overall = total_pct / count if count else 0.0
    done = overall >= 99.9
    if not done:
        eta = _last_eta()
        lines.append(f"ETA (largest file): {eta}")
    return lines, done, overall
