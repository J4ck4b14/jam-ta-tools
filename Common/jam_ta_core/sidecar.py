"""Engine-facing metadata sidecars written next to exported models."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Iterable, Dict, Any

from .models import AssetReport
from .export_profiles import export_format, export_extension


def build_export_sidecar(
    export_file: str,
    reports: Iterable[AssetReport],
    source_host: str,
    profile_id: str,
    suite_version: str = "2.4.0",
    metadata: Dict[str, Any] | None = None,
    asset_set: Dict[str, Any] | None = None,
    export_profile_id: str | None = None,
) -> Dict[str, Any]:
    assets = []
    for report in reports:
        metrics = report.metrics
        assets.append({
            "name": report.object_name,
            "status": report.status,
            "error_count": report.error_count,
            "warning_count": report.warning_count,
            "metrics": {
                "model_vertices": int(metrics.get("model_vertices", 0)),
                "triangles": int(metrics.get("triangles", 0)),
                "render_vertices": int(metrics.get("render_vertices", 0)),
                "material_slots": int(metrics.get("material_slots", 0)),
                "uv_channels": int(metrics.get("uv_channels", 0)),
                "deform_bones": int(metrics.get("deform_bones", 0)),
                "max_skin_influences": int(metrics.get("max_skin_influences", 0)),
                "mesh_buffer_bytes": int(metrics.get("mesh_buffer_bytes", 0)),
                "vertex_stride_bytes": int(metrics.get("vertex_stride_bytes", 0)),
                "index_size_bytes": int(metrics.get("index_size_bytes", 0)),
                "texel_density_px_per_m": float(metrics.get("texel_density_px_per_m", 0.0)),
                "texel_density_spread_percent": float(metrics.get("texel_density_spread_percent", 0.0)),
                "texel_density_islands": int(metrics.get("texel_density_islands", 0)),
                "uv_island_count": int(metrics.get("uv_island_count", 0)),
                "uv_mirrored_islands": int(metrics.get("uv_mirrored_islands", 0)),
                "uv_overlap_faces": int(metrics.get("uv_overlap_faces", 0)),
                "uv_utilization_percent": float(metrics.get("uv_utilization_percent", 0.0)),
                "uv_min_padding_px": float(metrics.get("uv_min_padding_px", 0.0)),
                "lightmap_uv_channel": int(metrics.get("lightmap_uv_channel", -1)),
                "lightmap_overlap_faces": int(metrics.get("lightmap_overlap_faces", 0)),
                "lightmap_min_padding_px": float(metrics.get("lightmap_min_padding_px", 0.0)),
                "material_count": int(metrics.get("material_count", 0)),
                "texture_count": int(metrics.get("texture_count", 0)),
                "missing_texture_count": int(metrics.get("missing_texture_count", 0)),
                "udim_texture_count": int(metrics.get("udim_texture_count", 0)),
                "texture_reference_bytes": int(metrics.get("texture_reference_bytes", 0)),
                "texture_uncompressed_bytes": int(metrics.get("texture_uncompressed_bytes", 0)),
                "modular_grid_cm": float(metrics.get("modular_grid_cm", 0.0)),
                "modular_dimensions_cm": list(metrics.get("modular_dimensions_cm", [])),
                "modular_dimensions_on_grid": bool(metrics.get("modular_dimensions_on_grid", True)),
                "modular_position_on_grid": bool(metrics.get("modular_position_on_grid", True)),
            },
            "textures": [
                {
                    "name": str(texture.get("name", "")),
                    "source_name": os.path.basename(str(texture.get("path", "") or texture.get("name", ""))),
                    "semantic": str(texture.get("semantic", "unknown")),
                    "width": int(texture.get("width", 0)),
                    "height": int(texture.get("height", 0)),
                    "color_space": str(texture.get("color_space", "")),
                    "is_udim": bool(texture.get("is_udim", False)),
                    "udim_tiles": int(texture.get("udim_tiles", 1)),
                    "reference_bytes": int(texture.get("memory", {}).get("game_reference_bytes", 0)),
                }
                for texture in report.metadata.get("material_analysis", {}).get("textures", [])
            ],
            "issues": [
                {
                    "rule_id": issue.rule_id,
                    "severity": issue.severity,
                    "title": issue.title,
                    "category": issue.category,
                }
                for issue in report.sorted_issues()
                if issue.severity in {"ERROR", "WARNING"}
            ],
        })

    return {
        "schema": "jam-ta-tools.export-sidecar.v3",
        "suite_version": suite_version,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_host": source_host,
        "profile_id": profile_id,
        "export_profile_id": export_profile_id or "",
        "export_format": export_format(export_profile_id or "GENERIC_FBX"),
        "export_extension": export_extension(export_profile_id or "GENERIC_FBX"),
        "export_file": os.path.basename(export_file),
        "metadata": metadata or {},
        "asset_set": asset_set or {},
        "assets": assets,
    }


def sidecar_path_for_export(export_file: str) -> str:
    stem, _extension = os.path.splitext(export_file)
    return stem + ".jammeta.json"


def write_export_sidecar(
    export_file: str,
    reports: Iterable[AssetReport],
    source_host: str,
    profile_id: str,
    suite_version: str = "2.4.0",
    metadata: Dict[str, Any] | None = None,
    asset_set: Dict[str, Any] | None = None,
    export_profile_id: str | None = None,
) -> str:
    path = sidecar_path_for_export(export_file)
    document = build_export_sidecar(
        export_file,
        reports,
        source_host=source_host,
        profile_id=profile_id,
        suite_version=suite_version,
        metadata=metadata,
        asset_set=asset_set,
        export_profile_id=export_profile_id,
    )
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=False)
    return path
