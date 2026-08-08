"""MiniMax H3 duration grid (17k + 5 frames @ 24 fps). See DESIGN.md §10.2."""
from __future__ import annotations

FPS = 24
GRID_MIN_K = 6      # 107 frames -> 4.458 s
GRID_MAX_K = 20     # 345 frames -> 14.375 s
ENVELOPE_MIN_S = 4.0
ENVELOPE_MAX_S = 15.0


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
