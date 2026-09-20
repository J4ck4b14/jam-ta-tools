"""Host-independent texel-density statistics.

Hosts provide face samples with geometric area already converted to square
metres and UV area in normalized UV coordinates. This keeps Maya and Blender
unit systems from leaking into the shared calculations.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any, Dict, Iterable, List, Optional


def face_texel_density_px_per_m(world_area_m2: float, uv_area: float, texture_size: int) -> float:
    world_area_m2 = float(world_area_m2)
    uv_area = float(uv_area)
    texture_size = max(1, int(texture_size))
    if world_area_m2 <= 0.0 or uv_area <= 0.0:
        return 0.0
    return float(texture_size) * math.sqrt(uv_area / world_area_m2)


def summarize_texel_density(
    samples: Iterable[Dict[str, Any]],
    texture_size: int,
    target_px_per_m: Optional[float] = None,
    tolerance_percent: float = 15.0,
) -> Dict[str, Any]:
    prepared: List[Dict[str, Any]] = []
    total_world_area = 0.0
    total_uv_area = 0.0

    for raw in samples:
        world_area = float(raw.get("world_area_m2", 0.0))
        uv_area = float(raw.get("uv_area", 0.0))
        if world_area <= 0.0 or uv_area <= 0.0:
            continue
        density = face_texel_density_px_per_m(world_area, uv_area, texture_size)
        entry = dict(raw)
        entry["density_px_per_m"] = density
        prepared.append(entry)
        total_world_area += world_area
        total_uv_area += uv_area

    if not prepared:
        return {
            "sample_count": 0,
            "density_px_per_m": 0.0,
            "density_px_per_cm": 0.0,
            "min_px_per_m": 0.0,
            "max_px_per_m": 0.0,
            "median_px_per_m": 0.0,
            "spread_percent": 0.0,
            "target_px_per_m": float(target_px_per_m or 0.0),
            "tolerance_percent": float(tolerance_percent),
            "outlier_components": [],
            "samples": [],
        }

    densities = [entry["density_px_per_m"] for entry in prepared]
    aggregate = face_texel_density_px_per_m(total_world_area, total_uv_area, texture_size)
    med = float(median(densities))
    min_density = min(densities)
    max_density = max(densities)
    spread_percent = ((max_density - min_density) / med * 100.0) if med > 0.0 else 0.0

    target = float(target_px_per_m or 0.0)
    tolerance = max(0.0, float(tolerance_percent)) / 100.0
    outliers = []
    if target > 0.0:
        low = target * (1.0 - tolerance)
        high = target * (1.0 + tolerance)
        for entry in prepared:
            if entry["density_px_per_m"] < low or entry["density_px_per_m"] > high:
                outliers.append(entry.get("component"))

    return {
        "sample_count": len(prepared),
        "density_px_per_m": aggregate,
        "density_px_per_cm": aggregate / 100.0,
        "min_px_per_m": min_density,
        "max_px_per_m": max_density,
        "median_px_per_m": med,
        "spread_percent": spread_percent,
        "target_px_per_m": target,
        "tolerance_percent": float(tolerance_percent),
        "outlier_components": [component for component in outliers if component is not None],
        "samples": prepared,
    }


def texel_density_color(density_px_per_m: float, target_px_per_m: float):
    """Return a simple low/target/high RGB heat colour for viewport diagnostics."""
    density = max(0.0, float(density_px_per_m))
    target = max(1e-9, float(target_px_per_m))
    ratio = density / target
    low = (0.08, 0.28, 1.0)
    mid = (0.12, 0.90, 0.20)
    high = (1.0, 0.12, 0.06)

    if ratio <= 1.0:
        t = max(0.0, min(1.0, ratio))
        return tuple(low[i] + (mid[i] - low[i]) * t for i in range(3))

    t = max(0.0, min(1.0, ratio - 1.0))
    return tuple(mid[i] + (high[i] - mid[i]) * t for i in range(3))
