"""Host-independent render-vertex split analysis."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple


def _normalized_corner(corner: Dict[str, Any], uv_count: int) -> Tuple[int, Any, List[Any], int]:
    vertex = int(corner.get("vertex", -1))
    normal = corner.get("normal")
    uvs = list(corner.get("uvs") or [])
    if len(uvs) < uv_count:
        uvs.extend([None] * (uv_count - len(uvs)))
    material = int(corner.get("material", -1))
    return vertex, normal, uvs[:uv_count], material


def analyze_render_splits(
    corners: Iterable[Dict[str, Any]],
    uv_layer_names: Sequence[str] = (),
    include_material: bool = True,
) -> Dict[str, Any]:
    """Return progressive split counts from face-corner data.

    The result answers a more useful question than a single engine-vertex number:
    how many unique vertices exist after adding each vertex attribute to the key?
    Contributions are progressive, so the totals always add up exactly.
    """

    uv_names = list(uv_layer_names)
    records = [_normalized_corner(corner, len(uv_names)) for corner in corners]

    base_keys = {(vertex,) for vertex, _normal, _uvs, _material in records}
    normal_keys = {
        (vertex, normal)
        for vertex, normal, _uvs, _material in records
    }

    stages: List[Dict[str, Any]] = []
    previous_count = len(base_keys)
    after_normals = len(normal_keys)
    stages.append({
        "id": "normal",
        "label": "Normals / hard edges",
        "count": after_normals,
        "added": after_normals - previous_count,
    })
    previous_count = after_normals

    for uv_index, uv_name in enumerate(uv_names):
        keys = set()
        for vertex, normal, uvs, _material in records:
            key = [vertex, normal]
            key.extend(uvs[:uv_index + 1])
            keys.add(tuple(key))

        count = len(keys)
        stages.append({
            "id": "uv:{0}".format(uv_name),
            "label": "UV: {0}".format(uv_name),
            "count": count,
            "added": count - previous_count,
        })
        previous_count = count

    if include_material:
        material_keys = set()
        for vertex, normal, uvs, material in records:
            material_keys.add(tuple([vertex, normal] + list(uvs) + [material]))
        render_vertices = len(material_keys)
        stages.append({
            "id": "material",
            "label": "Material boundaries",
            "count": render_vertices,
            "added": render_vertices - previous_count,
        })
    else:
        render_vertices = previous_count

    base_vertices = len(base_keys)
    split_vertices = max(0, render_vertices - base_vertices)
    ratio = (float(render_vertices) / base_vertices) if base_vertices else 0.0

    for stage in stages:
        stage["share_of_extra"] = (
            float(stage.get("added", 0)) / split_vertices
            if split_vertices else 0.0
        )

    dominant_stage = None
    if stages:
        dominant_stage = max(stages, key=lambda stage: int(stage.get("added", 0)))
        if int(dominant_stage.get("added", 0)) <= 0:
            dominant_stage = None

    return {
        "base_vertices": base_vertices,
        "render_vertices": render_vertices,
        "extra_vertices": split_vertices,
        "split_ratio": ratio,
        "uv_layer_names": uv_names,
        "include_material": bool(include_material),
        "corner_count": len(records),
        "stages": stages,
        "dominant_stage": dict(dominant_stage) if dominant_stage else None,
    }
