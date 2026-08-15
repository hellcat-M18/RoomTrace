from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .model import FrameRecord


@dataclass
class PoseRefinement:
    applied: bool
    method: str
    translation_correction_m: float = 0.0
    note: str = ""


def apply_loop_closure(frames: list[FrameRecord], *, enabled: bool = False) -> PoseRefinement:
    """Apply a conservative positional loop correction when explicitly requested.

    A capture may end somewhere other than its start, so automatically forcing
    the endpoints together would be unsafe. The Android guide asks the user to
    return to the start; the CLI therefore exposes this as an explicit option.
    Orientation remains untouched because a linear positional correction is
    less destructive than inventing a rotational interpolation.
    """
    if not enabled or len(frames) < 3:
        return PoseRefinement(False, "arcore_initial", note="loop closure not requested")
    start = frames[0].position.copy()
    end = frames[-1].position.copy()
    delta = start - end
    distance = float(np.linalg.norm(delta))
    if distance < 1e-4:
        return PoseRefinement(False, "arcore_initial", note="endpoints already coincide")
    for index, frame in enumerate(frames):
        weight = index / max(1, len(frames) - 1)
        frame.pose_c2w[:3, 3] += delta * weight
    return PoseRefinement(True, "linear_loop_closure", distance, "positional correction distributed over the trajectory")

