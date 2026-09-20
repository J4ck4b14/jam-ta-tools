"""Host-independent mesh memory estimates.

These numbers are deliberately labelled estimates. DCCs do not know the exact
runtime vertex declaration, compression, platform index format or engine-side
repacking that will be used after import. The reference layout gives artists a
stable way to compare assets before they reach the engine.
"""

from __future__ import annotations

from typing import Any, Dict


def estimate_mesh_memory(
    render_vertices: int,
    triangles: int,
    uv_channels: int = 1,
    has_skinning: bool = False,
    color_channels: int = 0,
    include_tangents: bool = True,
) -> Dict[str, Any]:
    render_vertices = max(0, int(render_vertices))
    triangles = max(0, int(triangles))
    uv_channels = max(0, int(uv_channels))
    color_channels = max(0, int(color_channels))

    # Reference layout: float3 position, float3 normal, float4 tangent,
    # float2 per UV channel, RGBA8 per color channel. For skinned assets we
    # budget four float weights and four uint16 indices.
    stride = 12 + 12
    attributes = {
        "position_bytes": 12,
        "normal_bytes": 12,
        "tangent_bytes": 0,
        "uv_bytes": uv_channels * 8,
        "color_bytes": color_channels * 4,
        "skin_bytes": 0,
    }

    if include_tangents and uv_channels:
        attributes["tangent_bytes"] = 16
        stride += 16

    stride += attributes["uv_bytes"] + attributes["color_bytes"]

    if has_skinning:
        attributes["skin_bytes"] = 24  # 4 float weights + 4 uint16 indices.
        stride += attributes["skin_bytes"]

    # 16-bit indices are common when an imported mesh section can address fewer
    # than 65,536 vertices; otherwise assume 32-bit. Engines may split meshes
    # differently, hence the explicit estimate label.
    index_bytes = 2 if render_vertices <= 65535 else 4
    vertex_buffer = render_vertices * stride
    index_buffer = triangles * 3 * index_bytes
    total = vertex_buffer + index_buffer

    return {
        "vertex_stride_bytes": stride,
        "index_size_bytes": index_bytes,
        "vertex_buffer_bytes": vertex_buffer,
        "index_buffer_bytes": index_buffer,
        "mesh_buffer_bytes": total,
        "attributes": attributes,
        "assumptions": {
            "tangents": bool(include_tangents and uv_channels),
            "skin_4_weights": bool(has_skinning),
            "compression": "none/reference layout",
        },
    }


def format_bytes(byte_count: int) -> str:
    value = float(max(0, int(byte_count)))
    units = ("B", "KiB", "MiB", "GiB")
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return "{0:.0f} {1}".format(value, unit)
            return "{0:.2f} {1}".format(value, unit)
        value /= 1024.0
    return "{0:.2f} GiB".format(value)
