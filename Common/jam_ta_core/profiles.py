"""Built-in asset profiles.

Profiles focus on validation semantics rather than assuming there is
one universal triangle budget for a category. Project-specific numeric limits can
be layered on top by the DCC host.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Dict, Iterable, Tuple

_BASE = {
    "description": "General game-ready mesh checks without project-specific assumptions.",
    "require_uvs": True,
    "require_uv_01": False,
    "flipped_uv_severity": "INFO",
    "open_boundary_severity": "INFO",
    "wire_edge_severity": "ERROR",
    "multiface_edge_severity": "ERROR",
    "non_contiguous_edge_severity": "ERROR",
    "negative_scale_severity": "ERROR",
    "non_unit_scale_severity": "WARNING",
    "zero_uv_area_severity": "ERROR",
    "zero_geo_area_severity": "ERROR",
    "lamina_severity": "ERROR",
    "max_material_slots": None,
    "max_skin_influences": None,
    "weight_sum_tolerance": 0.01,
    "unweighted_vertex_severity": "ERROR",
    "skin_influence_severity": "ERROR",
    "weight_normalization_severity": "WARNING",
    "asset_role": "generic",
    "missing_texture_severity": "ERROR",
    "texture_color_space_severity": "WARNING",
    "npot_texture_severity": "INFO",
    "unused_texture_severity": "INFO",
    "max_texture_dimension": None,
    "max_textures_per_material": None,
    "texture_memory_budget_mb": None,
    "prefer_mask_packing": False,
    "uv_overlap_severity": "INFO",
    "min_uv_utilization_percent": None,
    "uv_utilization_severity": "INFO",
    "audit_lightmap_uv": False,
    "lightmap_uv_channel": 1,
    "missing_lightmap_uv_severity": "INFO",
    "lightmap_overlap_severity": "WARNING",
    "min_lightmap_padding_px": 4.0,
    "lightmap_padding_severity": "INFO",
    "modular_grid_cm": None,
    "modular_grid_tolerance_cm": 0.1,
    "modular_check_position": False,
    "modular_dimension_severity": "WARNING",
    "modular_position_severity": "INFO",
}


def _profile(label: str, **overrides):
    data = deepcopy(_BASE)
    data.update(overrides)
    data["label"] = label
    return data


PROFILES: Dict[str, Dict[str, object]] = {
    "GENERIC_GAME": _profile(
        "Generic Game Asset",
        description="Balanced checks for props and environment meshes.",
    ),
    "UNREAL_STATIC": _profile(
        "Unreal Static Mesh",
        description="Static-mesh checks with Unreal-friendly transform and topology expectations.",
        asset_role="static",
        open_boundary_severity="INFO",
        audit_lightmap_uv=True,
    ),
    "UNITY_STATIC": _profile(
        "Unity Static Mesh",
        description="Static-mesh checks suitable for a conventional Unity import pipeline.",
        asset_role="static",
        open_boundary_severity="INFO",
        audit_lightmap_uv=True,
    ),
    "MODULAR_ENV": _profile(
        "Modular Environment",
        description="Allows intentional open boundaries and prioritizes clean transforms/UV data.",
        asset_role="static",
        open_boundary_severity="INFO",
        flipped_uv_severity="INFO",
        audit_lightmap_uv=True,
        modular_grid_cm=100.0,
        modular_check_position=True,
        modular_dimension_severity="WARNING",
        modular_position_severity="INFO",
    ),
    "VR_PROP": _profile(
        "VR Prop",
        description="Static prop profile intended to pair with stricter project budget overrides.",
        asset_role="static",
        open_boundary_severity="INFO",
        max_material_slots=4,
        max_texture_dimension=2048,
        max_textures_per_material=6,
        texture_memory_budget_mb=96.0,
        prefer_mask_packing=True,
    ),
    "HERO_CHARACTER": _profile(
        "Hero Character",
        description="Character-oriented profile; open borders can be intentional at seams.",
        asset_role="skeletal",
        open_boundary_severity="INFO",
        non_unit_scale_severity="ERROR",
        max_material_slots=8,
        max_skin_influences=4,
        max_texture_dimension=4096,
        max_textures_per_material=10,
        texture_memory_budget_mb=384.0,
    ),
    "STRICT_01_STATIC": _profile(
        "Strict 0-1 Static Mesh",
        description="Conservative static-mesh profile for assets that must stay in the 0-1 UV tile.",
        asset_role="static",
        require_uv_01=True,
        open_boundary_severity="WARNING",
        flipped_uv_severity="WARNING",
        max_material_slots=4,
        uv_overlap_severity="ERROR",
        audit_lightmap_uv=True,
        missing_lightmap_uv_severity="WARNING",
    ),
}


def get_profile(profile_id: str) -> Dict[str, object]:
    profile_id = profile_id if profile_id in PROFILES else "GENERIC_GAME"
    data = deepcopy(PROFILES[profile_id])
    data["id"] = profile_id
    return data


def profile_items() -> Iterable[Tuple[str, str, str]]:
    for profile_id, data in PROFILES.items():
        yield profile_id, str(data["label"]), str(data["description"])


_BUILTIN_PROFILE_IDS = tuple(PROFILES.keys())


def register_profile(profile_id: str, data: Dict[str, object], replace: bool = False) -> Dict[str, object]:
    profile_id = str(profile_id or "").strip().upper()
    if not profile_id:
        raise ValueError("Profile id cannot be empty")
    if profile_id in PROFILES and profile_id in _BUILTIN_PROFILE_IDS and not replace:
        raise ValueError("Cannot replace built-in profile without replace=True: {0}".format(profile_id))

    merged = deepcopy(_BASE)
    merged.update(dict(data or {}))
    merged.setdefault("label", profile_id.replace("_", " ").title())
    merged.setdefault("description", "Project profile loaded from JSON.")
    PROFILES[profile_id] = merged
    return get_profile(profile_id)


def clear_external_profiles() -> None:
    for profile_id in list(PROFILES.keys()):
        if profile_id not in _BUILTIN_PROFILE_IDS:
            PROFILES.pop(profile_id, None)


def load_profiles_json(path: str, replace_external: bool = True) -> Tuple[str, ...]:
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)

    payload = document.get("profiles", document) if isinstance(document, dict) else document
    if replace_external:
        clear_external_profiles()

    loaded = []
    if isinstance(payload, dict):
        records = payload.items()
    elif isinstance(payload, list):
        records = ((record.get("id", ""), record) for record in payload if isinstance(record, dict))
    else:
        raise ValueError("Profile JSON must contain a 'profiles' object/list")

    for profile_id, record in records:
        record = dict(record or {})
        record.pop("id", None)
        if not str(profile_id).strip():
            continue
        register_profile(profile_id, record, replace=True)
        loaded.append(str(profile_id).strip().upper())
    return tuple(loaded)
