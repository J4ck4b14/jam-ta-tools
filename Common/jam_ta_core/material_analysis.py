"""Host-independent material and texture analysis for game assets.

DCC adapters only gather facts. Naming heuristics and policy live here so Maya,
Blender and command-line validation do not quietly drift apart over time.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import ValidationIssue


SEMANTIC_ALIASES = {
    "base_color": ("basecolor", "base_color", "albedo", "diffuse", "diff", "color", "colour"),
    "normal": ("normal", "nrm", "nor"),
    "roughness": ("roughness", "rough", "rgh"),
    "metallic": ("metallic", "metalness", "metal", "mtl"),
    "ao": ("ambientocclusion", "ambient_occlusion", "occlusion", "ao"),
    "height": ("height", "displacement", "disp", "bump"),
    "opacity": ("opacity", "alpha", "transparency", "mask"),
    "emissive": ("emissive", "emission", "emit"),
    "specular": ("specular", "spec"),
    "gloss": ("glossiness", "gloss"),
    "packed_mask": ("orm", "rma", "mra", "arm", "maskmap", "mask_map"),
}

RAW_SEMANTICS = {
    "normal", "roughness", "metallic", "ao", "height", "opacity",
    "specular", "gloss", "packed_mask",
}
COLOR_SEMANTICS = {"base_color", "emissive"}

_UDIM_TOKEN_RE = re.compile(r"<udim>", re.IGNORECASE)
_UDIM_NUMBER_RE = re.compile(r"(?<!\d)(1\d{3})(?!\d)")
_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _tokens(value: str) -> List[str]:
    return [token for token in _TOKEN_RE.split(str(value).lower()) if token]


def infer_texture_semantic(*hints: str) -> str:
    """Infer a conventional texture role from names/plugs, conservatively."""
    text = "_".join(str(hint or "") for hint in hints).lower()
    collapsed = re.sub(r"[^a-z0-9]", "", text)
    token_set = set(_tokens(text))

    # Packed masks go first; otherwise `orm` can get eaten by a short alias.
    for semantic in ("packed_mask", "base_color", "normal", "roughness", "metallic", "ao",
                     "height", "opacity", "emissive", "specular", "gloss"):
        for alias in SEMANTIC_ALIASES[semantic]:
            compact = re.sub(r"[^a-z0-9]", "", alias.lower())
            if alias.lower() in token_set or (len(compact) >= 4 and compact in collapsed):
                return semantic
    return "unknown"


def detect_udim(path_or_name: str) -> bool:
    value = str(path_or_name or "")
    return bool(_UDIM_TOKEN_RE.search(value) or _UDIM_NUMBER_RE.search(os.path.basename(value)))


def is_power_of_two(value: int) -> bool:
    value = int(value)
    return value > 0 and (value & (value - 1)) == 0


def texture_set_key(path_or_name: str, semantic: str = "") -> str:
    """Best-effort key used only for consistency checks, never asset identity."""
    stem = os.path.splitext(os.path.basename(str(path_or_name or "")))[0].lower()
    stem = _UDIM_TOKEN_RE.sub("", stem)
    stem = _UDIM_NUMBER_RE.sub("", stem)

    aliases: Sequence[str] = ()
    if semantic in SEMANTIC_ALIASES:
        aliases = SEMANTIC_ALIASES[semantic]
    for alias in aliases:
        stem = re.sub(r"(^|[_\-.]){0}($|[_\-.])".format(re.escape(alias)), "_", stem, flags=re.IGNORECASE)

    stem = re.sub(r"[_\-.]+", "_", stem).strip("_")
    return stem or os.path.splitext(os.path.basename(str(path_or_name or "texture")))[0].lower()


def _mip_factor(width: int, height: int, mipmaps: bool) -> float:
    if not mipmaps or width <= 1 or height <= 1:
        return 1.0
    # 4/3 is exact for a long square chain only in the limit, which is good
    # enough for a reference budget. Avoid implying unsupported precision.
    return 4.0 / 3.0


def _block_compressed_bytes(width: int, height: int, bytes_per_block: int, mipmaps: bool) -> int:
    def level_bytes(w: int, h: int) -> int:
        blocks_x = max(1, int(math.ceil(float(w) / 4.0)))
        blocks_y = max(1, int(math.ceil(float(h) / 4.0)))
        return blocks_x * blocks_y * bytes_per_block

    total = 0
    w = max(1, int(width))
    h = max(1, int(height))
    while True:
        total += level_bytes(w, h)
        if not mipmaps or (w == 1 and h == 1):
            break
        w = max(1, w // 2)
        h = max(1, h // 2)
    return total


def estimate_texture_memory(
    width: int,
    height: int,
    semantic: str = "unknown",
    channels: int = 4,
    bits_per_channel: int = 8,
    mipmaps: bool = True,
    tile_count: int = 1,
) -> Dict[str, Any]:
    """Return deliberately explicit reference texture costs.

    `game_reference_bytes` assumes common desktop BC formats, not the actual
    engine import result. Engine bridges should measure the real thing later.
    """
    width = max(0, int(width))
    height = max(0, int(height))
    channels = max(1, int(channels or 4))
    bits_per_channel = max(1, int(bits_per_channel or 8))
    tile_count = max(1, int(tile_count or 1))

    if width <= 0 or height <= 0:
        return {
            "uncompressed_bytes": 0,
            "game_reference_bytes": 0,
            "reference_format": "unknown",
            "tile_count": tile_count,
        }

    bytes_per_pixel = channels * (bits_per_channel / 8.0)
    uncompressed = int(round(width * height * bytes_per_pixel * _mip_factor(width, height, mipmaps))) * tile_count

    # Keep semantic inference conservative to reduce false classifications.
    if semantic == "normal":
        reference_format = "BC5"
        game_bytes = _block_compressed_bytes(width, height, 16, mipmaps)
    elif semantic in {"ao", "roughness", "metallic", "height", "gloss"}:
        reference_format = "BC4"
        game_bytes = _block_compressed_bytes(width, height, 8, mipmaps)
    elif semantic == "packed_mask":
        reference_format = "BC7/BC3"
        game_bytes = _block_compressed_bytes(width, height, 16, mipmaps)
    else:
        reference_format = "BC7/BC3"
        game_bytes = _block_compressed_bytes(width, height, 16, mipmaps)

    return {
        "uncompressed_bytes": uncompressed,
        "game_reference_bytes": int(game_bytes) * tile_count,
        "reference_format": reference_format,
        "tile_count": tile_count,
    }


def _color_space_state(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    if "srgb" in text or "s-rgb" in text:
        return "srgb"
    if "raw" in text or "non-color" in text or "non color" in text or text in {"linear", "data"}:
        return "raw"
    return "other"


def _normalized_texture(raw: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(raw)
    semantic = str(item.get("semantic") or "").strip().lower()
    if not semantic or semantic == "unknown":
        semantic = infer_texture_semantic(
            item.get("semantic_hint", ""),
            item.get("name", ""),
            item.get("path", ""),
        )

    path = str(item.get("path") or "")
    tile_count = max(1, int(item.get("udim_tiles") or item.get("tile_count") or 1))
    is_udim = bool(item.get("is_udim")) or tile_count > 1 or detect_udim(path)
    if is_udim and tile_count < 1:
        tile_count = 1

    item.update({
        "name": str(item.get("name") or os.path.basename(path) or "Texture"),
        "path": path,
        "semantic": semantic,
        "width": max(0, int(item.get("width") or 0)),
        "height": max(0, int(item.get("height") or 0)),
        "channels": max(1, int(item.get("channels") or 4)),
        "bits_per_channel": max(1, int(item.get("bits_per_channel") or 8)),
        "color_space": str(item.get("color_space") or ""),
        "exists": bool(item.get("exists", True)),
        "used": bool(item.get("used", True)),
        "is_udim": is_udim,
        "udim_tiles": tile_count,
        "set_key": texture_set_key(path or item.get("name", ""), semantic),
    })
    item["memory"] = estimate_texture_memory(
        item["width"],
        item["height"],
        semantic=semantic,
        channels=item["channels"],
        bits_per_channel=item["bits_per_channel"],
        mipmaps=bool(item.get("mipmaps", True)),
        tile_count=tile_count,
    )
    return item


def analyze_material_inventory(
    materials: Iterable[Dict[str, Any]],
    profile: Optional[Dict[str, Any]] = None,
    object_name: str = "",
) -> Dict[str, Any]:
    """Analyze a host-collected material inventory and return issues + metrics."""
    profile = dict(profile or {})
    normalized_materials: List[Dict[str, Any]] = []
    issues: List[ValidationIssue] = []
    unique_textures: Dict[str, Dict[str, Any]] = {}

    missing_severity = str(profile.get("missing_texture_severity", "ERROR") or "ERROR")
    color_severity = str(profile.get("texture_color_space_severity", "WARNING") or "WARNING")
    npot_severity = str(profile.get("npot_texture_severity", "INFO") or "INFO")
    unused_severity = str(profile.get("unused_texture_severity", "INFO") or "INFO")
    max_dimension = profile.get("max_texture_dimension")
    max_per_material = profile.get("max_textures_per_material")
    texture_budget_mb = profile.get("texture_memory_budget_mb")
    prefer_mask_packing = bool(profile.get("prefer_mask_packing", False))

    for raw_material in materials:
        material = dict(raw_material)
        material_name = str(material.get("name") or "Material")
        textures = [_normalized_texture(tex) for tex in material.get("textures", [])]
        material["name"] = material_name
        material["textures"] = textures
        normalized_materials.append(material)

        if max_per_material is not None and len(textures) > int(max_per_material):
            issues.append(ValidationIssue(
                "material.texture_count",
                "High texture count",
                "{0} uses {1} texture node(s); profile target is at most {2}.".format(
                    material_name, len(textures), int(max_per_material)
                ),
                "Materials",
                "WARNING",
                object_name=object_name,
                metadata={"material": material_name, "count": len(textures)},
            ))

        semantics = {tex["semantic"] for tex in textures if tex.get("used")}
        if prefer_mask_packing and {"ao", "roughness", "metallic"}.issubset(semantics):
            issues.append(ValidationIssue(
                "material.mask_pack_candidate",
                "Mask packing opportunity",
                "{0} uses separate AO, roughness and metallic maps; this profile prefers packed masks.".format(material_name),
                "Materials",
                "INFO",
                object_name=object_name,
                metadata={"material": material_name, "channels": ["ao", "roughness", "metallic"]},
            ))

        set_dimensions: Dict[str, set] = {}
        for tex in textures:
            key = tex["path"] or "{0}:{1}".format(material_name, tex["name"])
            norm_key = os.path.normcase(os.path.abspath(key)) if tex["path"] else key.lower()
            unique_textures.setdefault(norm_key, tex)

            if not tex["exists"]:
                issues.append(ValidationIssue(
                    "texture.missing_source",
                    "Missing texture source",
                    "{0} references a missing texture: {1}.".format(material_name, tex["path"] or tex["name"]),
                    "Textures",
                    missing_severity,
                    object_name=object_name,
                    metadata={"material": material_name, "texture": tex["name"], "path": tex["path"]},
                ))

            if not tex["used"]:
                issues.append(ValidationIssue(
                    "texture.unused_node",
                    "Unused texture node",
                    "{0} contains an image texture that is not connected downstream: {1}.".format(material_name, tex["name"]),
                    "Textures",
                    unused_severity,
                    object_name=object_name,
                    metadata={"material": material_name, "texture": tex["name"]},
                ))

            width, height = tex["width"], tex["height"]
            if width > 0 and height > 0:
                if (not is_power_of_two(width)) or (not is_power_of_two(height)):
                    issues.append(ValidationIssue(
                        "texture.non_power_of_two",
                        "Non-power-of-two texture",
                        "{0} is {1}x{2}. NPOT may be intentional, so this profile treats it as {3}.".format(
                            tex["name"], width, height, npot_severity.lower()
                        ),
                        "Textures",
                        npot_severity,
                        object_name=object_name,
                        metadata={"material": material_name, "texture": tex["name"], "width": width, "height": height},
                    ))
                if max_dimension is not None and max(width, height) > int(max_dimension):
                    issues.append(ValidationIssue(
                        "texture.max_dimension",
                        "Texture exceeds profile resolution",
                        "{0} is {1}x{2}; profile maximum is {3}px.".format(
                            tex["name"], width, height, int(max_dimension)
                        ),
                        "Textures",
                        "WARNING",
                        object_name=object_name,
                        metadata={"material": material_name, "texture": tex["name"], "limit": int(max_dimension)},
                    ))
                set_dimensions.setdefault(tex["set_key"], set()).add((width, height))

            color_state = _color_space_state(tex["color_space"])
            if tex["semantic"] in RAW_SEMANTICS and color_state == "srgb":
                issues.append(ValidationIssue(
                    "texture.color_space_data",
                    "Data texture appears to use sRGB",
                    "{0} looks like a {1} map but its colour space is '{2}'.".format(
                        tex["name"], tex["semantic"], tex["color_space"]
                    ),
                    "Textures",
                    color_severity,
                    object_name=object_name,
                    metadata={"material": material_name, "texture": tex["name"], "semantic": tex["semantic"]},
                ))
            elif tex["semantic"] in COLOR_SEMANTICS and color_state == "raw":
                issues.append(ValidationIssue(
                    "texture.color_space_color",
                    "Colour texture appears to use raw/linear input",
                    "{0} looks like a {1} map but its colour space is '{2}'.".format(
                        tex["name"], tex["semantic"], tex["color_space"]
                    ),
                    "Textures",
                    color_severity,
                    object_name=object_name,
                    metadata={"material": material_name, "texture": tex["name"], "semantic": tex["semantic"]},
                ))

        for set_key, dimensions in set_dimensions.items():
            if len(dimensions) > 1:
                issues.append(ValidationIssue(
                    "texture.set_resolution_mismatch",
                    "Texture-set resolutions differ",
                    "{0} has mixed resolutions in texture set '{1}': {2}.".format(
                        material_name,
                        set_key,
                        ", ".join("{0}x{1}".format(*size) for size in sorted(dimensions)),
                    ),
                    "Textures",
                    "INFO",
                    object_name=object_name,
                    metadata={"material": material_name, "set_key": set_key, "dimensions": sorted(dimensions)},
                ))

    textures = list(unique_textures.values())
    texture_reference_bytes = sum(int(tex["memory"].get("game_reference_bytes", 0)) for tex in textures)
    texture_uncompressed_bytes = sum(int(tex["memory"].get("uncompressed_bytes", 0)) for tex in textures)
    missing_count = sum(1 for tex in textures if not tex["exists"])
    udim_sets = sum(1 for tex in textures if tex["is_udim"])

    if texture_budget_mb is not None:
        budget_bytes = int(float(texture_budget_mb) * 1024.0 * 1024.0)
        if texture_reference_bytes > budget_bytes:
            issues.append(ValidationIssue(
                "performance.texture_memory",
                "Reference texture budget exceeded",
                "Estimated game texture footprint is {0:.2f} MiB; profile budget is {1:.2f} MiB.".format(
                    texture_reference_bytes / (1024.0 * 1024.0), float(texture_budget_mb)
                ),
                "Performance",
                "WARNING",
                object_name=object_name,
                metadata={"bytes": texture_reference_bytes, "limit_bytes": budget_bytes},
            ))

    # `unique` means unique source assignment, not unique file contents. We are
    # File-content hashing is intentionally avoided during interactive analysis because it is expensive for large textures.
    metrics = {
        "material_count": len(normalized_materials),
        "texture_node_count": sum(len(material["textures"]) for material in normalized_materials),
        "texture_count": len(textures),
        "missing_texture_count": missing_count,
        "udim_texture_count": udim_sets,
        "texture_reference_bytes": texture_reference_bytes,
        "texture_uncompressed_bytes": texture_uncompressed_bytes,
    }

    return {
        "materials": normalized_materials,
        "textures": textures,
        "metrics": metrics,
        "issues": issues,
        "status": "ERROR" if any(issue.severity == "ERROR" for issue in issues) else (
            "WARNING" if any(issue.severity == "WARNING" for issue in issues) else "CLEAN"
        ),
    }
