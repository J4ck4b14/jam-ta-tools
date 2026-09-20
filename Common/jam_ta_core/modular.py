"""Small host-independent helpers for modular-kit placement and bounds checks.

Everything here works in centimetres on purpose. Blender/Maya adapters do the
unit conversion once at the boundary, which keeps project rules predictable.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, ceil
from typing import Dict, Iterable, List, Sequence, Tuple


AXES = ("X", "Y", "Z")


@dataclass(frozen=True)
class Bounds3:
    minimum: Tuple[float, float, float]
    maximum: Tuple[float, float, float]

    @property
    def dimensions(self) -> Tuple[float, float, float]:
        return tuple(max(0.0, self.maximum[i] - self.minimum[i]) for i in range(3))

    @property
    def center(self) -> Tuple[float, float, float]:
        return tuple((self.minimum[i] + self.maximum[i]) * 0.5 for i in range(3))


def snap_scalar(value: float, step: float, mode: str = "NEAREST") -> float:
    """Snap one scalar to a positive step."""
    step = abs(float(step))
    if step <= 1e-12:
        return float(value)

    scaled = float(value) / step
    mode = str(mode or "NEAREST").upper()
    if mode == "FLOOR":
        snapped = floor(scaled)
    elif mode == "CEIL":
        snapped = ceil(scaled)
    else:
        # round() is banker's rounding; kit placement generally wants .5 away
        # from zero instead. This keeps snapping symmetric around negative grid cells.
        snapped = floor(scaled + 0.5) if scaled >= 0.0 else ceil(scaled - 0.5)
    return float(snapped) * step


def snap_vector(values: Sequence[float], step: float, axes: Sequence[bool] = (True, True, True), mode: str = "NEAREST") -> Tuple[float, float, float]:
    source = tuple(float(v) for v in values[:3])
    enabled = tuple(bool(v) for v in axes[:3])
    return tuple(snap_scalar(source[i], step, mode) if enabled[i] else source[i] for i in range(3))


def multiple_delta(value: float, step: float) -> float:
    """Absolute distance to the nearest grid multiple."""
    step = abs(float(step))
    if step <= 1e-12:
        return 0.0
    return abs(float(value) - snap_scalar(float(value), step, "NEAREST"))


def analyze_modular_bounds(
    minimum_cm: Sequence[float],
    maximum_cm: Sequence[float],
    grid_cm: float,
    tolerance_cm: float = 0.1,
    check_position: bool = True,
) -> Dict[str, object]:
    """Check whether dimensions (and optionally bounds) conform to a kit grid."""
    bounds = Bounds3(tuple(map(float, minimum_cm[:3])), tuple(map(float, maximum_cm[:3])))
    step = abs(float(grid_cm))
    tolerance = max(0.0, float(tolerance_cm))

    dimension_errors: List[str] = []
    position_errors: List[str] = []
    axis_metrics: Dict[str, Dict[str, float | bool]] = {}

    for index, axis in enumerate(AXES):
        dimension = bounds.dimensions[index]
        dim_delta = multiple_delta(dimension, step)
        min_delta = multiple_delta(bounds.minimum[index], step)
        max_delta = multiple_delta(bounds.maximum[index], step)
        dim_ok = dim_delta <= tolerance
        min_ok = min_delta <= tolerance
        max_ok = max_delta <= tolerance

        if not dim_ok:
            dimension_errors.append(axis)
        if check_position and (not min_ok or not max_ok):
            position_errors.append(axis)

        axis_metrics[axis] = {
            "dimension_cm": dimension,
            "dimension_delta_cm": dim_delta,
            "minimum_cm": bounds.minimum[index],
            "minimum_delta_cm": min_delta,
            "maximum_cm": bounds.maximum[index],
            "maximum_delta_cm": max_delta,
            "dimension_on_grid": dim_ok,
            "minimum_on_grid": min_ok,
            "maximum_on_grid": max_ok,
        }

    return {
        "grid_cm": step,
        "tolerance_cm": tolerance,
        "minimum_cm": bounds.minimum,
        "maximum_cm": bounds.maximum,
        "dimensions_cm": bounds.dimensions,
        "center_cm": bounds.center,
        "dimension_error_axes": dimension_errors,
        "position_error_axes": position_errors,
        "dimensions_on_grid": not dimension_errors,
        "position_on_grid": not position_errors,
        "is_modular": not dimension_errors and (not check_position or not position_errors),
        "axes": axis_metrics,
    }


def bounds_scale_factors(
    source_dimensions: Sequence[float],
    target_dimensions: Sequence[float],
    axes: Sequence[bool] = (True, True, True),
    epsilon: float = 1e-9,
) -> Tuple[float, float, float]:
    """Scale ratios needed to match one axis-aligned AABB to another."""
    source = tuple(abs(float(v)) for v in source_dimensions[:3])
    target = tuple(abs(float(v)) for v in target_dimensions[:3])
    enabled = tuple(bool(v) for v in axes[:3])
    factors = []
    for index in range(3):
        if not enabled[index]:
            factors.append(1.0)
        elif source[index] <= epsilon:
            # A flat dimension cannot be recovered by scale alone. Leave it as-is
            # instead of feeding infinity into the DCC transform.
            factors.append(1.0)
        else:
            factors.append(target[index] / source[index])
    return tuple(factors)


def bounds_anchor(minimum: Sequence[float], maximum: Sequence[float], anchor: str) -> Tuple[float, float, float]:
    """Return a common bounds anchor such as CENTER, BOTTOM or a named corner."""
    lo = tuple(map(float, minimum[:3]))
    hi = tuple(map(float, maximum[:3]))
    mid = tuple((lo[i] + hi[i]) * 0.5 for i in range(3))
    key = str(anchor or "CENTER").upper()

    presets = {
        "CENTER": mid,
        "BOTTOM": (mid[0], mid[1], lo[2]),
        "TOP": (mid[0], mid[1], hi[2]),
        "MIN": lo,
        "MAX": hi,
        "X_MIN": (lo[0], mid[1], mid[2]),
        "X_MAX": (hi[0], mid[1], mid[2]),
        "Y_MIN": (mid[0], lo[1], mid[2]),
        "Y_MAX": (mid[0], hi[1], mid[2]),
        "Z_MIN": (mid[0], mid[1], lo[2]),
        "Z_MAX": (mid[0], mid[1], hi[2]),
    }
    return presets.get(key, mid)


def make_attachment_name(root_name: str, label: str, role: str = "SOCKET") -> str:
    """Build a predictable attachment/helper name without trying to police studio naming."""
    prefix = "SOCKET" if str(role).upper() == "SOCKET" else "HELP"
    root = "_".join(part for part in str(root_name).strip().replace(" ", "_").split("_") if part) or "Asset"
    clean_label = "_".join(part for part in str(label).strip().replace(" ", "_").split("_") if part) or "Attachment"
    return f"{prefix}_{root}_{clean_label}"
