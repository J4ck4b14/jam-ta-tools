"""Small, explicit export presets shared by DCC integrations.

The point is reproducibility, not hiding every FBX switch behind a magic name.
Host adapters still own the actual exporter API calls.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Dict, Iterable, Tuple


EXPORT_PROFILES: Dict[str, Dict[str, object]] = {
    "GENERIC_FBX": {
        "label": "Generic FBX",
        "format": "FBX",
        "extension": ".fbx",
        "description": "Meshes, empties/helpers and armatures; no baked animation.",
        "include_meshes": True,
        "include_helpers": True,
        "include_armatures": True,
        "include_animation": False,
        "export_tangents": True,
        "export_smoothing": True,
        "triangulate": False,
        "embed_textures": False,
    },
    "UNITY_STATIC": {
        "label": "Unity Static",
        "format": "FBX",
        "extension": ".fbx",
        "description": "Static meshes/helpers for a conventional Unity model import.",
        "include_meshes": True,
        "include_helpers": True,
        "include_armatures": False,
        "include_animation": False,
        "export_tangents": True,
        "export_smoothing": True,
        "triangulate": False,
        "embed_textures": False,
    },
    "UNITY_RIGGED": {
        "label": "Unity Rigged",
        "format": "FBX",
        "extension": ".fbx",
        "description": "Meshes + armatures, with animation export enabled.",
        "include_meshes": True,
        "include_helpers": True,
        "include_armatures": True,
        "include_animation": True,
        "export_tangents": True,
        "export_smoothing": True,
        "triangulate": False,
        "embed_textures": False,
    },
    "UNREAL_STATIC": {
        "label": "Unreal Static",
        "format": "FBX",
        "extension": ".fbx",
        "description": "Static meshes/helpers; Unreal collision/socket members can travel with the set.",
        "include_meshes": True,
        "include_helpers": True,
        "include_armatures": False,
        "include_animation": False,
        "export_tangents": True,
        "export_smoothing": True,
        "triangulate": False,
        "embed_textures": False,
    },
    "UNREAL_SKELETAL": {
        "label": "Unreal Skeletal",
        "format": "FBX",
        "extension": ".fbx",
        "description": "Meshes + skeleton/armature with animation export enabled.",
        "include_meshes": True,
        "include_helpers": True,
        "include_armatures": True,
        "include_animation": True,
        "export_tangents": True,
        "export_smoothing": True,
        "triangulate": False,
        "embed_textures": False,
    },
    "GENERIC_OBJ": {
        "label": "Generic OBJ",
        "format": "OBJ",
        "extension": ".obj",
        "description": "Static mesh interchange; no skeleton, sockets or animation semantics.",
        "include_meshes": True,
        "include_helpers": False,
        "include_armatures": False,
        "include_animation": False,
        "export_tangents": False,
        "export_smoothing": True,
        "triangulate": False,
        "embed_textures": False,
    },
    "GENERIC_GLTF": {
        "label": "Generic glTF / GLB",
        "format": "GLB",
        "extension": ".glb",
        "description": "Compact glTF binary export where the host provides a glTF exporter.",
        "include_meshes": True,
        "include_helpers": True,
        "include_armatures": True,
        "include_animation": True,
        "export_tangents": True,
        "export_smoothing": True,
        "triangulate": False,
        "embed_textures": True,
    },
    "GENERIC_USD": {
        "label": "Generic USD",
        "format": "USD",
        "extension": ".usd",
        "description": "USD interchange export; availability depends on the DCC's USD support/plugin.",
        "include_meshes": True,
        "include_helpers": True,
        "include_armatures": True,
        "include_animation": True,
        "export_tangents": False,
        "export_smoothing": True,
        "triangulate": False,
        "embed_textures": False,
    },
}


def export_extension(profile_id: str) -> str:
    profile = get_export_profile(profile_id)
    return str(profile.get("extension") or ".fbx")


def export_format(profile_id: str) -> str:
    profile = get_export_profile(profile_id)
    return str(profile.get("format") or "FBX").upper()


def get_export_profile(profile_id: str) -> Dict[str, object]:
    profile_id = profile_id if profile_id in EXPORT_PROFILES else "GENERIC_FBX"
    data = deepcopy(EXPORT_PROFILES[profile_id])
    data["id"] = profile_id
    return data


def export_profile_items() -> Iterable[Tuple[str, str, str]]:
    for profile_id, data in EXPORT_PROFILES.items():
        yield profile_id, str(data["label"]), str(data["description"])
