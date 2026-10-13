"""MiniMax H3 duration grids.

Local ComfyUI (MiniMaxH3ReferenceToVideo / MiniMaxH3ImageToVideo) uses
the 17k+5 grid at 24fps (DESIGN.md §10.2). The cloud API uses integer
seconds 4..15. The Multishot pack works in raw frames (frames_per_shot)
and internally snaps to the local grid, so both are valid.

See: joeygambino/MiniMax-H3-Multishot-Workflow (frames_per_shot),
MiniMax H3 API docs (integer seconds), docs.comfy.org H3 tutorial.
"""
from __future__ import annotations

FPS = 24

# Local ComfyUI grid: 17k+5
GRID_MIN_K = 6      # 107 frames -> 4.458 s
GRID_MAX_K = 42     # 719 frames -> 29.958 s
ENVELOPE_MIN_S = 4.0
ENVELOPE_MAX_S = 30.0

# Cloud API / Multishot integer grid
H3_API_MIN_S = 4
H3_API_MAX_S = 15
H3_API_VALID_S = list(range(H3_API_MIN_S, H3_API_MAX_S + 1))  # 4..15 inclusive
# Multishot pack shipped defaults (frames at 24fps)
MULTISHOT_FRAMES_PER_SHOT_DEFAULT = 192  # 8s, k=11 grid point
MULTISHOT_FRAMES_PER_SHOT_PRESETS = {
    124: 5.167,   # k=7  (legacy insert)
    141: 5.875,
    158: 6.583,
    192: 8.0,     # k=11 (pack default)
    243: 10.125,  # k=14 (legacy standard)
    362: 15.083,  # near API max
}


def k_to_frames(k: int) -> int:
    return 17 * k + 5


def k_to_seconds(k: int) -> float:
    return k_to_frames(k) / FPS


def valid_durations() -> list[tuple[int, int, float]]:
    """[(k, frames, seconds)] for every grid value within the H3 envelope."""
    return [
        (k, k_to_frames(k), k_to_seconds(k))
        for k in range(GRID_MIN_K, GRID_MAX_K + 1)
    ]


def snap_duration(requested_s: float) -> tuple[int, int, float]:
    """Snap a requested duration to the nearest valid grid value.

    Returns (k, frames, exact_seconds). Outside the envelope, clamps.
    """
    if requested_s <= 0:
        requested_s = 10.125
    grid = valid_durations()
    best = min(grid, key=lambda item: abs(item[2] - requested_s))
    return best


def default_shot_seconds(short: bool = False) -> float:
    """k=14 (10.125s) for dialogue scenes; k=7 (5.167s) for inserts."""
    return 10.125 if not short else 5.167


def snap_duration_api(requested_s: float) -> tuple[int, float]:
    """Snap to H3 cloud API integer grid (4..15s).

    Returns (frames, seconds). Used for API parity checks; local ComfyUI
    still uses snap_duration() with the 17k+5 grid.
    """
    if requested_s <= 0:
        requested_s = 8
    clamped = max(H3_API_MIN_S, min(H3_API_MAX_S, round(requested_s)))
    return int(clamped * FPS), float(clamped)


def frames_to_seconds(frames: int) -> float:
    return frames / FPS


def seconds_to_frames(seconds: float) -> int:
    return int(round(seconds * FPS))
