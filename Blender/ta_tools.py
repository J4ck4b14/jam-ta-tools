bl_info = {
    "name": "JAM TA Tools",
    "blender": (4, 2, 0),
    "category": "Object",
    "version": (2, 4, 0),
    "author": "Juan Abia Merino",
    "description": (
        "Game-art asset pipeline toolkit with Asset Doctor profiles, render-split "
        "analysis, mesh/UV validation, texel density, export and rigging helpers"
    ),
}

import json
import math
import os
import sys

# Shared host-independent core. The repo layout keeps one source of truth for
# profiles/reporting while Blender-specific code stays in this file.
_TA_CORE_PATHS = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Common")),
    os.path.abspath(os.path.dirname(__file__)),
]
for _ta_core_path in _TA_CORE_PATHS:
    if os.path.isdir(os.path.join(_ta_core_path, "jam_ta_core")) and _ta_core_path not in sys.path:
        sys.path.insert(0, _ta_core_path)

from jam_ta_core import (
    AssetReport,
    ValidationIssue,
    analyze_render_splits,
    estimate_mesh_memory,
    format_bytes,
    get_profile,
    profile_items,
    summarize_texel_density,
    texel_density_color,
    write_report_json,
    write_export_sidecar,
    AssetMemberSpec,
    ROLE_RENDER,
    ROLE_LOD,
    ROLE_COLLISION,
    ROLE_SOCKET,
    ROLE_SKELETON,
    ROLE_HELPER,
    infer_asset_member,
    canonical_root_from_members,
    analyze_asset_set,
    analyze_material_inventory,
    infer_texture_semantic,
    load_profiles_json,
    load_rule_module,
    run_registered_rules,
    get_export_profile,
    export_profile_items,
    export_extension,
    export_format,
    snap_vector,
    analyze_modular_bounds,
    bounds_anchor,
    bounds_scale_factors,
    make_attachment_name,
    build_export_plan,
    format_export_plan,
    analyze_uv_layout,
    summarize_island_texel_density,
)

import bpy
import bmesh
from mathutils import Vector, Matrix
from bpy.app.handlers import persistent


TA_LAST_REPORTS = []
TA_LAST_ASSET_SET = None
TA_TD_HEATMAP_NAME = "JAM_TD_HEATMAP"


@persistent
def ta_invalidate_analysis_cache(scene, depsgraph):
    """Invalidate cached UI results whenever relevant mesh/object data changes."""
    global TA_LAST_REPORTS, TA_LAST_ASSET_SET

    relevant_change = False
    for update in getattr(depsgraph, "updates", []):
        datablock = getattr(update, "id", None)
        if isinstance(datablock, (bpy.types.Mesh, bpy.types.Object)):
            relevant_change = True
            break

    if relevant_change:
        if hasattr(scene, "ee_budget_cached_valid"):
            scene.ee_budget_cached_valid = False
        TA_LAST_REPORTS = []
        TA_LAST_ASSET_SET = None


def get_extreme_vertex(obj, axis_str):
    """
    Finds the axis-most vertex of the given axis (+-X, +-Y, +-Z) or the mid point
    """

    if not obj.data.vertices:
        return obj.matrix_world.translation.copy()

    bpy.ops.object.mode_set(mode="OBJECT")

    if axis_str == "MID":
        local_coords = [v.co for v in obj.data.vertices]
        median_co = sum(local_coords, Vector()) / len(local_coords)

        return obj.matrix_world @ median_co

    axis_index = {'X': 0, 'Y': 1, 'Z': 2}[axis_str[-1]]
    find_max = not axis_str.startswith('-')

    coords = [v.co[axis_index] for v in obj.data.vertices]
    target_value = max(coords) if find_max else min(coords)
    target_index = coords.index(target_value)
    vertex = obj.data.vertices[target_index]

    return obj.matrix_world @ vertex.co


def get_selection_extreme_vertex(objs, axis_str):
    mesh_objs = [obj for obj in objs if
                 obj.type == "MESH" and obj.data.vertices]

    if not mesh_objs:
        return None

    if axis_str == "MID":
        coords = []

        for obj in mesh_objs:
            for vert in obj.data.vertices:
                coords.append(obj.matrix_world @ vert.co)

        return sum(coords, Vector()) / len(coords)

    axis_index = {'X': 0, 'Y': 1, 'Z': 2}[axis_str[-1]]
    find_max = not axis_str.startswith('-')

    extreme_points = [
        get_extreme_vertex(obj, axis_str)
        for obj in mesh_objs
    ]

    if find_max:
        return max(extreme_points, key=lambda co: co[axis_index])

    return min(extreme_points, key=lambda co: co[axis_index])


def update_axis(self, context):
    # This function just forces the UI redraw
    pass


def get_mesh_metrics(obj, context, mode):
    """
    Returns VERTS, FACES, or TRIS count.
    Uses evaluated mesh when ee_count_modifiers is enabled
    """

    if obj is None or obj.type != "MESH":
        return 0

    if mode == "ENGINE":
        return get_engine_vertex_estimate(obj, context)

    scene = context.scene

    if scene.ee_count_modifiers:
        graph = context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(graph)
        mesh = eval_obj.to_mesh()
        should_clear = True
    else:
        mesh = obj.data
        eval_obj = None
        should_clear = False

    try:
        if mesh is None:
            return 0

        if mode == "VERTS":
            return len(mesh.vertices)

        if mode == "FACES":
            return len(mesh.polygons)

        mesh.calc_loop_triangles()
        return len(mesh.loop_triangles)

    finally:
        if should_clear and eval_obj:
            if hasattr(eval_obj, "to_mesh_clear"):
                eval_obj.to_mesh_clear()
            else:
                bpy.data.meshes.remove(mesh)


def get_rounded_tuple(values, precision=6):
    return tuple(round(v, precision) for v in values)


def get_loop_normal(mesh, loop_index):
    """
    Gets a per-corner normal in a way that works across Blender versions.
    """

    if hasattr(mesh, "corner_normals") and len(mesh.corner_normals) > loop_index:
        normal = mesh.corner_normals[loop_index].vector
        return get_rounded_tuple(normal)

    loop = mesh.loops[loop_index]
    return get_rounded_tuple(loop.normal)


def get_valid_uv_layers(mesh):
    """
    Returns only UV layers whose data length matches the mesh loop count.
    Prevents uv_layer.data[loop_index] out-of-range errors.
    """
    loop_count = len(mesh.loops)

    valid_layers = []

    for uv_layer in mesh.uv_layers:
        if len(uv_layer.data) == loop_count:
            valid_layers.append(uv_layer)

    return valid_layers


def ta_get_render_split_breakdown(obj, context):
    """Detailed render-vertex estimate with progressive split attribution."""

    if obj is None or obj.type != "MESH":
        return {
            "base_vertices": 0,
            "render_vertices": 0,
            "extra_vertices": 0,
            "split_ratio": 0.0,
            "uv_layer_names": [],
            "include_material": bool(context.scene.ee_count_material_splits),
            "corner_count": 0,
            "stages": [],
        }

    scene = context.scene

    if scene.ee_count_modifiers:
        graph = context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(graph)
        mesh = eval_obj.to_mesh()
        should_clear = True
    else:
        if obj.mode == "EDIT":
            try:
                obj.update_from_editmode()
            except Exception:
                pass

        mesh = obj.data
        eval_obj = None
        should_clear = False

    try:
        if mesh is None:
            return {
                "base_vertices": 0,
                "render_vertices": 0,
                "extra_vertices": 0,
                "split_ratio": 0.0,
                "uv_layer_names": [],
                "include_material": bool(scene.ee_count_material_splits),
                "corner_count": 0,
                "stages": [],
            }

        mesh.calc_loop_triangles()
        if hasattr(mesh, "calc_normals_split"):
            mesh.calc_normals_split()

        uv_layers = get_valid_uv_layers(mesh)
        corners = []

        for poly in mesh.polygons:
            for loop_index in poly.loop_indices:
                loop = mesh.loops[loop_index]
                uvs = []

                for uv_layer in uv_layers:
                    if loop_index < len(uv_layer.data):
                        uv = uv_layer.data[loop_index].uv
                        uvs.append(get_rounded_tuple((uv.x, uv.y)))
                    else:
                        uvs.append(None)

                corners.append({
                    "vertex": loop.vertex_index,
                    "normal": get_loop_normal(mesh, loop_index),
                    "uvs": uvs,
                    "material": poly.material_index,
                })

        result = analyze_render_splits(
            corners,
            [layer.name for layer in uv_layers],
            include_material=scene.ee_count_material_splits,
        )
        result["triangle_count"] = len(mesh.loop_triangles)
        result["face_count"] = len(mesh.polygons)
        result["material_slots"] = len(getattr(obj, "material_slots", []))
        return result

    finally:
        if should_clear and eval_obj:
            if hasattr(eval_obj, "to_mesh_clear"):
                eval_obj.to_mesh_clear()
            else:
                bpy.data.meshes.remove(mesh)


def get_engine_vertex_estimate(obj, context):
    """Return the render-vertex estimate used by the budget display."""
    return int(ta_get_render_split_breakdown(obj, context)["render_vertices"])

def get_selected_meshes(context):
    return [obj for obj in context.selected_objects if obj.type == "MESH"]


def get_selection_metric(context):
    scene = context.scene
    mesh_objects = get_selected_meshes(context)

    return sum(
        get_mesh_metrics(obj, context, scene.ee_budget_count_mode)
        for obj in mesh_objects
    )


def get_poly_classification(count, scene):
    budget = max(scene.ee_poly_budget, 1)
    margin = int(budget * (scene.ee_poly_margin / 100.0))

    warning_start = max(0, budget - margin)
    high_limit = budget + margin

    if count > high_limit:
        return {
            "prefix": scene.ee_hp_prefix,
            "state": "HIGH",
            "label": "High",
            "icon": "CANCEL",
            "limit": high_limit,
        }

    if count >= warning_start:
        return {
            "prefix": scene.ee_lp_prefix,
            "state": "MARGIN",
            "label": "Near budget",
            "icon": "ERROR",
            "limit": high_limit,
        }

    return {
        "prefix": scene.ee_lp_prefix,
        "state": "LOW",
        "label": "Low",
        "icon": "CHECKMARK",
        "limit": high_limit,
    }


def remove_existing_poly_prefix(name, prefixes):
    for prefix in prefixes:
        if not prefix:
            continue

        token = prefix + "_"

        if name.startswith(token):
            return name[len(token):]

    return name


def get_export_name_with_prefix(context, base_name):
    scene = context.scene
    count = get_selection_metric(context)
    classification = get_poly_classification(count, scene)

    clean_name = remove_existing_poly_prefix(
        base_name,
        [scene.ee_lp_prefix, scene.ee_hp_prefix]
    )

    export_name = f"{classification['prefix']}_{clean_name}"

    return export_name, count, classification


def get_object_export_name(context, obj):
    """
    Export name for a single object, with LP/HP prefix classification
    based on that object's own count (used by batch export).
    """
    scene = context.scene

    if not scene.ee_auto_poly_prefix:
        return obj.name

    count = get_mesh_metrics(obj, context, scene.ee_budget_count_mode)
    classification = get_poly_classification(count, scene)

    clean_name = remove_existing_poly_prefix(
        obj.name,
        [scene.ee_lp_prefix, scene.ee_hp_prefix]
    )

    return f"{classification['prefix']}_{clean_name}"


def get_budget_count_label(scene):
    return {
        "ENGINE": "engine verts",
        "TRIS": "tris",
        "FACES": "faces",
        "VERTS": "verts",
    }.get(scene.ee_budget_count_mode, "items")


def get_budget_signature(context):
    """
    Used only to warn that the cached count may be stale.
    Checks selection and count settings, not mesh changes.
    """
    scene = context.scene
    selected_meshes = get_selected_meshes(context)

    object_part = "|".join(
        f"{obj.name}:{obj.as_pointer()}:{obj.data.as_pointer()}"
        for obj in selected_meshes
    )

    return "|".join([
        scene.ee_budget_count_mode,
        str(scene.ee_count_modifiers),
        str(scene.ee_count_material_splits),
        object_part,
    ])


def draw_budget_marker(layout, context):
    scene = context.scene
    mesh_objects = get_selected_meshes(context)

    row = layout.row(align=True)
    row.scale_y = 0.75

    if not mesh_objects:
        row.label(text="Poly budget: No mesh selected", icon="INFO")
        return

    if not scene.ee_budget_cached_valid:
        row.label(text="Poly budget: Not calculated", icon="INFO")
        return

    cached_signature = get_budget_signature(context)
    is_stale = cached_signature != scene.ee_budget_cached_signature

    count = scene.ee_budget_cached_count
    classification = get_poly_classification(count, scene)
    count_label = get_budget_count_label(scene)

    if classification["state"] == "HIGH":
        row.alert = True

    if is_stale:
        row.alert = True
        row.label(
            text=(
                f"Budget may be stale · "
                f"{count:,}/{classification['limit']:,} {count_label} · "
                f"Press Calculate"
            ),
            icon="FILE_REFRESH"
        )
        return

    row.label(
        text=(
            f"{classification['label']} · "
            f"{count:,}/{classification['limit']:,} {count_label} · "
            f"Export as {classification['prefix']}_"
        ),
        icon=classification["icon"]
    )


def get_non_manifold_edges(obj):
    """
    Returns non-manifold edge data for a mesh object.

    Handled categories:
    - wire: edge with no faces
    - boundary: edge has just one face
    - multiface: edge has more than two faces
    - non_contiguous: edge has two faces but is still not manifold
      (valid for some mesh topologies)
    """

    if obj is None or obj.type != "MESH":
        return None

    mesh = obj.data

    bm = bmesh.new()
    bm.from_mesh(mesh)

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.edges.index_update()

    bad_edges = []
    wire_edges = []
    boundary_edges = []
    multiface_edges = []
    non_contiguous_edges = []

    try:
        for edge in bm.edges:
            face_count = len(edge.link_faces)

            if edge.is_manifold:
                continue

            bad_edges.append(edge.index)

            if face_count == 0:
                wire_edges.append(edge.index)
            elif face_count == 1:
                boundary_edges.append(edge.index)
            elif face_count > 2:
                multiface_edges.append(edge.index)
            else:
                non_contiguous_edges.append(edge.index)

        return {
            "bad_edges": bad_edges,
            "wire_edges": wire_edges,
            "boundary_edges": boundary_edges,
            "multiface_edges": multiface_edges,
            "non_contiguous_edges": non_contiguous_edges,
        }
    finally:
        bm.free()


def ta_get_edit_mesh_objects(context):
    """
    Returns mesh objects in edit mode.
    Supports multi-object edit mode when available.
    """
    if context.mode != "EDIT_MESH":
        return []

    objects = getattr(context, "objects_in_mode_unique_data", None)

    if objects:
        return [obj for obj in objects if obj and obj.type == "MESH"]

    if context.active_object and context.active_object.type == "MESH":
        return [context.active_object]

    return []


def ta_get_bmesh_uv_layer(bm, uv_map_name):
    """
    Gets or creates a BMesh UV layer.
    """
    uv_map_name = uv_map_name.strip()

    if uv_map_name:
        uv_layer = bm.loops.layers.uv.get(uv_map_name)

        if uv_layer is None:
            uv_layer = bm.loops.layers.uv.new(uv_map_name)

        return uv_layer

    return bm.loops.layers.uv.verify()


def ta_get_selected_face_islands(faces):
    """
    Splits selected faces into connected face islands.
    This is geometry-island based, not existing UV-island based.
    """
    selected = set(faces)
    visited = set()
    islands = []

    for start_face in faces:
        if start_face in visited:
            continue

        island = []
        stack = [start_face]
        visited.add(start_face)

        while stack:
            face = stack.pop()
            island.append(face)

            for edge in face.edges:
                for linked_face in edge.link_faces:
                    if linked_face in selected and linked_face not in visited:
                        visited.add(linked_face)
                        stack.append(linked_face)

        islands.append(island)

    return islands


def ta_get_average_face_normal(obj, faces, space):
    """
    Area-weighted average normal for selected faces.
    Used by the Normal / Best Fit projection.
    """
    normal = Vector((0.0, 0.0, 0.0))

    normal_matrix = None

    if space == "WORLD":
        normal_matrix = obj.matrix_world.to_3x3().inverted().transposed()

    for face in faces:
        face_normal = face.normal.copy()

        if space == "WORLD":
            face_normal = normal_matrix @ face_normal

        if face_normal.length_squared == 0.0:
            continue

        face_normal.normalize()
        normal += face_normal * max(face.calc_area(), 0.000001)

    if normal.length_squared == 0.0:
        return Vector((0.0, 0.0, 1.0))

    normal.normalize()
    return normal


def ta_get_basis_from_normal(normal):
    """
    Builds stable U/V projection axes from a normal.
    """
    normal = normal.normalized()

    reference = Vector((0.0, 0.0, 1.0))

    if abs(normal.dot(reference)) > 0.95:
        reference = Vector((0.0, 1.0, 0.0))

    u_axis = reference.cross(normal)

    if u_axis.length_squared == 0.0:
        u_axis = Vector((1.0, 0.0, 0.0))
    else:
        u_axis.normalize()

    v_axis = normal.cross(u_axis)

    if v_axis.length_squared == 0.0:
        v_axis = Vector((0.0, 1.0, 0.0))
    else:
        v_axis.normalize()

    return u_axis, v_axis


def ta_get_projection_basis(obj, faces, projection_mode, space):
    """
    Returns U and V axes for planar projection.
    """
    if projection_mode == "XY":
        return Vector((1.0, 0.0, 0.0)), Vector((0.0, 1.0, 0.0))

    if projection_mode == "XZ":
        return Vector((1.0, 0.0, 0.0)), Vector((0.0, 0.0, 1.0))

    if projection_mode == "YZ":
        return Vector((0.0, 1.0, 0.0)), Vector((0.0, 0.0, 1.0))

    normal = ta_get_average_face_normal(obj, faces, space)
    return ta_get_basis_from_normal(normal)


def ta_get_loop_coord(obj, loop, space):
    co = loop.vert.co.copy()

    if space == "WORLD":
        return obj.matrix_world @ co

    return co


def ta_normalize_projected_uv(u, v, bounds, preserve_aspect, padding):
    min_u, max_u, min_v, max_v = bounds

    span_u = max(max_u - min_u, 0.000001)
    span_v = max(max_v - min_v, 0.000001)

    if preserve_aspect:
        size = max(span_u, span_v)

        center_u = (min_u + max_u) * 0.5
        center_v = (min_v + max_v) * 0.5

        min_u = center_u - size * 0.5
        min_v = center_v - size * 0.5

        span_u = size
        span_v = size

    final_u = (u - min_u) / span_u
    final_v = (v - min_v) / span_v

    padding = max(0.0, min(padding, 0.49))
    usable_space = 1.0 - padding * 2.0

    final_u = padding + final_u * usable_space
    final_v = padding + final_v * usable_space

    return final_u, final_v


def ta_apply_uv_post_transform(u, v, rotation, flip_u, flip_v):
    """
    Applies simple DCC-style UV post transforms.
    """
    if rotation == "90":
        u, v = v, 1.0 - u
    elif rotation == "180":
        u, v = 1.0 - u, 1.0 - v
    elif rotation == "270":
        u, v = 1.0 - v, u

    if flip_u:
        u = 1.0 - u

    if flip_v:
        v = 1.0 - v

    return u, v


def ta_project_faces_to_uv(
    obj,
    faces,
    uv_layer,
    projection_mode,
    space,
    preserve_aspect,
    padding,
    rotation,
    flip_u,
    flip_v
):
    """
    Projects a group of selected faces into UV space.
    """
    if not faces:
        return 0

    u_axis, v_axis = ta_get_projection_basis(
        obj,
        faces,
        projection_mode,
        space
    )

    projected_loops = []

    min_u = float("inf")
    max_u = float("-inf")
    min_v = float("inf")
    max_v = float("-inf")

    for face in faces:
        for loop in face.loops:
            co = ta_get_loop_coord(obj, loop, space)

            raw_u = co.dot(u_axis)
            raw_v = co.dot(v_axis)

            projected_loops.append((loop, raw_u, raw_v))

            min_u = min(min_u, raw_u)
            max_u = max(max_u, raw_u)
            min_v = min(min_v, raw_v)
            max_v = max(max_v, raw_v)

    bounds = min_u, max_u, min_v, max_v

    for loop, raw_u, raw_v in projected_loops:
        final_u, final_v = ta_normalize_projected_uv(
            raw_u,
            raw_v,
            bounds,
            preserve_aspect,
            padding
        )

        final_u, final_v = ta_apply_uv_post_transform(
            final_u,
            final_v,
            rotation,
            flip_u,
            flip_v
        )

        loop[uv_layer].uv = (final_u, final_v)

    return len(faces)


# ---
# Settings persistence (JSON defaults shared across sessions/files)
# ---

# Scene properties saved by "Save Defaults" and restored by "Apply
# Defaults". "ee_unify" is deliberately excluded: destructive toggles
# should always reset to off.
TA_PERSISTED_PROPS = (
    "rs_prefix",
    "rs_name",
    "ee_poly_budget",
    "ee_poly_margin",
    "ee_budget_count_mode",
    "ee_count_material_splits",
    "ee_count_modifiers",
    "ee_auto_poly_prefix",
    "ee_lp_prefix",
    "ee_hp_prefix",
    "ee_export_path",
    "ta_export_profile",
    "ee_validate_export",
    "ee_block_export",
    "ee_batch_export",
    "ee_move_to_origin",
    "ee_write_sidecar",
    "ta_uv_checks",
    "ta_profile_id",
    "ta_profile_file",
    "ta_rule_file",
    "ta_enforce_project_budget",
    "ta_asset_report_path",
    "ta_show_split_details",
    "ta_uv_map_name",
    "ta_uv_projection_mode",
    "ta_uv_projection_space",
    "ta_uv_fit_mode",
    "ta_uv_preserve_aspect",
    "ta_uv_padding",
    "ta_td_texture_size",
    "ta_td_target",
    "ta_td_unit",
    "ta_td_tolerance",
    "ta_enforce_td_target",
    "ta_asset_set_root",
    "ta_asset_member_role",
    "ta_asset_member_lod",
    "ta_asset_collision_type",
    "ta_asset_member_export",
    "ta_lod_generate_level",
    "ta_lod_generate_ratio",
    "ta_lod_min_reduction",
    "ta_require_contiguous_lods",
    "ta_enforce_collision_budget",
    "ta_collision_triangle_budget",
    "ta_asset_set_move_to_origin",
    "ta_ik_chain_count",
    "ta_pole_distance",
    "ta_muscle_axis",
    "ta_muscle_max_angle",
    "ta_muscle_bulge",
)


def ta_get_config_path():
    """
    Returns the path of the JSON file that stores cross-session defaults,
    inside Blender's per-user config folder.
    """
    config_dir = bpy.utils.user_resource("CONFIG")

    return os.path.join(config_dir, "jam_ta_tools.json")


def ta_save_defaults(scene):
    data = {}

    for prop_name in TA_PERSISTED_PROPS:
        if hasattr(scene, prop_name):
            data[prop_name] = getattr(scene, prop_name)

    config_path = ta_get_config_path()

    with open(config_path, "w", encoding="utf-8") as config_file:
        json.dump(data, config_file, indent=2)

    return config_path


def ta_apply_defaults(scene):
    config_path = ta_get_config_path()

    if not os.path.isfile(config_path):
        return 0

    with open(config_path, "r", encoding="utf-8") as config_file:
        data = json.load(config_file)

    applied = 0

    for prop_name, value in data.items():
        if prop_name not in TA_PERSISTED_PROPS:
            continue

        if not hasattr(scene, prop_name):
            continue

        try:
            setattr(scene, prop_name, value)
            applied += 1
        except (TypeError, ValueError):
            # Saved value no longer matches the property (e.g. renamed
            # enum item); skip it instead of failing the whole load
            continue

    return applied


# ---
# UV validation
# ---

def ta_uv_signed_area(uvs):
    """
    Signed area of a UV polygon via the shoelace formula.
    A negative value means the face's UV winding is flipped.
    """
    area = 0.0
    count = len(uvs)

    for i in range(count):
        j = (i + 1) % count
        area += uvs[i][0] * uvs[j][1] - uvs[j][0] * uvs[i][1]

    return area * 0.5


def ta_check_object_uvs(obj, tolerance=0.001):
    """
    UV checks for one mesh object (active UV layer): missing UVs, UVs
    outside the 0-1 range, flipped faces and zero-area faces.

    Note: full UV overlap detection is intentionally out of scope; it
    needs spatial acceleration to stay usable on production meshes.
    """
    result = {
        "missing_uvs": False,
        "uvs_out_of_range": False,
        "flipped_faces": [],
        "zero_faces": [],
    }

    if obj is None or obj.type != "MESH":
        return result

    bm = bmesh.new()
    bm.from_mesh(obj.data)

    try:
        uv_layer = bm.loops.layers.uv.active

        if uv_layer is None:
            result["missing_uvs"] = True
            return result

        bm.faces.ensure_lookup_table()

        for face in bm.faces:
            uvs = [loop[uv_layer].uv.copy() for loop in face.loops]

            for uv in uvs:
                if (uv.x < -tolerance or uv.y < -tolerance
                        or uv.x > 1.0 + tolerance or uv.y > 1.0 + tolerance):
                    result["uvs_out_of_range"] = True
                    break

            area = ta_uv_signed_area(uvs)

            if abs(area) < 1e-9:
                result["zero_faces"].append(face.index)
            elif area < 0.0:
                result["flipped_faces"].append(face.index)

        return result

    finally:
        bm.free()


def ta_collect_uv_faces(obj, uv_layer_index=0):
    """Return host-neutral UV faces for one Blender UV channel."""
    if obj is None or obj.type != "MESH":
        return []
    mesh = obj.data
    if obj.mode == "EDIT":
        try:
            obj.update_from_editmode()
        except Exception:
            pass
    if uv_layer_index < 0 or uv_layer_index >= len(mesh.uv_layers):
        return []
    layer = mesh.uv_layers[uv_layer_index]
    faces = []
    for poly in mesh.polygons:
        vertices = []
        uvs = []
        for loop_index in poly.loop_indices:
            loop = mesh.loops[loop_index]
            uv = layer.data[loop_index].uv
            vertices.append(int(loop.vertex_index))
            uvs.append((float(uv.x), float(uv.y)))
        faces.append({"component": int(poly.index), "vertices": vertices, "uvs": uvs})
    return faces


def ta_uv_layout_analysis(obj, texture_size, uv_layer_index=0):
    return analyze_uv_layout(
        ta_collect_uv_faces(obj, uv_layer_index),
        texture_size=texture_size,
    )


def ta_uv_issue_count(uv_result):
    return (
        int(uv_result["missing_uvs"])
        + int(uv_result["uvs_out_of_range"])
        + len(uv_result["flipped_faces"])
        + len(uv_result["zero_faces"])
    )


def ta_run_export_validation(context, objects):
    """
    Runs geometry (and optionally UV) validation on the given objects.
    Returns a list of human-readable issue lines; empty means clean.
    """
    issues = []

    for obj in objects:
        flags = []

        result = get_non_manifold_edges(obj)

        if result and result["bad_edges"]:
            flags.append(f"{len(result['bad_edges'])} non-manifold edges")

        if context.scene.ta_uv_checks:
            uv_result = ta_check_object_uvs(obj)

            if uv_result["missing_uvs"]:
                flags.append("missing UVs")

            if uv_result["uvs_out_of_range"]:
                flags.append("UVs outside 0-1")

            if uv_result["flipped_faces"]:
                flags.append(f"flipped UVs: {len(uv_result['flipped_faces'])}")

            if uv_result["zero_faces"]:
                flags.append(f"zero-area UVs: {len(uv_result['zero_faces'])}")

        if flags:
            issues.append(f"{obj.name}: " + ", ".join(flags))

    return issues


# ---
# Texel density
# ---

def ta_object_area_totals(obj):
    """
    Total world-space surface area and UV area (active UV layer) of a
    mesh object. World scale is included by transforming the BMesh.
    """
    total_world = 0.0
    total_uv = 0.0

    if obj is None or obj.type != "MESH":
        return total_world, total_uv

    bm = bmesh.new()
    bm.from_mesh(obj.data)

    try:
        bm.transform(obj.matrix_world)

        uv_layer = bm.loops.layers.uv.active

        for face in bm.faces:
            total_world += face.calc_area()

            if uv_layer is not None:
                uvs = [loop[uv_layer].uv.copy() for loop in face.loops]
                total_uv += abs(ta_uv_signed_area(uvs))

        return total_world, total_uv

    finally:
        bm.free()


def ta_scene_meters_per_unit(scene):
    """Return metres represented by one Blender world unit."""
    try:
        scale = float(scene.unit_settings.scale_length)
        return scale if scale > 0.0 else 1.0
    except Exception:
        return 1.0


def ta_density_to_display(px_per_m, scene):
    return float(px_per_m) / 100.0 if scene.ta_td_unit == "PX_CM" else float(px_per_m)


def ta_density_from_display(value, scene):
    return float(value) * 100.0 if scene.ta_td_unit == "PX_CM" else float(value)


def ta_density_unit_label(scene):
    return "px/cm" if scene.ta_td_unit == "PX_CM" else "px/m"


def ta_face_texel_samples(obj, scene):
    """Collect per-face world area (m²) and active-UV area."""
    samples = []
    if obj is None or obj.type != "MESH":
        return samples

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    try:
        bm.faces.ensure_lookup_table()
        bm.transform(obj.matrix_world)
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            return samples
        metres_per_unit = ta_scene_meters_per_unit(scene)
        area_scale = metres_per_unit * metres_per_unit
        for face in bm.faces:
            world_area_m2 = float(face.calc_area()) * area_scale
            uvs = [loop[uv_layer].uv.copy() for loop in face.loops]
            uv_area = abs(ta_uv_signed_area(uvs))
            samples.append({
                "component": int(face.index),
                "world_area_m2": world_area_m2,
                "uv_area": float(uv_area),
            })
        return samples
    finally:
        bm.free()


def ta_texel_density_analysis(obj, scene):
    return summarize_texel_density(
        ta_face_texel_samples(obj, scene),
        texture_size=scene.ta_td_texture_size,
        target_px_per_m=ta_density_from_display(scene.ta_td_target, scene),
        tolerance_percent=scene.ta_td_tolerance,
    )


def ta_get_render_split_edge_indices(obj, cause):
    """Return source-mesh edge indices responsible for a visible split cause."""
    if obj is None or obj.type != "MESH":
        return []
    mesh = obj.data
    if obj.mode == "EDIT":
        try:
            obj.update_from_editmode()
        except Exception:
            pass
    if hasattr(mesh, "calc_normals_split"):
        try:
            mesh.calc_normals_split()
        except Exception:
            pass

    valid_uv_layers = get_valid_uv_layers(mesh)
    edge_faces = {edge.index: {} for edge in mesh.edges}
    for poly in mesh.polygons:
        for loop_index in poly.loop_indices:
            loop = mesh.loops[loop_index]
            edge_faces.setdefault(loop.edge_index, {}).setdefault(poly.index, {})[loop.vertex_index] = loop_index

    result = []
    for edge in mesh.edges:
        face_map = edge_faces.get(edge.index, {})
        if len(face_map) != 2:
            continue
        face_ids = list(face_map)
        a, b = face_ids[0], face_ids[1]

        if cause == "MATERIAL":
            if mesh.polygons[a].material_index != mesh.polygons[b].material_index:
                result.append(edge.index)
            continue

        split = False
        for vertex_id in edge.vertices:
            loop_a = face_map[a].get(vertex_id)
            loop_b = face_map[b].get(vertex_id)
            if loop_a is None or loop_b is None:
                continue
            if cause == "NORMAL":
                na = Vector(get_loop_normal(mesh, loop_a))
                nb = Vector(get_loop_normal(mesh, loop_b))
                if (na - nb).length > 1e-5:
                    split = True
                    break
            elif cause == "UV":
                for uv_layer in valid_uv_layers:
                    uva = uv_layer.data[loop_a].uv
                    uvb = uv_layer.data[loop_b].uv
                    if (uva - uvb).length > 1e-6:
                        split = True
                        break
                if split:
                    break
        if split:
            result.append(edge.index)
    return result


def ta_apply_td_heatmap(context, obj):
    """Write a temporary corner color attribute for texel-density inspection."""
    if obj is None or obj.type != "MESH":
        return 0
    if obj.mode == "EDIT":
        try:
            obj.update_from_editmode()
        except Exception:
            pass

    analysis = ta_texel_density_analysis(obj, context.scene)
    samples = analysis.get("samples", [])
    if not samples:
        return 0

    mesh = obj.data
    attributes = getattr(mesh, "color_attributes", None)
    if attributes is None:
        return 0
    existing = attributes.get(TA_TD_HEATMAP_NAME)
    if existing is not None:
        attributes.remove(existing)
    attribute = attributes.new(
        name=TA_TD_HEATMAP_NAME,
        type="BYTE_COLOR",
        domain="CORNER",
    )

    target = ta_density_from_display(context.scene.ta_td_target, context.scene)
    if target <= 0.0:
        target = float(analysis.get("median_px_per_m", 1.0)) or 1.0

    colored = 0
    for sample in samples:
        face_index = sample.get("component")
        if not isinstance(face_index, int) or not (0 <= face_index < len(mesh.polygons)):
            continue
        rgb = texel_density_color(sample.get("density_px_per_m", 0.0), target)
        color = (float(rgb[0]), float(rgb[1]), float(rgb[2]), 1.0)
        for loop_index in mesh.polygons[face_index].loop_indices:
            if 0 <= loop_index < len(attribute.data):
                attribute.data[loop_index].color = color
        colored += 1

    try:
        # Blender 5.x uses the active color attribute for Vertex shading.
        # Setting the name explicitly is more robust than relying on collection order.
        if hasattr(attributes, "active_color_name"):
            attributes.active_color_name = TA_TD_HEATMAP_NAME
        for index, item in enumerate(attributes):
            if item.name == TA_TD_HEATMAP_NAME:
                attributes.active_color_index = index
                break
    except Exception:
        pass

    for area in getattr(context.screen, "areas", []) if context.screen else []:
        if area.type == "VIEW_3D":
            try:
                area.spaces.active.shading.color_type = "VERTEX"
            except Exception:
                pass
    mesh.update()
    return colored


def ta_has_td_heatmap(obj):
    if obj is None or obj.type != "MESH":
        return False
    attributes = getattr(obj.data, "color_attributes", None)
    return bool(attributes is not None and attributes.get(TA_TD_HEATMAP_NAME) is not None)


def ta_clear_td_heatmap(obj):
    if obj is None or obj.type != "MESH":
        return False
    attributes = getattr(obj.data, "color_attributes", None)
    if attributes is None:
        return False
    attribute = attributes.get(TA_TD_HEATMAP_NAME)
    if attribute is None:
        return False
    attributes.remove(attribute)
    obj.data.update()
    return True


def ta_get_object_texel_density(obj, texture_size, scene=None):
    """Average texel density in the currently selected display unit."""
    scene = scene or bpy.context.scene
    analysis = summarize_texel_density(
        ta_face_texel_samples(obj, scene),
        texture_size=texture_size,
    )
    if not analysis["sample_count"]:
        return None
    return ta_density_to_display(analysis["density_px_per_m"], scene)


def ta_get_selection_texel_density(context, texture_size):
    """Area-weighted texel density across the selection, normalized by scene units."""
    samples = []
    for obj in get_selected_meshes(context):
        samples.extend(ta_face_texel_samples(obj, context.scene))
    analysis = summarize_texel_density(samples, texture_size=texture_size)
    if not analysis["sample_count"]:
        return None
    return ta_density_to_display(analysis["density_px_per_m"], context.scene)

def ta_set_object_texel_density(obj, factor):
    """
    Uniformly scales the object's active UV layer around its UV
    bounding-box center by the given factor.
    """
    bm = bmesh.new()
    bm.from_mesh(obj.data)

    try:
        uv_layer = bm.loops.layers.uv.active

        if uv_layer is None:
            return False

        min_u = min_v = float("inf")
        max_u = max_v = float("-inf")

        for face in bm.faces:
            for loop in face.loops:
                uv = loop[uv_layer].uv
                min_u = min(min_u, uv.x)
                max_u = max(max_u, uv.x)
                min_v = min(min_v, uv.y)
                max_v = max(max_v, uv.y)

        if min_u > max_u:
            return False

        center_u = (min_u + max_u) * 0.5
        center_v = (min_v + max_v) * 0.5

        for face in bm.faces:
            for loop in face.loops:
                uv = loop[uv_layer].uv
                uv.x = center_u + (uv.x - center_u) * factor
                uv.y = center_v + (uv.y - center_v) * factor

        bm.to_mesh(obj.data)
        obj.data.update()

        return True

    finally:
        bm.free()



# ---
# Modular / attachment authoring
# ---

def ta_blender_cm_per_unit(context):
    """Convert Blender world units to centimetres once, at the host edge."""
    scale = float(getattr(context.scene.unit_settings, "scale_length", 1.0) or 1.0)
    return scale * 100.0


def ta_world_bounds(objects):
    corners = []
    for obj in objects:
        if obj.type == "MESH":
            corners.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
        else:
            corners.append(obj.matrix_world.translation.copy())
    if not corners:
        return None, None
    minimum = Vector((min(v.x for v in corners), min(v.y for v in corners), min(v.z for v in corners)))
    maximum = Vector((max(v.x for v in corners), max(v.y for v in corners), max(v.z for v in corners)))
    return minimum, maximum


def ta_modular_analysis_for_object(context, obj, grid_cm=None, tolerance_cm=None, check_position=None):
    minimum, maximum = ta_world_bounds([obj])
    if minimum is None:
        return None
    factor = ta_blender_cm_per_unit(context)
    scene = context.scene
    return analyze_modular_bounds(
        tuple(v * factor for v in minimum),
        tuple(v * factor for v in maximum),
        float(grid_cm if grid_cm is not None else scene.ta_modular_grid_cm),
        float(tolerance_cm if tolerance_cm is not None else scene.ta_modular_tolerance_cm),
        bool(scene.ta_modular_check_position if check_position is None else check_position),
    )


def ta_attachment_world_location(context, source_objects, mode):
    mode = str(mode or "ACTIVE_ORIGIN")
    active = context.active_object
    if mode == "CURSOR":
        return context.scene.cursor.location.copy()
    if mode in {"BOUNDS_CENTER", "BOUNDS_BOTTOM"}:
        minimum, maximum = ta_world_bounds(source_objects)
        if minimum is not None:
            anchor = bounds_anchor(tuple(minimum), tuple(maximum), "BOTTOM" if mode == "BOUNDS_BOTTOM" else "CENTER")
            return Vector(anchor)
    return active.matrix_world.translation.copy() if active is not None else context.scene.cursor.location.copy()


# ---
# Asset Sets
# ---

TA_ASSET_ROOT_PROP = "jam_asset_root"
TA_ASSET_ROLE_PROP = "jam_asset_role"
TA_ASSET_LOD_PROP = "jam_lod_level"
TA_ASSET_COLLISION_PROP = "jam_collision_type"
TA_ASSET_EXPORT_PROP = "jam_export_enabled"


def ta_asset_set_root_for_object(obj):
    if obj is None:
        return ""
    root = str(obj.get(TA_ASSET_ROOT_PROP, "") or "").strip()
    if root:
        return root
    return infer_asset_member(obj.name).root_name


def ta_set_asset_membership(obj, root_name, role=None, lod_level=None, collision_type=None, export_enabled=True):
    """Persist explicit Asset Set membership on a Blender scene object."""
    inferred = infer_asset_member(obj.name, root_name)
    role = role or inferred.role
    if obj.type == "ARMATURE":
        role = ROLE_SKELETON
    elif obj.type == "EMPTY" and role == ROLE_RENDER:
        role = ROLE_SOCKET

    obj[TA_ASSET_ROOT_PROP] = str(root_name)
    obj[TA_ASSET_ROLE_PROP] = str(role)
    obj[TA_ASSET_LOD_PROP] = int(inferred.lod_level if lod_level is None else lod_level)
    obj[TA_ASSET_COLLISION_PROP] = str(collision_type if collision_type is not None else inferred.collision_type)
    obj[TA_ASSET_EXPORT_PROP] = bool(export_enabled)


def ta_asset_member_spec(context, obj, root_name=""):
    """Build the host-independent member record for one Blender object."""
    root = str(obj.get(TA_ASSET_ROOT_PROP, root_name or "") or root_name or "").strip()
    inferred = infer_asset_member(obj.name, root)
    role = str(obj.get(TA_ASSET_ROLE_PROP, inferred.role) or inferred.role)
    lod_level = int(obj.get(TA_ASSET_LOD_PROP, inferred.lod_level) or 0)
    collision_type = str(obj.get(TA_ASSET_COLLISION_PROP, inferred.collision_type) or inferred.collision_type)
    export_enabled = bool(obj.get(TA_ASSET_EXPORT_PROP, True))

    triangles = 0
    is_closed = None
    is_convex = None
    if obj.type == "MESH":
        triangles = int(get_mesh_metrics(obj, context, "TRIS"))
        if role == ROLE_COLLISION and collision_type == "CONVEX":
            is_closed, is_convex = ta_collision_quality(obj)

    return AssetMemberSpec(
        name=obj.name,
        root_name=root or inferred.root_name,
        role=role,
        lod_level=lod_level,
        collision_type=collision_type,
        export_enabled=export_enabled,
        triangles=triangles,
        is_closed=is_closed,
        is_convex=is_convex,
        metadata={"object_type": obj.type},
    )


def ta_collision_quality(obj, epsilon=1e-5):
    """Conservative closed/convex test for custom collision meshes.

    Convexity is tested by checking that every vertex stays on the centroid side
    of every face plane. A result of False is useful and actionable; the test is
    deliberately conservative around degenerate faces.
    """
    if obj is None or obj.type != "MESH" or not obj.data.vertices:
        return None, None

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    try:
        bm.transform(obj.matrix_world)
        bm.normal_update()
        closed = all(len(edge.link_faces) == 2 for edge in bm.edges)
        if not closed or not bm.faces:
            return closed, False if not closed else None

        centroid = sum((vert.co for vert in bm.verts), Vector()) / max(1, len(bm.verts))
        convex = True
        for face in bm.faces:
            if face.calc_area() <= epsilon:
                continue
            normal = face.normal.normalized()
            plane_point = face.verts[0].co
            center_side = normal.dot(centroid - plane_point)
            for vert in bm.verts:
                side = normal.dot(vert.co - plane_point)
                if center_side <= 0.0:
                    if side > epsilon:
                        convex = False
                        break
                elif side < -epsilon:
                    convex = False
                    break
            if not convex:
                break
        return closed, convex
    finally:
        bm.free()


def ta_get_asset_set_members(context, root_name, include_disabled=True):
    members = []
    for obj in context.scene.objects:
        if str(obj.get(TA_ASSET_ROOT_PROP, "") or "") != root_name:
            continue
        if not include_disabled and not bool(obj.get(TA_ASSET_EXPORT_PROP, True)):
            continue
        members.append(obj)
    return members


def ta_asset_set_primary(members):
    candidates = []
    for obj in members:
        role = str(obj.get(TA_ASSET_ROLE_PROP, ""))
        level = int(obj.get(TA_ASSET_LOD_PROP, 0) or 0)
        if role in {ROLE_RENDER, ROLE_LOD} and level == 0 and obj.type == "MESH":
            candidates.append(obj)
    return candidates[0] if candidates else (members[0] if members else None)


def ta_build_asset_set_analysis(context, root_name):
    global TA_LAST_ASSET_SET
    members = ta_get_asset_set_members(context, root_name, include_disabled=True)
    specs = [ta_asset_member_spec(context, obj, root_name) for obj in members]
    collision_budget = (
        int(context.scene.ta_collision_triangle_budget)
        if context.scene.ta_enforce_collision_budget
        else None
    )
    analysis = analyze_asset_set(
        root_name,
        specs,
        min_lod_reduction_percent=float(context.scene.ta_lod_min_reduction),
        require_contiguous_lods=bool(context.scene.ta_require_contiguous_lods),
        collision_triangle_budget=collision_budget,
    )
    analysis["objects"] = members
    TA_LAST_ASSET_SET = analysis
    context.scene.ta_asset_set_summary = (
        "{0} · {1} member(s) · {2} LOD level(s) · {3} collision(s)".format(
            analysis["status"],
            analysis["metrics"].get("member_count", 0),
            analysis["metrics"].get("lod_count", 0),
            analysis["metrics"].get("collision_count", 0),
        )
    )
    return analysis


def ta_asset_set_for_active(context):
    active = context.active_object
    if active is None:
        return ""
    return str(active.get(TA_ASSET_ROOT_PROP, "") or "")


def ta_asset_set_reports(context, analysis):
    reports = []
    for obj in analysis.get("objects", []):
        role = str(obj.get(TA_ASSET_ROLE_PROP, ""))
        if obj.type == "MESH" and role in {ROLE_RENDER, ROLE_LOD} and bool(obj.get(TA_ASSET_EXPORT_PROP, True)):
            reports.append(ta_asset_doctor_analyze_object(context, obj))
    return reports


def ta_asset_set_sidecar_data(analysis):
    return {
        "root_name": analysis.get("root_name", ""),
        "status": analysis.get("status", "CLEAN"),
        "metrics": analysis.get("metrics", {}),
        "members": analysis.get("members", []),
        "issues": [
            {
                "rule_id": issue.rule_id,
                "severity": issue.severity,
                "title": issue.title,
                "category": issue.category,
                "object_name": issue.object_name,
            }
            for issue in analysis.get("issues", [])
        ],
    }


def ta_select_only(context, objects, active=None):
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in objects:
        try:
            obj.select_set(True)
        except Exception:
            pass
    context.view_layer.objects.active = active or (objects[0] if objects else None)


def ta_next_collision_index(context, root_name, collision_prefix="UBX"):
    indices = []
    prefix = "{0}_{1}_".format(collision_prefix, root_name)
    for obj in context.scene.objects:
        if obj.name.startswith(prefix):
            tail = obj.name[len(prefix):]
            if tail.isdigit():
                indices.append(int(tail))
    return (max(indices) + 1) if indices else 0


# ---
# Asset Doctor
# ---

def ta_get_zero_area_faces(obj, epsilon=1e-12):
    if obj is None or obj.type != "MESH":
        return []

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    try:
        bm.faces.ensure_lookup_table()
        return [face.index for face in bm.faces if face.calc_area() <= epsilon]
    finally:
        bm.free()


def ta_can_apply_scale_safely(obj):
    """Conservative gate for the Asset Doctor's automatic scale fix."""
    if obj is None or obj.type != "MESH":
        return False
    if any(value <= 0.0 for value in obj.scale):
        return False
    if getattr(obj.data, "shape_keys", None) is not None:
        return False
    if obj.parent and obj.parent.type == "ARMATURE":
        return False
    if any(mod.type == "ARMATURE" for mod in obj.modifiers):
        return False
    if getattr(obj, "library", None) is not None or getattr(obj.data, "library", None) is not None:
        return False
    return True


def ta_collect_material_inventory(obj):
    """Collect material/image facts without deciding whether they are good or bad."""
    materials = []
    if obj is None or obj.type != "MESH":
        return materials

    seen = set()
    for slot in obj.material_slots:
        material = getattr(slot, "material", None)
        if material is None or material.as_pointer() in seen:
            continue
        seen.add(material.as_pointer())

        textures = []
        tree = getattr(material, "node_tree", None)
        if getattr(material, "use_nodes", False) and tree:
            for node in tree.nodes:
                if getattr(node, "type", "") != "TEX_IMAGE":
                    continue
                image = getattr(node, "image", None)
                if image is None:
                    continue

                raw_path = str(getattr(image, "filepath", "") or "")
                path = bpy.path.abspath(raw_path) if raw_path else ""
                source = str(getattr(image, "source", "FILE") or "FILE")
                packed = bool(getattr(image, "packed_file", None))
                generated = source == "GENERATED"
                tiled = source == "TILED"
                tile_count = len(getattr(image, "tiles", [])) if tiled else 1

                exists = packed or generated
                if not exists and path:
                    if tiled:
                        # A UDIM path often points at 1001 while Blender owns the rest.
                        exists = os.path.isfile(path) or tile_count > 0
                    else:
                        exists = os.path.isfile(path)

                links = []
                for output in getattr(node, "outputs", []):
                    for link in getattr(output, "links", []):
                        target = getattr(link, "to_socket", None)
                        target_node = getattr(link, "to_node", None)
                        if target:
                            links.append(str(getattr(target, "name", "")))
                        if target_node:
                            links.append(str(getattr(target_node, "name", "")))

                size = list(getattr(image, "size", (0, 0)))
                width = int(size[0]) if len(size) > 0 else 0
                height = int(size[1]) if len(size) > 1 else 0
                color_space = ""
                try:
                    color_space = str(image.colorspace_settings.name)
                except Exception:
                    pass

                hint = " ".join([node.name, node.label, image.name] + links)
                textures.append({
                    "name": image.name,
                    "path": path,
                    "width": width,
                    "height": height,
                    "channels": int(getattr(image, "channels", 4) or 4),
                    "bits_per_channel": 8,
                    "color_space": color_space,
                    "semantic": infer_texture_semantic(hint, path),
                    "semantic_hint": hint,
                    "exists": exists,
                    "used": any(bool(getattr(output, "is_linked", False)) for output in getattr(node, "outputs", [])),
                    "is_udim": tiled,
                    "udim_tiles": max(1, tile_count),
                    "source": source,
                })

        materials.append({
            "name": material.name,
            "textures": textures,
        })

    return materials


def ta_material_texture_analysis(obj, profile):
    inventory = ta_collect_material_inventory(obj)
    return analyze_material_inventory(inventory, profile=profile, object_name=obj.name if obj else "")


def ta_metric_for_budget(report, mode):
    metrics = report.metrics
    return {
        "ENGINE": int(metrics.get("render_vertices", 0)),
        "TRIS": int(metrics.get("triangles", 0)),
        "FACES": int(metrics.get("faces", 0)),
        "VERTS": int(metrics.get("model_vertices", 0)),
    }.get(mode, 0)


def ta_skinning_details(obj, max_influences=None, tolerance=0.01):
    """Return skin-weight quality metrics for a Blender mesh."""
    result = {
        "has_skinning": False,
        "armature": None,
        "deform_bones": 0,
        "max_influences_found": 0,
        "unweighted_vertices": [],
        "over_influence_vertices": [],
        "non_normalized_vertices": [],
    }

    if obj is None or obj.type != "MESH":
        return result

    armature_obj = None
    for modifier in obj.modifiers:
        if modifier.type == "ARMATURE" and getattr(modifier, "object", None):
            armature_obj = modifier.object
            break

    if armature_obj is None or armature_obj.type != "ARMATURE":
        return result

    deform_names = {bone.name for bone in armature_obj.data.bones if bone.use_deform}
    if not deform_names:
        return result

    group_names = {group.index: group.name for group in obj.vertex_groups}
    result["has_skinning"] = True
    result["armature"] = armature_obj.name
    result["deform_bones"] = len(deform_names)

    for vertex in obj.data.vertices:
        weights = []
        for membership in vertex.groups:
            group_name = group_names.get(membership.group)
            if group_name in deform_names and membership.weight > 1e-8:
                weights.append(float(membership.weight))

        influence_count = len(weights)
        result["max_influences_found"] = max(
            result["max_influences_found"],
            influence_count,
        )

        if influence_count == 0:
            result["unweighted_vertices"].append(vertex.index)
            continue

        if max_influences is not None and influence_count > int(max_influences):
            result["over_influence_vertices"].append(vertex.index)

        if abs(sum(weights) - 1.0) > float(tolerance):
            result["non_normalized_vertices"].append(vertex.index)

    return result


def ta_asset_doctor_analyze_object(context, obj):
    scene = context.scene
    profile = get_profile(scene.ta_profile_id)
    report = AssetReport(obj.name, "Blender", profile["id"])

    split = ta_get_render_split_breakdown(obj, context)
    uv_result = ta_check_object_uvs(obj)
    topology = get_non_manifold_edges(obj) or {
        "wire_edges": [],
        "boundary_edges": [],
        "multiface_edges": [],
        "non_contiguous_edges": [],
    }
    zero_geo_faces = ta_get_zero_area_faces(obj)
    skinning = ta_skinning_details(
        obj,
        max_influences=profile.get("max_skin_influences"),
        tolerance=profile.get("weight_sum_tolerance", 0.01),
    )
    td_analysis = ta_texel_density_analysis(obj, scene)
    uv_layout = ta_uv_layout_analysis(obj, scene.ta_td_texture_size, 0)
    island_td = summarize_island_texel_density(
        ta_face_texel_samples(obj, scene),
        uv_layout.get("islands", []),
        scene.ta_td_texture_size,
    )
    lightmap_index = int(profile.get("lightmap_uv_channel", 1))
    lightmap_layout = (
        ta_uv_layout_analysis(obj, scene.ta_td_texture_size, lightmap_index)
        if len(obj.data.uv_layers) > lightmap_index else None
    )
    material_analysis = ta_material_texture_analysis(obj, profile)
    color_channels = 0
    if hasattr(obj.data, "color_attributes"):
        color_channels = sum(
            1 for attribute in obj.data.color_attributes
            if attribute.name != TA_TD_HEATMAP_NAME
        )
    memory = estimate_mesh_memory(
        int(split.get("render_vertices", 0)),
        int(split.get("triangle_count", 0)),
        uv_channels=len(obj.data.uv_layers),
        has_skinning=bool(skinning.get("has_skinning")),
        color_channels=color_channels,
    )
    modular = None
    if profile.get("modular_grid_cm"):
        modular = ta_modular_analysis_for_object(
            context,
            obj,
            grid_cm=float(profile.get("modular_grid_cm")),
            tolerance_cm=float(profile.get("modular_grid_tolerance_cm", 0.1)),
            check_position=bool(profile.get("modular_check_position", False)),
        )

    report.metrics.update({
        "model_vertices": int(get_mesh_metrics(obj, context, "VERTS")),
        "faces": int(split.get("face_count", len(obj.data.polygons))),
        "triangles": int(split.get("triangle_count", 0)),
        "render_vertices": int(split.get("render_vertices", 0)),
        "render_vertex_overhead": int(split.get("extra_vertices", 0)),
        "render_vertex_ratio": float(split.get("split_ratio", 0.0)),
        "material_slots": len(obj.material_slots),
        "uv_channels": len(obj.data.uv_layers),
        "render_split_stages": split.get("stages", []),
        "has_skinning": bool(skinning.get("has_skinning")),
        "deform_bones": int(skinning.get("deform_bones", 0)),
        "max_skin_influences": int(skinning.get("max_influences_found", 0)),
        "unweighted_vertices": len(skinning.get("unweighted_vertices", [])),
        "non_normalized_vertices": len(skinning.get("non_normalized_vertices", [])),
        "texel_density_px_per_m": float(td_analysis.get("density_px_per_m", 0.0)),
        "texel_density_px_per_cm": float(td_analysis.get("density_px_per_cm", 0.0)),
        "texel_density_min_px_per_m": float(td_analysis.get("min_px_per_m", 0.0)),
        "texel_density_max_px_per_m": float(td_analysis.get("max_px_per_m", 0.0)),
        "texel_density_spread_percent": float(td_analysis.get("spread_percent", 0.0)),
        "texel_density_outliers": len(td_analysis.get("outlier_components", [])),
        "texel_density_islands": len(island_td),
        "uv_island_count": int(uv_layout.get("island_count", 0)),
        "uv_mirrored_islands": int(uv_layout.get("mirrored_island_count", 0)),
        "uv_overlap_faces": len(uv_layout.get("overlap_components", [])),
        "uv_utilization_percent": float(uv_layout.get("utilization_percent", 0.0)),
        "uv_min_padding_px": float(uv_layout.get("min_island_padding_px", 0.0)),
        "lightmap_uv_channel": lightmap_index if lightmap_layout is not None else -1,
        "lightmap_overlap_faces": len(lightmap_layout.get("overlap_components", [])) if lightmap_layout else 0,
        "lightmap_min_padding_px": float(lightmap_layout.get("min_island_padding_px", 0.0)) if lightmap_layout else 0.0,
        "mesh_buffer_bytes": int(memory.get("mesh_buffer_bytes", 0)),
        "vertex_buffer_bytes": int(memory.get("vertex_buffer_bytes", 0)),
        "index_buffer_bytes": int(memory.get("index_buffer_bytes", 0)),
        "vertex_stride_bytes": int(memory.get("vertex_stride_bytes", 0)),
        "index_size_bytes": int(memory.get("index_size_bytes", 0)),
        "render_split_dominant": split.get("dominant_stage"),
        "material_count": int(material_analysis.get("metrics", {}).get("material_count", 0)),
        "texture_node_count": int(material_analysis.get("metrics", {}).get("texture_node_count", 0)),
        "texture_count": int(material_analysis.get("metrics", {}).get("texture_count", 0)),
        "missing_texture_count": int(material_analysis.get("metrics", {}).get("missing_texture_count", 0)),
        "udim_texture_count": int(material_analysis.get("metrics", {}).get("udim_texture_count", 0)),
        "texture_reference_bytes": int(material_analysis.get("metrics", {}).get("texture_reference_bytes", 0)),
        "texture_uncompressed_bytes": int(material_analysis.get("metrics", {}).get("texture_uncompressed_bytes", 0)),
        "modular_grid_cm": float(modular.get("grid_cm", 0.0)) if modular else 0.0,
        "modular_dimensions_cm": list(modular.get("dimensions_cm", ())) if modular else [],
        "modular_dimensions_on_grid": bool(modular.get("dimensions_on_grid", True)) if modular else True,
        "modular_position_on_grid": bool(modular.get("position_on_grid", True)) if modular else True,
    })
    report.metadata["material_analysis"] = material_analysis
    report.metadata["uv_layout"] = uv_layout
    report.metadata["island_texel_density"] = island_td
    if lightmap_layout is not None:
        report.metadata["lightmap_uv_layout"] = lightmap_layout
    report.extend(material_analysis.get("issues", []))

    def add(rule_id, title, message, category, severity, component_type="", components=None, safe_fix_id=None, metadata=None):
        if not severity:
            return
        report.add_issue(ValidationIssue(
            rule_id=rule_id,
            title=title,
            message=message,
            category=category,
            severity=severity,
            object_name=obj.name,
            component_type=component_type,
            components=list(components or []),
            safe_fix_id=safe_fix_id,
            metadata=dict(metadata or {}),
        ))

    if topology["wire_edges"]:
        add(
            "geometry.wire_edges",
            "Wire edges",
            "{0} edge(s) are not connected to any face.".format(len(topology["wire_edges"])),
            "Geometry",
            profile.get("wire_edge_severity", "ERROR"),
            "EDGE",
            topology["wire_edges"],
        )

    if topology["boundary_edges"]:
        add(
            "geometry.open_boundaries",
            "Open boundaries",
            "{0} boundary edge(s). This can be intentional for planes, shells and modular seams.".format(len(topology["boundary_edges"])),
            "Geometry",
            profile.get("open_boundary_severity", "INFO"),
            "EDGE",
            topology["boundary_edges"],
        )

    if topology["multiface_edges"]:
        add(
            "geometry.multiface_edges",
            "Edges shared by more than two faces",
            "{0} edge(s) have more than two linked faces.".format(len(topology["multiface_edges"])),
            "Geometry",
            profile.get("multiface_edge_severity", "ERROR"),
            "EDGE",
            topology["multiface_edges"],
        )

    if topology["non_contiguous_edges"]:
        add(
            "geometry.non_contiguous_edges",
            "Non-contiguous edges",
            "{0} two-face edge(s) are still non-manifold.".format(len(topology["non_contiguous_edges"])),
            "Geometry",
            profile.get("non_contiguous_edge_severity", "ERROR"),
            "EDGE",
            topology["non_contiguous_edges"],
        )

    if zero_geo_faces:
        add(
            "geometry.zero_area_faces",
            "Zero-area faces",
            "{0} face(s) have effectively zero geometric area.".format(len(zero_geo_faces)),
            "Geometry",
            profile.get("zero_geo_area_severity", "ERROR"),
            "FACE",
            zero_geo_faces,
        )

    scale = tuple(float(v) for v in obj.scale)
    negative_scale = any(v < 0.0 for v in scale)
    non_unit_scale = any(abs(v - 1.0) > 0.0001 for v in scale)

    if negative_scale:
        add(
            "transform.negative_scale",
            "Negative scale",
            "Object scale is ({0:.4g}, {1:.4g}, {2:.4g}); mirrored transforms can change winding and tangent behavior.".format(*scale),
            "Transform",
            profile.get("negative_scale_severity", "ERROR"),
        )
    elif non_unit_scale:
        safe_fix = "apply_positive_scale" if ta_can_apply_scale_safely(obj) else None
        add(
            "transform.non_unit_scale",
            "Scale not applied",
            "Object scale is ({0:.4g}, {1:.4g}, {2:.4g}) instead of (1, 1, 1).".format(*scale),
            "Transform",
            profile.get("non_unit_scale_severity", "WARNING"),
            safe_fix_id=safe_fix,
        )

    if modular:
        if modular.get("dimension_error_axes"):
            add(
                "modular.dimensions_off_grid",
                "Bounds do not fit the modular grid",
                "Dimension axis/axes {0} are not multiples of the {1:g} cm grid within {2:g} cm tolerance.".format(
                    ", ".join(modular["dimension_error_axes"]), modular["grid_cm"], modular["tolerance_cm"]
                ),
                "Modular",
                profile.get("modular_dimension_severity", "WARNING"),
                metadata={"axes": list(modular["dimension_error_axes"]), "grid_cm": modular["grid_cm"]},
            )
        if profile.get("modular_check_position") and modular.get("position_error_axes"):
            add(
                "modular.position_off_grid",
                "Bounds are offset from the modular grid",
                "World bound axis/axes {0} miss the {1:g} cm grid. This can be fine while authoring, but it is worth catching before kit handoff.".format(
                    ", ".join(modular["position_error_axes"]), modular["grid_cm"]
                ),
                "Modular",
                profile.get("modular_position_severity", "INFO"),
                metadata={"axes": list(modular["position_error_axes"]), "grid_cm": modular["grid_cm"]},
            )

    if profile.get("require_uvs") and uv_result["missing_uvs"]:
        add(
            "uv.missing",
            "Missing UVs",
            "The selected profile requires at least one UV channel.",
            "UV",
            "ERROR",
        )

    if profile.get("require_uv_01") and uv_result["uvs_out_of_range"]:
        add(
            "uv.outside_01",
            "UVs outside 0-1",
            "This profile requires the active UV set to remain inside the 0-1 tile.",
            "UV",
            "ERROR",
        )

    if uv_result["flipped_faces"]:
        add(
            "uv.flipped_faces",
            "Flipped UV winding",
            "{0} face(s) have negative signed UV area. Mirroring may be intentional.".format(len(uv_result["flipped_faces"])),
            "UV",
            profile.get("flipped_uv_severity", "INFO"),
            "FACE",
            uv_result["flipped_faces"],
        )

    if uv_result["zero_faces"]:
        add(
            "uv.zero_area_faces",
            "Zero-area UV faces",
            "{0} face(s) collapse to zero area in the active UV set.".format(len(uv_result["zero_faces"])),
            "UV",
            profile.get("zero_uv_area_severity", "ERROR"),
            "FACE",
            uv_result["zero_faces"],
        )

    if uv_layout.get("overlap_components"):
        add(
            "uv.overlap", "Overlapping UV islands",
            "{0} face(s) participate in overlap between separate UV islands.".format(len(uv_layout["overlap_components"])),
            "UV", profile.get("uv_overlap_severity", "INFO"), "FACE", uv_layout["overlap_components"],
            metadata={"overlap_pairs": int(uv_layout.get("overlap_pair_count", 0))},
        )

    min_utilization = profile.get("min_uv_utilization_percent")
    if min_utilization is not None and uv_layout.get("utilization_percent", 0.0) < float(min_utilization):
        add(
            "uv.low_utilization", "Low UV utilization",
            "Estimated 0-1 utilization is {0:.1f}%; profile target is at least {1:.1f}%.".format(uv_layout.get("utilization_percent", 0.0), float(min_utilization)),
            "UV", profile.get("uv_utilization_severity", "INFO"),
        )

    if profile.get("audit_lightmap_uv"):
        if lightmap_layout is None:
            add(
                "uv.lightmap_missing", "Lightmap UV channel missing",
                "Profile audits UV channel {0} for lightmap suitability, but the mesh only has {1} channel(s).".format(lightmap_index, len(obj.data.uv_layers)),
                "UV", profile.get("missing_lightmap_uv_severity", "INFO"),
            )
        else:
            if lightmap_layout.get("overlap_components"):
                add(
                    "uv.lightmap_overlap", "Lightmap UV overlap",
                    "{0} face(s) overlap in UV channel {1}.".format(len(lightmap_layout["overlap_components"]), lightmap_index),
                    "UV", profile.get("lightmap_overlap_severity", "WARNING"), "FACE", lightmap_layout["overlap_components"],
                )
            minimum_padding = float(profile.get("min_lightmap_padding_px", 0.0) or 0.0)
            measured_padding = float(lightmap_layout.get("min_island_padding_px", 0.0))
            if minimum_padding > 0.0 and lightmap_layout.get("island_count", 0) > 1 and measured_padding < minimum_padding:
                add(
                    "uv.lightmap_padding", "Lightmap island padding",
                    "Closest lightmap islands are about {0:.2f}px apart at {1}px; profile target is {2:.2f}px.".format(measured_padding, scene.ta_td_texture_size, minimum_padding),
                    "UV", profile.get("lightmap_padding_severity", "INFO"),
                )

    if scene.ta_enforce_td_target and td_analysis.get("outlier_components"):
        outliers = list(td_analysis.get("outlier_components", []))
        target_label = "{0:.3f} {1}".format(scene.ta_td_target, ta_density_unit_label(scene))
        add(
            "uv.texel_density_outliers",
            "Texel-density outliers",
            "{0} face(s) fall outside ±{1:.1f}% of the {2} target.".format(
                len(outliers), scene.ta_td_tolerance, target_label
            ),
            "UV",
            "WARNING",
            "FACE",
            outliers,
            metadata={
                "target_px_per_m": ta_density_from_display(scene.ta_td_target, scene),
                "tolerance_percent": scene.ta_td_tolerance,
            },
        )

    if skinning.get("has_skinning"):
        if skinning["unweighted_vertices"]:
            add(
                "skinning.unweighted_vertices",
                "Unweighted vertices",
                "{0} vertex/vertices have no weight on a deform bone.".format(len(skinning["unweighted_vertices"])),
                "Skinning",
                profile.get("unweighted_vertex_severity", "ERROR"),
                "VERT",
                skinning["unweighted_vertices"],
            )

        max_influences = profile.get("max_skin_influences")
        if max_influences is not None and skinning["over_influence_vertices"]:
            add(
                "skinning.influence_limit",
                "Too many skin influences",
                "{0} vertex/vertices exceed the profile limit of {1} influences.".format(
                    len(skinning["over_influence_vertices"]), max_influences
                ),
                "Skinning",
                profile.get("skin_influence_severity", "ERROR"),
                "VERT",
                skinning["over_influence_vertices"],
            )

        if skinning["non_normalized_vertices"]:
            add(
                "skinning.non_normalized_weights",
                "Weights not normalized",
                "{0} vertex/vertices have deform weights that do not sum to 1 within tolerance.".format(
                    len(skinning["non_normalized_vertices"])
                ),
                "Skinning",
                profile.get("weight_normalization_severity", "WARNING"),
                "VERT",
                skinning["non_normalized_vertices"],
            )

    max_material_slots = profile.get("max_material_slots")
    if max_material_slots is not None and len(obj.material_slots) > int(max_material_slots):
        add(
            "performance.material_slots",
            "High material-section count",
            "{0} material slot(s); profile target is at most {1}.".format(len(obj.material_slots), max_material_slots),
            "Performance",
            "WARNING",
        )

    # The budget panel is a project-specific numeric override, not an
    # intrinsic asset-profile rule. Asset Doctor only enforces it when asked.
    if scene.ta_enforce_project_budget:
        count = ta_metric_for_budget(report, scene.ee_budget_count_mode)
        budget = max(int(scene.ee_poly_budget), 1)
        margin = int(budget * (float(scene.ee_poly_margin) / 100.0))
        warn_at = max(0, budget - margin)
        error_at = budget + margin
        unit = get_budget_count_label(scene)

        if count > error_at:
            add(
                "performance.project_budget",
                "Project budget exceeded",
                "{0:,} {1}; configured upper limit with margin is {2:,}.".format(count, unit, error_at),
                "Performance",
                "ERROR",
                metadata={"count": count, "limit": error_at, "mode": scene.ee_budget_count_mode},
            )
        elif count >= warn_at:
            add(
                "performance.project_budget_margin",
                "Near project budget",
                "{0:,} {1}; configured budget is {2:,}.".format(count, unit, budget),
                "Performance",
                "WARNING",
                metadata={"count": count, "limit": budget, "mode": scene.ee_budget_count_mode},
            )

    run_registered_rules(
        report,
        profile,
        {
            "object_name": report.object_name,
            "metrics": report.metrics,
            "metadata": report.metadata,
        },
    )
    return report


def ta_analyze_selection(context):
    global TA_LAST_REPORTS
    TA_LAST_REPORTS = [
        ta_asset_doctor_analyze_object(context, obj)
        for obj in get_selected_meshes(context)
    ]
    return TA_LAST_REPORTS


def ta_asset_doctor_summary(reports):
    if not reports:
        return "No mesh objects selected"
    errors = sum(report.error_count for report in reports)
    warnings = sum(report.warning_count for report in reports)
    safe = sum(report.safe_fix_count for report in reports)
    return "{0} asset(s) · {1} error(s) · {2} warning(s) · {3} safe fix(es)".format(
        len(reports), errors, warnings, safe
    )


def ta_apply_safe_issue_fix(context, report, issue):
    obj = bpy.data.objects.get(report.object_name)
    if obj is None:
        return False

    if issue.safe_fix_id == "apply_positive_scale" and ta_can_apply_scale_safely(obj):
        previous_selected = list(context.selected_objects)
        previous_active = context.view_layer.objects.active
        previous_mode = context.mode

        try:
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            context.view_layer.objects.active = obj
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
            return True
        finally:
            try:
                bpy.ops.object.select_all(action="DESELECT")
                for selected in previous_selected:
                    if selected and selected.name in bpy.data.objects:
                        selected.select_set(True)
                if previous_active and previous_active.name in bpy.data.objects:
                    context.view_layer.objects.active = previous_active
            except Exception:
                pass

    return False


def ta_select_issue_components(context, report, issue):
    obj = bpy.data.objects.get(report.object_name)
    if obj is None:
        return False

    if context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    context.view_layer.objects.active = obj

    if not issue.components or issue.component_type not in {"VERT", "EDGE", "FACE"}:
        return True

    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="DESELECT")
    bm = bmesh.from_edit_mesh(obj.data)

    if issue.component_type == "VERT":
        bm.verts.ensure_lookup_table()
        for index in issue.components:
            if isinstance(index, int) and 0 <= index < len(bm.verts):
                bm.verts[index].select = True
        context.tool_settings.mesh_select_mode = (True, False, False)
    elif issue.component_type == "EDGE":
        bm.edges.ensure_lookup_table()
        for index in issue.components:
            if isinstance(index, int) and 0 <= index < len(bm.edges):
                bm.edges[index].select = True
        context.tool_settings.mesh_select_mode = (False, True, False)
    else:
        bm.faces.ensure_lookup_table()
        for index in issue.components:
            if isinstance(index, int) and 0 <= index < len(bm.faces):
                bm.faces[index].select = True
        context.tool_settings.mesh_select_mode = (False, False, True)

    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return True


class TA_OT_create_attachment(bpy.types.Operator):
    bl_idname = "ta.create_attachment"
    bl_label = "Create Attachment"
    bl_description = "Create a socket/helper and attach it to the current Asset Set"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        active = context.active_object
        root = scene.ta_asset_set_root.strip() or ta_asset_set_for_active(context)
        if not root and active is not None:
            root = infer_asset_member(active.name).root_name
        if not root:
            self.report({"WARNING"}, "Create/activate an Asset Set or select an asset object first")
            return {"CANCELLED"}

        source = [obj for obj in context.selected_objects if obj.type in {"MESH", "EMPTY", "ARMATURE"}]
        location = ta_attachment_world_location(context, source, scene.ta_attachment_location)
        role = scene.ta_attachment_role
        name = make_attachment_name(root, scene.ta_attachment_label, role)
        empty = bpy.data.objects.new(name, None)
        empty.empty_display_type = "PLAIN_AXES" if role == ROLE_SOCKET else "CUBE"
        empty.empty_display_size = max(0.001, float(scene.ta_attachment_size_cm) / ta_blender_cm_per_unit(context))
        collection = active.users_collection[0] if active is not None and active.users_collection else context.collection
        collection.objects.link(empty)
        empty.matrix_world.translation = location

        # Parenting is optional; sockets often want it, scene-level helpers often don't.
        if scene.ta_attachment_parent and active is not None:
            world = empty.matrix_world.copy()
            empty.parent = active
            empty.matrix_world = world

        ta_set_asset_membership(empty, root, role=role, lod_level=0, collision_type="", export_enabled=True)
        scene.ta_asset_set_root = root
        ta_select_only(context, [empty], empty)
        ta_build_asset_set_analysis(context, root)
        self.report({"INFO"}, "Created {0}".format(empty.name))
        return {"FINISHED"}


class TA_OT_modular_analyze(bpy.types.Operator):
    bl_idname = "ta.modular_analyze"
    bl_label = "Check Modular Bounds"
    bl_description = "Check selected mesh bounds against the authoring grid"

    def execute(self, context):
        meshes = get_selected_meshes(context)
        if not meshes:
            self.report({"WARNING"}, "Select one or more mesh objects")
            return {"CANCELLED"}
        bad_dimensions = []
        bad_positions = []
        for obj in meshes:
            result = ta_modular_analysis_for_object(context, obj)
            if result and not result.get("dimensions_on_grid", True):
                bad_dimensions.append(obj.name)
            if result and context.scene.ta_modular_check_position and not result.get("position_on_grid", True):
                bad_positions.append(obj.name)

        scene = context.scene
        scene.ta_modular_result = "{0} checked · {1} size issue(s) · {2} placement issue(s)".format(
            len(meshes), len(bad_dimensions), len(bad_positions)
        )
        if bad_dimensions or bad_positions:
            self.report({"WARNING"}, scene.ta_modular_result)
        else:
            self.report({"INFO"}, scene.ta_modular_result)
        return {"FINISHED"}


class TA_OT_modular_snap(bpy.types.Operator):
    bl_idname = "ta.modular_snap"
    bl_label = "Snap Origins to Grid"
    bl_description = "Snap selected object translations to the physical modular grid without touching mesh data"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        selected = list(context.selected_objects)
        if not selected:
            return {"CANCELLED"}
        scene = context.scene
        factor = ta_blender_cm_per_unit(context)
        axes = (scene.ta_modular_snap_x, scene.ta_modular_snap_y, scene.ta_modular_snap_z)
        changed = 0
        for obj in selected:
            location_cm = tuple(float(v) * factor for v in obj.matrix_world.translation)
            snapped_cm = snap_vector(location_cm, scene.ta_modular_grid_cm, axes, scene.ta_modular_snap_mode)
            target = Vector(tuple(v / factor for v in snapped_cm))
            if (target - obj.matrix_world.translation).length > 1e-10:
                obj.matrix_world.translation = target
                changed += 1
        self.report({"INFO"}, "Snapped {0} object(s)".format(changed))
        return {"FINISHED"}


class TA_OT_modular_align_anchor(bpy.types.Operator):
    bl_idname = "ta.modular_align_anchor"
    bl_label = "Align Bounds to Active"
    bl_description = "Translate selected objects so a chosen bounds anchor matches the active object's anchor"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        active = context.active_object
        selected = [obj for obj in context.selected_objects if obj is not active]
        if active is None or not selected:
            self.report({"WARNING"}, "Select target objects and an active reference object")
            return {"CANCELLED"}
        active_min, active_max = ta_world_bounds([active])
        if active_min is None:
            return {"CANCELLED"}
        anchor = context.scene.ta_modular_align_anchor
        target_anchor = Vector(bounds_anchor(tuple(active_min), tuple(active_max), anchor))
        moved = 0
        for obj in selected:
            minimum, maximum = ta_world_bounds([obj])
            if minimum is None:
                continue
            current = Vector(bounds_anchor(tuple(minimum), tuple(maximum), anchor))
            obj.matrix_world.translation += target_anchor - current
            moved += 1
        self.report({"INFO"}, "Aligned {0} object(s) to {1}".format(moved, anchor))
        return {"FINISHED"}


class TA_OT_modular_match_bounds(bpy.types.Operator):
    bl_idname = "ta.modular_match_bounds"
    bl_label = "Match Bounds to Active"
    bl_description = "Scale axis-aligned selected meshes so their world AABB dimensions match the active reference"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        active = context.active_object
        movers = [obj for obj in context.selected_objects if obj is not active and obj.type == "MESH"]
        if active is None or active.type != "MESH" or not movers:
            self.report({"WARNING"}, "Select mesh objects to resize and an active mesh reference")
            return {"CANCELLED"}

        active_rotation = active.matrix_world.to_euler()
        if any(abs(float(angle)) > 1e-5 for angle in active_rotation):
            self.report({"ERROR"}, "Bounds matching requires an axis-aligned active reference")
            return {"CANCELLED"}

        target_min, target_max = ta_world_bounds([active])
        target_dims = tuple(float(target_max[i] - target_min[i]) for i in range(3))
        axes = (scene.ta_modular_match_x, scene.ta_modular_match_y, scene.ta_modular_match_z)
        matched = 0
        skipped = 0
        for obj in movers:
            rotation = obj.matrix_world.to_euler()
            if any(abs(float(angle)) > 1e-5 for angle in rotation):
                skipped += 1
                continue
            minimum, maximum = ta_world_bounds([obj])
            if minimum is None:
                skipped += 1
                continue
            source_dims = tuple(float(maximum[i] - minimum[i]) for i in range(3))
            factors = bounds_scale_factors(source_dims, target_dims, axes)
            obj.scale = tuple(float(obj.scale[i]) * float(factors[i]) for i in range(3))
            matched += 1

        self.report({"INFO"}, "Matched {0} object(s); skipped {1} rotated/invalid".format(matched, skipped))
        return {"FINISHED"}


class TA_OT_normalize_transforms(bpy.types.Operator):
    bl_idname = "ta.normalize_transforms"
    bl_label = "Normalize Transforms"
    bl_description = "Apply selected transform channels in batch; negative scale is skipped unless explicitly allowed"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        objects = [obj for obj in context.selected_objects if obj.type in {"MESH", "ARMATURE", "EMPTY"}]
        if not objects:
            return {"CANCELLED"}
        skipped = 0
        applied = 0
        original_active = context.view_layer.objects.active
        original_selection = list(context.selected_objects)
        try:
            for obj in objects:
                if scene.ta_normalize_scale and any(v < 0.0 for v in obj.scale) and not scene.ta_normalize_allow_negative:
                    skipped += 1
                    continue
                ta_select_only(context, [obj], obj)
                bpy.ops.object.transform_apply(
                    location=False,
                    rotation=bool(scene.ta_normalize_rotation),
                    scale=bool(scene.ta_normalize_scale),
                )
                applied += 1
        finally:
            ta_select_only(context, original_selection, original_active)
        self.report({"INFO"}, "Normalized {0} object(s); skipped {1}".format(applied, skipped))
        return {"FINISHED"}


class TA_OT_asset_set_create(bpy.types.Operator):
    bl_idname = "ta.asset_set_create"
    bl_label = "Create / Update Asset Set"
    bl_description = "Persist explicit Asset Set membership on the current selection; names are used only to bootstrap roles"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        selected = list(context.selected_objects)
        if not selected:
            self.report({"WARNING"}, "Select at least one object")
            return {"CANCELLED"}

        requested = context.scene.ta_asset_set_root.strip()
        root = requested or canonical_root_from_members(
            [obj.name for obj in selected],
            context.active_object.name if context.active_object else "",
        )
        if not root:
            self.report({"ERROR"}, "Could not determine an Asset Set root")
            return {"CANCELLED"}

        for obj in selected:
            inferred = infer_asset_member(obj.name, root)
            role = inferred.role
            if obj.type == "ARMATURE":
                role = ROLE_SKELETON
            elif obj.type == "EMPTY" and role == ROLE_RENDER:
                role = ROLE_SOCKET
            # Once the root is explicit, all selected parts belong to that set,
            # even if their names do not independently encode it.
            ta_set_asset_membership(
                obj,
                root,
                role=role,
                lod_level=inferred.lod_level,
                collision_type=inferred.collision_type,
                export_enabled=True,
            )

        context.scene.ta_asset_set_root = root
        analysis = ta_build_asset_set_analysis(context, root)
        self.report({"INFO"}, "Asset Set {0}: {1} member(s)".format(root, analysis["metrics"]["member_count"]))
        return {"FINISHED"}


class TA_OT_asset_set_assign_role(bpy.types.Operator):
    bl_idname = "ta.asset_set_assign_role"
    bl_label = "Assign Role"
    bl_description = "Assign the selected objects to the active Asset Set with the chosen production role"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        selected = list(context.selected_objects)
        if not selected:
            self.report({"WARNING"}, "Select one or more objects")
            return {"CANCELLED"}

        root = context.scene.ta_asset_set_root.strip() or ta_asset_set_for_active(context)
        if not root:
            root = canonical_root_from_members([obj.name for obj in selected], context.active_object.name if context.active_object else "")
        role = context.scene.ta_asset_member_role
        lod_level = int(context.scene.ta_asset_member_lod)
        collision_type = context.scene.ta_asset_collision_type if role == ROLE_COLLISION else ""

        for obj in selected:
            ta_set_asset_membership(
                obj,
                root,
                role=role,
                lod_level=lod_level,
                collision_type=collision_type,
                export_enabled=context.scene.ta_asset_member_export,
            )

        context.scene.ta_asset_set_root = root
        ta_build_asset_set_analysis(context, root)
        self.report({"INFO"}, "Assigned {0} object(s) as {1}".format(len(selected), role))
        return {"FINISHED"}


class TA_OT_asset_set_analyze(bpy.types.Operator):
    bl_idname = "ta.asset_set_analyze"
    bl_label = "Analyze Asset Set"
    bl_description = "Validate LOD progression, collision structure and Asset Set membership"

    def execute(self, context):
        root = context.scene.ta_asset_set_root.strip() or ta_asset_set_for_active(context)
        if not root:
            self.report({"WARNING"}, "Active object is not assigned to an Asset Set")
            return {"CANCELLED"}
        context.scene.ta_asset_set_root = root
        analysis = ta_build_asset_set_analysis(context, root)
        self.report({"INFO"}, context.scene.ta_asset_set_summary)
        return {"FINISHED"}


class TA_OT_asset_set_select(bpy.types.Operator):
    bl_idname = "ta.asset_set_select"
    bl_label = "Select Asset Set"
    bl_description = "Select every object explicitly assigned to the current Asset Set"

    def execute(self, context):
        root = context.scene.ta_asset_set_root.strip() or ta_asset_set_for_active(context)
        if not root:
            return {"CANCELLED"}
        members = ta_get_asset_set_members(context, root, include_disabled=True)
        ta_select_only(context, members, ta_asset_set_primary(members))
        self.report({"INFO"}, "Selected {0} member(s)".format(len(members)))
        return {"FINISHED"}


class TA_OT_asset_set_create_box_collision(bpy.types.Operator):
    bl_idname = "ta.asset_set_create_box_collision"
    bl_label = "Create Box Collision"
    bl_description = "Create an axis-aligned UBX collision box around selected render members or the Asset Set LOD0"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        root = context.scene.ta_asset_set_root.strip() or ta_asset_set_for_active(context)
        if not root:
            self.report({"WARNING"}, "Create or activate an Asset Set first")
            return {"CANCELLED"}

        source = [obj for obj in context.selected_objects if obj.type == "MESH" and ta_asset_set_root_for_object(obj) == root]
        if not source:
            source = [
                obj for obj in ta_get_asset_set_members(context, root)
                if obj.type == "MESH"
                and str(obj.get(TA_ASSET_ROLE_PROP, "")) in {ROLE_RENDER, ROLE_LOD}
                and int(obj.get(TA_ASSET_LOD_PROP, 0) or 0) == 0
            ]
        if not source:
            self.report({"ERROR"}, "Asset Set has no mesh to bound")
            return {"CANCELLED"}

        corners = []
        for obj in source:
            corners.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
        minimum = Vector((min(v.x for v in corners), min(v.y for v in corners), min(v.z for v in corners)))
        maximum = Vector((max(v.x for v in corners), max(v.y for v in corners), max(v.z for v in corners)))
        center = (minimum + maximum) * 0.5
        dimensions = maximum - minimum

        bpy.ops.mesh.primitive_cube_add(size=1.0, location=center)
        collision = context.active_object
        collision.dimensions = dimensions
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        collision.name = "UBX_{0}_{1:02d}".format(root, ta_next_collision_index(context, root))
        ta_set_asset_membership(collision, root, role=ROLE_COLLISION, lod_level=0, collision_type="BOX", export_enabled=True)
        context.scene.ta_asset_set_root = root
        ta_build_asset_set_analysis(context, root)
        self.report({"INFO"}, "Created {0}".format(collision.name))
        return {"FINISHED"}


class TA_OT_asset_set_create_sphere_collision(bpy.types.Operator):
    bl_idname = "ta.asset_set_create_sphere_collision"
    bl_label = "Create Sphere Collision"
    bl_description = "Create a USP sphere that encloses selected render members or the Asset Set LOD0"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        root = context.scene.ta_asset_set_root.strip() or ta_asset_set_for_active(context)
        if not root:
            self.report({"WARNING"}, "Create or activate an Asset Set first")
            return {"CANCELLED"}

        source = [obj for obj in context.selected_objects if obj.type == "MESH" and ta_asset_set_root_for_object(obj) == root]
        if not source:
            source = [
                obj for obj in ta_get_asset_set_members(context, root)
                if obj.type == "MESH"
                and str(obj.get(TA_ASSET_ROLE_PROP, "")) in {ROLE_RENDER, ROLE_LOD}
                and int(obj.get(TA_ASSET_LOD_PROP, 0) or 0) == 0
            ]
        if not source:
            self.report({"ERROR"}, "Asset Set has no mesh to bound")
            return {"CANCELLED"}

        corners = []
        for obj in source:
            corners.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
        minimum = Vector((min(v.x for v in corners), min(v.y for v in corners), min(v.z for v in corners)))
        maximum = Vector((max(v.x for v in corners), max(v.y for v in corners), max(v.z for v in corners)))
        center = (minimum + maximum) * 0.5

        # AABB radius is intentionally conservative. Collision helpers should not quietly clip
        # corners when the source has strongly unequal extents.
        radius = max((corner - center).length for corner in corners)
        bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=max(radius, 1e-6), location=center)
        collision = context.active_object
        collision.name = "USP_{0}_{1:02d}".format(root, ta_next_collision_index(context, root, "USP"))
        ta_set_asset_membership(collision, root, role=ROLE_COLLISION, lod_level=0, collision_type="SPHERE", export_enabled=True)
        context.scene.ta_asset_set_root = root
        ta_build_asset_set_analysis(context, root)
        self.report({"INFO"}, "Created {0}".format(collision.name))
        return {"FINISHED"}


class TA_OT_asset_set_generate_lod(bpy.types.Operator):
    bl_idname = "ta.asset_set_generate_lod"
    bl_label = "Generate LOD"
    bl_description = "Duplicate selected render members into a target LOD and add a non-destructive Decimate modifier"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        root = context.scene.ta_asset_set_root.strip() or ta_asset_set_for_active(context)
        level = int(context.scene.ta_lod_generate_level)
        ratio = float(context.scene.ta_lod_generate_ratio)
        source = [
            obj for obj in context.selected_objects
            if obj.type == "MESH"
            and ta_asset_set_root_for_object(obj) == root
            and str(obj.get(TA_ASSET_ROLE_PROP, ROLE_RENDER)) in {ROLE_RENDER, ROLE_LOD}
        ]
        if not root or not source:
            self.report({"WARNING"}, "Select render members from an Asset Set")
            return {"CANCELLED"}
        if level <= 0:
            self.report({"ERROR"}, "Generated LOD level must be 1 or higher")
            return {"CANCELLED"}

        created = []
        for obj in source:
            dup = obj.copy()
            dup.data = obj.data.copy()
            for collection in obj.users_collection:
                collection.objects.link(dup)
            base_name = obj.name
            if "_LOD" in base_name.upper():
                index = base_name.upper().rfind("_LOD")
                suffix = base_name[index + 4:]
                if suffix.isdigit():
                    base_name = base_name[:index]
            dup.name = "{0}_LOD{1}".format(base_name, level)
            modifier = dup.modifiers.new(name="JAM_LOD_DECIMATE", type="DECIMATE")
            modifier.ratio = ratio
            ta_set_asset_membership(dup, root, role=ROLE_LOD, lod_level=level, collision_type="", export_enabled=True)
            created.append(dup)

        ta_select_only(context, created, created[0] if created else None)
        ta_build_asset_set_analysis(context, root)
        self.report({"INFO"}, "Generated LOD{0} for {1} member(s) at {2:.0f}%".format(level, len(created), ratio * 100.0))
        return {"FINISHED"}


class TA_OT_asset_set_export(bpy.types.Operator):
    bl_idname = "ta.asset_set_export"
    bl_label = "Export Asset Set"
    bl_description = "Validate and export the current Asset Set as one deterministic file with sidecar metadata"

    def execute(self, context):
        scene = context.scene
        root = scene.ta_asset_set_root.strip() or ta_asset_set_for_active(context)
        if not root:
            self.report({"WARNING"}, "Active object is not assigned to an Asset Set")
            return {"CANCELLED"}

        export_path = bpy.path.abspath(scene.ee_export_path)
        if not os.path.isdir(export_path):
            self.report({"ERROR"}, "Invalid export folder")
            return {"CANCELLED"}

        analysis = ta_build_asset_set_analysis(context, root)
        set_errors = [issue for issue in analysis.get("issues", []) if issue.severity == "ERROR"]
        reports = ta_asset_set_reports(context, analysis) if scene.ee_validate_export or scene.ee_write_sidecar else []
        report_errors = [issue for report in reports for issue in report.issues if issue.severity == "ERROR"]
        if scene.ee_validate_export and scene.ee_block_export and (set_errors or report_errors):
            self.report({"ERROR"}, "Export blocked: {0} set error(s), {1} asset error(s)".format(len(set_errors), len(report_errors)))
            return {"CANCELLED"}

        export_members = [
            obj for obj in analysis.get("objects", [])
            if bool(obj.get(TA_ASSET_EXPORT_PROP, True))
        ]
        if not export_members:
            self.report({"ERROR"}, "Asset Set has no export-enabled members")
            return {"CANCELLED"}

        original_active = context.view_layer.objects.active
        original_selection = list(context.selected_objects)
        original_matrices = {obj: obj.matrix_world.copy() for obj in export_members}
        plan = build_export_plan(
            export_path, root, scene.ta_export_profile,
            [obj.name for obj in export_members], scene.ee_write_sidecar,
        )
        filepath = plan["filepath"]
        if plan.get("has_conflict") and not scene.ee_overwrite_existing:
            self.report({"ERROR"}, "Export or sidecar target exists. Enable Overwrite Existing or change the target.")
            return {"CANCELLED"}

        try:
            if scene.ta_asset_set_move_to_origin:
                primary = ta_asset_set_primary(export_members)
                if primary is not None:
                    delta = Matrix.Translation(-primary.matrix_world.translation)
                    for obj, matrix in original_matrices.items():
                        obj.matrix_world = delta @ matrix

            ta_select_only(context, export_members, ta_asset_set_primary(export_members))
            ta_export_selected_file(filepath, scene.ta_export_profile)

            if scene.ee_write_sidecar:
                if not reports:
                    reports = ta_asset_set_reports(context, analysis)
                write_export_sidecar(
                    filepath,
                    reports,
                    source_host="Blender",
                    profile_id=scene.ta_profile_id,
                    suite_version="2.4.0",
                    metadata={"export_mode": "asset_set"},
                    asset_set=ta_asset_set_sidecar_data(analysis),
                    export_profile_id=scene.ta_export_profile,
                )
        finally:
            for obj, matrix in original_matrices.items():
                try:
                    obj.matrix_world = matrix
                except Exception:
                    pass
            ta_select_only(context, original_selection, original_active)

        self.report({"INFO"}, "Exported Asset Set {0} ({1} member(s))".format(root, len(export_members)))
        return {"FINISHED"}


class TA_OT_asset_doctor_analyze(bpy.types.Operator):
    bl_idname = "ta.asset_doctor_analyze"
    bl_label = "Analyze Selection"
    bl_description = "Build a profile-aware production report for selected mesh assets"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        reports = ta_analyze_selection(context)
        context.scene.ta_asset_doctor_summary = ta_asset_doctor_summary(reports)
        self.report({"INFO"}, context.scene.ta_asset_doctor_summary)
        return {"FINISHED"}


class TA_OT_asset_doctor_fix_safe(bpy.types.Operator):
    bl_idname = "ta.asset_doctor_fix_safe"
    bl_label = "Fix Safe Issues"
    bl_description = "Apply only fixes explicitly classified as safe for the current asset state"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        # Transform baking and component selection are intentionally kept out of Edit Mode.
        return context.mode == "OBJECT" and any(
            obj.type == "MESH" for obj in context.selected_objects
        )

    def execute(self, context):
        reports = ta_analyze_selection(context)
        fixed = 0
        for report in reports:
            for issue in report.sorted_issues():
                if issue.is_safe_fixable and ta_apply_safe_issue_fix(context, report, issue):
                    fixed += 1

        reports = ta_analyze_selection(context)
        context.scene.ta_asset_doctor_summary = ta_asset_doctor_summary(reports)
        self.report({"INFO"}, "Applied {0} safe fix(es)".format(fixed))
        return {"FINISHED"}


class TA_OT_asset_doctor_select_issue(bpy.types.Operator):
    bl_idname = "ta.asset_doctor_select_issue"
    bl_label = "Select Issue"
    bl_description = "Select the object or components responsible for this issue"
    bl_options = {"REGISTER"}

    report_index: bpy.props.IntProperty(default=-1)
    issue_index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        if not (0 <= self.report_index < len(TA_LAST_REPORTS)):
            return {"CANCELLED"}
        report = TA_LAST_REPORTS[self.report_index]
        issues = report.sorted_issues()
        if not (0 <= self.issue_index < len(issues)):
            return {"CANCELLED"}
        ta_select_issue_components(context, report, issues[self.issue_index])
        return {"FINISHED"}


class TA_OT_asset_doctor_select_split_cause(bpy.types.Operator):
    bl_idname = "ta.asset_doctor_select_split_cause"
    bl_label = "Select Render Split Cause"
    bl_description = "Select source-mesh edges that create the chosen render-vertex split"
    bl_options = {"REGISTER"}

    cause: bpy.props.EnumProperty(
        items=[
            ("NORMAL", "Normals", "Select hard/split-normal boundaries"),
            ("UV", "UVs", "Select UV discontinuities across shared edges"),
            ("MATERIAL", "Materials", "Select material-section boundaries"),
        ],
        default="UV",
    )

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "MESH"

    def execute(self, context):
        obj = context.active_object
        indices = ta_get_render_split_edge_indices(obj, self.cause)
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="DESELECT")
        bpy.ops.object.mode_set(mode="OBJECT")
        for index in indices:
            if 0 <= index < len(obj.data.edges):
                obj.data.edges[index].select = True
        bpy.ops.object.mode_set(mode="EDIT")
        context.tool_settings.mesh_select_mode = (False, True, False)
        self.report({"INFO"}, "Selected {0} {1} split edge(s)".format(len(indices), self.cause.lower()))
        return {"FINISHED"}


class TA_OT_asset_doctor_export_report(bpy.types.Operator):
    bl_idname = "ta.asset_doctor_export_report"
    bl_label = "Export JSON Report"
    bl_description = "Write the current Asset Doctor results to a machine-readable JSON report"
    bl_options = {"REGISTER"}

    def execute(self, context):
        reports = TA_LAST_REPORTS or ta_analyze_selection(context)
        if not reports:
            self.report({"WARNING"}, "No mesh assets to report")
            return {"CANCELLED"}

        path = bpy.path.abspath(context.scene.ta_asset_report_path)
        if not path.lower().endswith(".json"):
            path += ".json"
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)

        write_report_json(
            path,
            reports,
            suite_version="2.4.0",
            metadata={"host": "Blender", "profile": context.scene.ta_profile_id},
        )
        self.report({"INFO"}, "Report written to {0}".format(path))
        return {"FINISHED"}


class TA_PT_asset_doctor(bpy.types.Panel):
    bl_label = "Asset Doctor"
    bl_idname = "TA_PT_asset_doctor"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TA Tools"
    bl_parent_id = "TA_PT_tools_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        layout.prop(scene, "ta_profile_id", text="Profile")
        profile = get_profile(scene.ta_profile_id)
        description = str(profile.get("description", ""))
        if description:
            layout.label(text=description[:70], icon="INFO")

        project_box = layout.box()
        project_box.prop(scene, "ta_profile_file", text="Project profiles")
        project_box.operator("ta.load_project_profiles", icon="FILE_REFRESH")
        project_box.prop(scene, "ta_rule_file", text="Project rules")
        project_box.operator("ta.load_project_rules", icon="SCRIPT")

        row = layout.row(align=True)
        row.operator("ta.asset_doctor_analyze", icon="VIEWZOOM")
        row.operator("ta.asset_doctor_fix_safe", icon="TOOL_SETTINGS")

        if scene.ta_asset_doctor_summary:
            layout.label(text=scene.ta_asset_doctor_summary, icon="CHECKMARK")

        layout.prop(scene, "ta_show_split_details", text="Render split details")
        layout.prop(scene, "ta_enforce_project_budget", text="Enforce project budget")
        layout.prop(scene, "ta_enforce_td_target", text="Enforce texel-density target")

        split_box = layout.box()
        split_box.label(text="Select render-split boundaries", icon="EDGESEL")
        row = split_box.row(align=True)
        op = row.operator("ta.asset_doctor_select_split_cause", text="Normals")
        op.cause = "NORMAL"
        op = row.operator("ta.asset_doctor_select_split_cause", text="UVs")
        op.cause = "UV"
        op = row.operator("ta.asset_doctor_select_split_cause", text="Materials")
        op.cause = "MATERIAL"

        for report_index, report in enumerate(TA_LAST_REPORTS[:8]):
            box = layout.box()
            icon = "CANCEL" if report.status == "ERROR" else ("ERROR" if report.status == "WARNING" else "CHECKMARK")
            box.label(text="{0} · {1}".format(report.object_name, report.summary()), icon=icon)

            metrics = report.metrics
            box.label(text=(
                "{0:,} tris · {1:,} model verts · {2:,} render verts · {3} mats · {4} UVs".format(
                    int(metrics.get("triangles", 0)),
                    int(metrics.get("model_vertices", 0)),
                    int(metrics.get("render_vertices", 0)),
                    int(metrics.get("material_slots", 0)),
                    int(metrics.get("uv_channels", 0)),
                )
            ))

            td_value = float(metrics.get("texel_density_px_per_cm", 0.0))
            memory_bytes = int(metrics.get("mesh_buffer_bytes", 0))
            if td_value > 0.0 or memory_bytes > 0:
                box.label(text=(
                    "TD {0:.2f} px/cm · spread {1:.1f}% · mesh buffers ~{2}".format(
                        td_value,
                        float(metrics.get("texel_density_spread_percent", 0.0)),
                        format_bytes(memory_bytes),
                    )
                ))

            if int(metrics.get("uv_island_count", 0)):
                box.label(text=(
                    "UV islands {0} · util ~{1:.1f}% · overlap faces {2} · min pad ~{3:.1f}px".format(
                        int(metrics.get("uv_island_count", 0)),
                        float(metrics.get("uv_utilization_percent", 0.0)),
                        int(metrics.get("uv_overlap_faces", 0)),
                        float(metrics.get("uv_min_padding_px", 0.0)),
                    )
                ))

            texture_count = int(metrics.get("texture_count", 0))
            texture_bytes = int(metrics.get("texture_reference_bytes", 0))
            if texture_count or int(metrics.get("material_count", 0)):
                box.label(text=(
                    "Materials {0} · textures {1} · missing {2} · tex ref ~{3}".format(
                        int(metrics.get("material_count", 0)),
                        texture_count,
                        int(metrics.get("missing_texture_count", 0)),
                        format_bytes(texture_bytes),
                    )
                ))

            if metrics.get("has_skinning"):
                box.label(text=(
                    "Skin: {0} deform bones · max {1} influences · {2} unweighted".format(
                        int(metrics.get("deform_bones", 0)),
                        int(metrics.get("max_skin_influences", 0)),
                        int(metrics.get("unweighted_vertices", 0)),
                    )
                ))

            if scene.ta_show_split_details:
                base = int(metrics.get("model_vertices", 0))
                render = int(metrics.get("render_vertices", 0))
                ratio = float(metrics.get("render_vertex_ratio", 0.0))
                box.label(text="Render splits: {0:,} → {1:,} ({2:.2f}x)".format(base, render, ratio))
                for stage in metrics.get("render_split_stages", []):
                    added = int(stage.get("added", 0))
                    if added:
                        share = float(stage.get("share_of_extra", 0.0)) * 100.0
                        box.label(text="  {0}: +{1:,} ({2:.0f}% of extra)".format(
                            stage.get("label", "Split"), added, share
                        ))

            issues = report.sorted_issues()
            for issue_index, issue in enumerate(issues[:8]):
                row = box.row(align=True)
                row.alert = issue.severity == "ERROR"
                issue_icon = "CANCEL" if issue.severity == "ERROR" else ("ERROR" if issue.severity == "WARNING" else "INFO")
                op = row.operator(
                    "ta.asset_doctor_select_issue",
                    text="{0}: {1}".format(issue.category, issue.title),
                    icon=issue_icon,
                )
                op.report_index = report_index
                op.issue_index = issue_index
                if issue.is_safe_fixable:
                    row.label(text="Safe fix", icon="CHECKMARK")

            if len(issues) > 8:
                box.label(text="+ {0} more issue(s) in JSON report".format(len(issues) - 8), icon="INFO")

        layout.separator()
        layout.prop(scene, "ta_asset_report_path", text="Report")
        layout.operator("ta.asset_doctor_export_report", icon="TEXT")

class TA_PT_authoring(bpy.types.Panel):
    bl_label = "Modular / Attachments"
    bl_idname = "TA_PT_authoring"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TA Tools"
    bl_parent_id = "TA_PT_tools_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        grid = layout.box()
        grid.label(text="Physical Modular Grid", icon="GRID")
        row = grid.row(align=True)
        row.prop(scene, "ta_modular_grid_cm")
        row.prop(scene, "ta_modular_tolerance_cm")
        grid.prop(scene, "ta_modular_check_position")
        row = grid.row(align=True)
        row.prop(scene, "ta_modular_snap_x", text="X", toggle=True)
        row.prop(scene, "ta_modular_snap_y", text="Y", toggle=True)
        row.prop(scene, "ta_modular_snap_z", text="Z", toggle=True)
        row.prop(scene, "ta_modular_snap_mode", text="")
        row = grid.row(align=True)
        row.operator("ta.modular_analyze", text="Check Bounds")
        row.operator("ta.modular_snap", text="Snap Origins")
        if scene.ta_modular_result:
            grid.label(text=scene.ta_modular_result, icon="INFO")

        align = layout.box()
        align.label(text="Bounds Alignment", icon="ORIENTATION_GLOBAL")
        align.prop(scene, "ta_modular_align_anchor", text="Anchor")
        align.operator("ta.modular_align_anchor", text="Align Selected to Active")
        row = align.row(align=True)
        row.prop(scene, "ta_modular_match_x", text="X", toggle=True)
        row.prop(scene, "ta_modular_match_y", text="Y", toggle=True)
        row.prop(scene, "ta_modular_match_z", text="Z", toggle=True)
        align.operator("ta.modular_match_bounds", text="Match Bounds Size to Active")

        attachment = layout.box()
        attachment.label(text="Socket / Helper", icon="EMPTY_AXIS")
        attachment.prop(scene, "ta_attachment_role", text="Role")
        attachment.prop(scene, "ta_attachment_label", text="Label")
        attachment.prop(scene, "ta_attachment_location", text="Place at")
        row = attachment.row(align=True)
        row.prop(scene, "ta_attachment_size_cm")
        row.prop(scene, "ta_attachment_parent", text="Parent")
        attachment.operator("ta.create_attachment", text="Create Attachment")

        normalize = layout.box()
        normalize.label(text="Batch Transform Normalization", icon="OBJECT_ORIGIN")
        row = normalize.row(align=True)
        row.prop(scene, "ta_normalize_scale")
        row.prop(scene, "ta_normalize_rotation")
        normalize.prop(scene, "ta_normalize_allow_negative")
        normalize.operator("ta.normalize_transforms", text="Apply Selected Channels")


class TA_PT_asset_sets(bpy.types.Panel):
    bl_label = "Asset Sets / LOD / Collision"
    bl_idname = "TA_PT_asset_sets"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TA Tools"
    bl_parent_id = "TA_PT_tools_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        layout.prop(scene, "ta_asset_set_root", text="Root")
        row = layout.row(align=True)
        row.operator("ta.asset_set_create", text="Create / Update", icon="OUTLINER_COLLECTION")
        row.operator("ta.asset_set_select", text="Select Set", icon="RESTRICT_SELECT_OFF")
        row.operator("ta.asset_set_analyze", text="Analyze", icon="VIEWZOOM")

        if scene.ta_asset_set_summary:
            layout.label(text=scene.ta_asset_set_summary, icon="INFO")

        assign = layout.box()
        assign.label(text="Member Assignment", icon="OBJECT_DATA")
        assign.prop(scene, "ta_asset_member_role", text="Role")
        if scene.ta_asset_member_role in {ROLE_RENDER, ROLE_LOD}:
            assign.prop(scene, "ta_asset_member_lod", text="LOD level")
        if scene.ta_asset_member_role == ROLE_COLLISION:
            assign.prop(scene, "ta_asset_collision_type", text="Collision")
        assign.prop(scene, "ta_asset_member_export", text="Include in export")
        assign.operator("ta.asset_set_assign_role", text="Assign Selected")

        lod_box = layout.box()
        lod_box.label(text="LOD", icon="MOD_DECIM")
        row = lod_box.row(align=True)
        row.prop(scene, "ta_lod_generate_level", text="Level")
        row.prop(scene, "ta_lod_generate_ratio", text="Ratio")
        lod_box.operator("ta.asset_set_generate_lod", text="Duplicate Selected as LOD")
        lod_box.prop(scene, "ta_lod_min_reduction", text="Min reduction %")
        lod_box.prop(scene, "ta_require_contiguous_lods", text="Require contiguous chain")

        collision_box = layout.box()
        collision_box.label(text="Collision", icon="MESH_CUBE")
        row = collision_box.row(align=True)
        row.operator("ta.asset_set_create_box_collision", text="UBX Box")
        row.operator("ta.asset_set_create_sphere_collision", text="USP Sphere")
        collision_box.prop(scene, "ta_enforce_collision_budget", text="Enforce collision tri budget")
        if scene.ta_enforce_collision_budget:
            collision_box.prop(scene, "ta_collision_triangle_budget", text="Collision tris")

        analysis = TA_LAST_ASSET_SET
        if analysis and analysis.get("root_name") == scene.ta_asset_set_root:
            metrics = analysis.get("metrics", {})
            box = layout.box()
            box.label(text="Set Report · {0}".format(analysis.get("status", "CLEAN")), icon=("CANCEL" if analysis.get("status") == "ERROR" else "CHECKMARK"))
            for lod in metrics.get("lod_metrics", []):
                reduction = lod.get("reduction_from_previous_percent")
                suffix = "" if reduction is None else " · -{0:.1f}%".format(float(reduction))
                box.label(text="LOD{0}: {1:,} tris · {2} member(s){3}".format(
                    int(lod.get("level", 0)),
                    int(lod.get("triangles", 0)),
                    int(lod.get("member_count", 0)),
                    suffix,
                ))
            box.label(text="Collision: {0} member(s) · {1:,} tris · Sockets: {2}".format(
                int(metrics.get("collision_count", 0)),
                int(metrics.get("collision_triangles", 0)),
                int(metrics.get("socket_count", 0)),
            ))
            for issue in analysis.get("issues", [])[:8]:
                row = box.row()
                row.alert = issue.severity == "ERROR"
                icon = "CANCEL" if issue.severity == "ERROR" else "ERROR"
                row.label(text="{0}: {1}".format(issue.category, issue.title), icon=icon)

            members = analysis.get("members", [])
            member_box = layout.box()
            member_box.label(text="Members ({0})".format(len(members)), icon="OUTLINER_OB_GROUP_INSTANCE")
            for member in members[:12]:
                role = str(member.get("role", ""))
                lod = int(member.get("lod_level", 0))
                role_label = role + ("{0}".format(lod) if role in {ROLE_RENDER, ROLE_LOD} else "")
                enabled = "" if member.get("export_enabled", True) else " · disabled"
                member_box.label(text="{0} · {1}{2}".format(member.get("name", ""), role_label, enabled))
            if len(members) > 12:
                member_box.label(text="+ {0} more".format(len(members) - 12))

        export_box = layout.box()
        export_box.label(text="Orchestrated Export", icon="EXPORT")
        export_box.prop(scene, "ta_asset_set_move_to_origin", text="Move set to LOD0 origin for export")
        row = export_box.row(align=True)
        op = row.operator("ta.preview_export", text="Preview")
        op.mode = "ASSET_SET"
        row.operator("ta.asset_set_export", text="Export Asset Set", icon="EXPORT")


# ---
# Rigging helpers
# ---

def ta_signed_angle(vector_u, vector_v, normal):
    """
    Angle between two vectors, signed by the given normal.
    """
    angle = vector_u.angle(vector_v)

    if vector_u.cross(vector_v).dot(normal) < 0.0:
        angle = -angle

    return angle


def ta_get_pole_angle(base_bone, ik_bone, pole_location):
    """
    Computes the IK constraint pole_angle so the chain does not snap
    when the pole target is assigned (all inputs in armature space).
    """
    pole_normal = (ik_bone.tail - base_bone.head).cross(
        pole_location - base_bone.head
    )
    projected_pole_axis = pole_normal.cross(base_bone.tail - base_bone.head)

    return ta_signed_angle(
        base_bone.x_axis,
        projected_pole_axis,
        base_bone.tail - base_bone.head,
    )


def ta_create_empty(name, world_location, collection, display_size=0.2):
    empty = bpy.data.objects.new(name, None)
    empty.empty_display_type = "PLAIN_AXES"
    empty.empty_display_size = display_size

    collection.objects.link(empty)

    empty.matrix_world.translation = world_location

    return empty


class TA_OT_rename_selected(bpy.types.Operator):
    bl_idname = "ta.rename_selected"
    bl_label = "Rename Selected"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        prefix = context.scene.rs_prefix
        item_name = context.scene.rs_name

        for index, obj in enumerate(context.selected_objects, start=1):
            obj.name = f"{prefix}_{item_name}_{index:02d}"

        self.report({"INFO"}, "Renamed selected objects")
        return {"FINISHED"}


def ta_export_selected_file(filepath, export_profile_id=None):
    """Export the current Blender selection using the preset's real format."""
    profile_id = export_profile_id or getattr(bpy.context.scene, "ta_export_profile", "GENERIC_FBX")
    profile = get_export_profile(profile_id)
    file_format = str(profile.get("format", "FBX")).upper()

    heatmapped = [
        obj for obj in bpy.context.selected_objects
        if obj.type == "MESH" and ta_has_td_heatmap(obj)
    ]
    for obj in heatmapped:
        ta_clear_td_heatmap(obj)

    try:
        if file_format == "FBX":
            object_types = {"MESH"}
            if profile.get("include_helpers", True):
                object_types.add("EMPTY")
            if profile.get("include_armatures", True):
                object_types.add("ARMATURE")
            bpy.ops.export_scene.fbx(
                filepath=filepath,
                use_selection=True,
                apply_unit_scale=True,
                bake_space_transform=False,
                object_types=object_types,
                use_mesh_modifiers=True,
                add_leaf_bones=False,
                bake_anim=bool(profile.get("include_animation", False)),
                path_mode="COPY" if profile.get("embed_textures", False) else "AUTO",
                embed_textures=bool(profile.get("embed_textures", False)),
            )
        elif file_format == "OBJ":
            if not hasattr(bpy.ops.wm, "obj_export"):
                raise RuntimeError("This Blender build does not expose the OBJ exporter")
            bpy.ops.wm.obj_export(
                filepath=filepath,
                export_selected_objects=True,
                apply_modifiers=True,
                export_materials=True,
            )
        elif file_format in {"GLB", "GLTF"}:
            if not hasattr(bpy.ops.export_scene, "gltf"):
                raise RuntimeError("glTF exporter is unavailable in this Blender build")
            bpy.ops.export_scene.gltf(
                filepath=filepath,
                use_selection=True,
                export_format="GLB" if file_format == "GLB" else "GLTF_SEPARATE",
                export_animations=bool(profile.get("include_animation", False)),
            )
        elif file_format == "USD":
            if not hasattr(bpy.ops.wm, "usd_export"):
                raise RuntimeError("USD exporter is unavailable in this Blender build")
            # Keep this conservative. USD exposes many options and mapping them
            # all map cleanly to FBX presets would be worse than exposing fewer.
            bpy.ops.wm.usd_export(
                filepath=filepath,
                selected_objects_only=True,
                export_animation=bool(profile.get("include_animation", False)),
            )
        else:
            raise RuntimeError("Unsupported export format: {0}".format(file_format))
    finally:
        for obj in heatmapped:
            ta_apply_td_heatmap(bpy.context, obj)


class TA_OT_preview_export(bpy.types.Operator):
    bl_idname = "ta.preview_export"
    bl_label = "Preview Export"
    bl_description = "Show the exact target file and whether it would replace an existing export"

    mode: bpy.props.EnumProperty(
        items=[("SELECTION", "Selection", "Preview the normal selection export"), ("ASSET_SET", "Asset Set", "Preview the active Asset Set export")],
        default="SELECTION",
    )

    def execute(self, context):
        scene = context.scene
        export_path = bpy.path.abspath(scene.ee_export_path)
        if self.mode == "ASSET_SET":
            root = scene.ta_asset_set_root.strip() or ta_asset_set_for_active(context)
            if not root:
                self.report({"WARNING"}, "No active Asset Set")
                return {"CANCELLED"}
            members = ta_get_asset_set_members(context, root, include_disabled=False)
            base_name = root
            member_names = [obj.name for obj in members]
        else:
            active = context.active_object
            if active is None:
                self.report({"WARNING"}, "No active object")
                return {"CANCELLED"}
            base_name = get_object_export_name(context, active) if scene.ee_auto_poly_prefix else active.name
            member_names = [obj.name for obj in context.selected_objects]

        plan = build_export_plan(
            export_path,
            base_name,
            scene.ta_export_profile,
            member_names,
            write_sidecar=scene.ee_write_sidecar,
        )
        scene.ta_export_preview = format_export_plan(plan)
        if plan.get("has_conflict") and not scene.ee_overwrite_existing:
            self.report({"WARNING"}, "Target already exists; enable Overwrite Existing to replace it")
        else:
            self.report({"INFO"}, scene.ta_export_preview)
        return {"FINISHED"}


class TA_OT_export_selected(bpy.types.Operator):
    bl_idname = "ta.export_selected"
    bl_label = "Export Selected"
    bl_description = (
        "Export the selection with the active preset; optionally validate first, "
        "batch one file per object and move objects to the origin"
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        scene = context.scene

        export_path = bpy.path.abspath(scene.ee_export_path)

        if not os.path.isdir(export_path):
            self.report({"WARNING"}, "Invalid export folder")
            return {"CANCELLED"}

        selected_meshes = get_selected_meshes(context)

        if not selected_meshes:
            self.report({"WARNING"}, "No mesh objects selected")
            return {"CANCELLED"}

        if scene.ee_validate_export:
            reports = [
                ta_asset_doctor_analyze_object(context, obj)
                for obj in selected_meshes
            ]
            blocking = []
            warnings = []

            for report in reports:
                for issue in report.sorted_issues():
                    if issue.category == "UV" and not scene.ta_uv_checks:
                        continue
                    line = "{0}: {1}".format(report.object_name, issue.title)
                    if issue.severity == "ERROR":
                        blocking.append(line)
                    elif issue.severity == "WARNING":
                        warnings.append(line)

            summary_lines = blocking + warnings
            scene.ta_validation_message = (
                " | ".join(summary_lines)
                if summary_lines
                else "Clean: profile validation passed"
            )

            if blocking and scene.ee_block_export:
                self.report(
                    {"ERROR"},
                    "Export blocked by Asset Doctor: " + " | ".join(blocking),
                )
                return {"CANCELLED"}

            if blocking or warnings:
                self.report(
                    {"WARNING"},
                    "Asset Doctor found export warnings; exporting anyway",
                )

        if scene.ee_batch_export:
            return self.execute_batch(context, export_path, selected_meshes)

        return self.execute_single(context, export_path)

    def execute_single(self, context, export_path):
        scene = context.scene

        if not context.active_object:
            self.report({"WARNING"}, "No active object")
            return {"CANCELLED"}

        base_name = context.active_object.name

        if scene.ee_auto_poly_prefix:
            export_name, count, classification = get_export_name_with_prefix(
                context,
                base_name
            )
        else:
            export_name = base_name
            count = get_selection_metric(context)
            classification = None

        plan = build_export_plan(
            export_path, export_name, scene.ta_export_profile,
            [obj.name for obj in get_selected_meshes(context)], scene.ee_write_sidecar,
        )
        extension = plan["extension"]
        filepath = plan["filepath"]
        if plan.get("has_conflict") and not scene.ee_overwrite_existing:
            self.report({"ERROR"}, "Export or sidecar target exists. Enable Overwrite Existing or change the name/path.")
            return {"CANCELLED"}
        self.export_selection(filepath)

        if scene.ee_write_sidecar:
            reports = [
                ta_asset_doctor_analyze_object(context, obj)
                for obj in get_selected_meshes(context)
            ]
            write_export_sidecar(
                filepath,
                reports,
                source_host="Blender",
                profile_id=scene.ta_profile_id,
                suite_version="2.4.0",
                export_profile_id=scene.ta_export_profile,
            )

        if classification:
            self.report(
                {"INFO"},
                f"Exported {export_name}{extension} as {classification['prefix']} "
                f"with {count:,} elements"
            )
        else:
            self.report({"INFO"}, f"Exported {export_name}{extension}")

        return {"FINISHED"}

    def execute_batch(self, context, export_path, selected_meshes):
        """
        Exports one file per object, optionally moving each object to the
        world origin for the export and restoring it afterwards (the
        standard game-art batch export workflow).
        """
        scene = context.scene

        if not scene.ee_overwrite_existing:
            conflicts = []
            for obj in selected_meshes:
                plan = build_export_plan(
                    export_path, get_object_export_name(context, obj), scene.ta_export_profile,
                    [obj.name], scene.ee_write_sidecar,
                )
                if plan.get("has_conflict"):
                    conflicts.append(plan["filepath"])
            if conflicts:
                self.report({"ERROR"}, "Batch export blocked: {0} export/sidecar target(s) already exist".format(len(conflicts)))
                return {"CANCELLED"}

        original_active = context.view_layer.objects.active
        original_selection = list(context.selected_objects)

        exported = 0

        try:
            for obj in selected_meshes:
                for other in context.selected_objects:
                    other.select_set(False)

                obj.select_set(True)
                context.view_layer.objects.active = obj

                export_name = get_object_export_name(context, obj)
                extension = export_extension(scene.ta_export_profile)
                filepath = os.path.join(export_path, export_name + extension)
                asset_report = (
                    ta_asset_doctor_analyze_object(context, obj)
                    if scene.ee_write_sidecar
                    else None
                )

                if scene.ee_move_to_origin:
                    original_matrix = obj.matrix_world.copy()
                    obj.matrix_world.translation = (0.0, 0.0, 0.0)

                    try:
                        self.export_selection(filepath)
                    finally:
                        obj.matrix_world = original_matrix
                else:
                    self.export_selection(filepath)

                if asset_report is not None:
                    write_export_sidecar(
                        filepath,
                        [asset_report],
                        source_host="Blender",
                        profile_id=scene.ta_profile_id,
                        suite_version="2.4.0",
                        export_profile_id=scene.ta_export_profile,
                    )

                exported += 1

        finally:
            for other in context.selected_objects:
                other.select_set(False)

            for obj in original_selection:
                obj.select_set(True)

            context.view_layer.objects.active = original_active

        self.report(
            {"INFO"},
            f"Batch exported {exported} file(s) to {export_path}"
        )

        return {"FINISHED"}

    def export_selection(self, filepath):
        ta_export_selected_file(filepath, bpy.context.scene.ta_export_profile)


class TA_PT_tools_panel(bpy.types.Panel):
    bl_label = "TA Tools"
    bl_idname = "TA_PT_tools_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TA Tools"

    def draw(self, context):
        layout = self.layout

        row = layout.row(align=True)
        row.operator("ta.save_defaults", text="Save Defaults", icon="EXPORT")
        row.operator("ta.apply_defaults", text="Apply Defaults", icon="IMPORT")


class TA_PT_rename_tool_panel(bpy.types.Panel):
    bl_label = "Rename Objects"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TA Tools"
    bl_parent_id = "TA_PT_tools_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        layout.prop(scene, "rs_prefix")
        layout.prop(scene, "rs_name")
        layout.operator("ta.rename_selected")


class TA_OT_unify(bpy.types.Operator):
    bl_idname = "ta.unify"
    bl_label = "Unify"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        active = context.active_object

        return active is not None and active.type == "MESH"

    def execute(self, context):
        if not context.scene.ee_unify:
            self.report({"INFO"}, "Unify is disabled")
            return {"CANCELLED"}

        if context.active_object and context.active_object.type == "MESH":
            bpy.ops.object.join()
            self.report({"INFO"}, "Objects unified")
            return {"FINISHED"}

        self.report({"INFO"}, "No active mesh object")
        return {"CANCELLED"}


class TA_OT_move_origin(bpy.types.Operator):
    bl_idname = "ta.move_origin"
    bl_label = "Move Origin"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        active = context.active_object

        return active is not None and active.type == "MESH"

    def execute(self, context):
        selected_meshes = get_selected_meshes(context)
        if not selected_meshes:
            self.report({"WARNING"}, "No selected mesh objects")
            return {"CANCELLED"}

        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        context.view_layer.objects.active = selected_meshes[0]

        scene = context.scene

        axis = [
            (scene.x_axis, 0),
            (scene.y_axis, 1),
            (scene.z_axis, 2),
        ]

        moved_count = 0

        if scene.individual_toggle:
            for obj in selected_meshes:
                target_co = obj.matrix_world.translation.copy()

                for axis_choice, axis_index in axis:
                    if axis_choice == "NA":
                        continue

                    extreme_vertex = get_extreme_vertex(obj, axis_choice)
                    target_co[axis_index] = extreme_vertex[axis_index]

                offset_world = target_co - obj.matrix_world.translation
                offset_local = obj.matrix_world.inverted().to_3x3() @ offset_world

                for vert in obj.data.vertices:
                    vert.co -= offset_local

                if obj.matrix_world.translation != target_co:
                    obj.matrix_world.translation = target_co
                    obj.data.update()
                    moved_count += 1

            self.report(
                {"INFO"},
                f"Moved origins individually on {moved_count} object(s)"
            )

        else:
            shared_values = {}

            for axis_choice, axis_index in axis:
                if axis_choice == "NA":
                    continue

                extreme_vertex = get_selection_extreme_vertex(
                    selected_meshes,
                    axis_choice
                )

                if extreme_vertex is not None:
                    shared_values[axis_index] = extreme_vertex[axis_index]

            for obj in selected_meshes:
                target_co = obj.matrix_world.translation.copy()

                for axis_index, value in shared_values.items():
                    target_co[axis_index] = value

                offset_world = target_co - obj.matrix_world.translation
                offset_local = obj.matrix_world.inverted().to_3x3() @ offset_world

                for vert in obj.data.vertices:
                    vert.co -= offset_local

                if obj.matrix_world.translation != target_co:
                    obj.matrix_world.translation = target_co
                    obj.data.update()
                    moved_count += 1

            self.report(
                {"INFO"},
                f"Moved origins to shared point on {moved_count} object(s)"
            )

        return {"FINISHED"}


class TA_OT_check_non_manifold(bpy.types.Operator):
    bl_idname = "ta.check_non_manifold"
    bl_label = "Check Non-Manifold"
    bl_description = "Checks selected geometry for non-manifolds"
    bl_options = {"REGISTER", "UNDO"}

    select_first_issue: bpy.props.BoolProperty(
        name="Select First Issue",
        description="Select first problematic object and highlight its non-manifold",
        default=True
    )

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        selected_meshes = get_selected_meshes(context)

        if not selected_meshes:
            self.report({"WARNING"}, "No mesh objects selected")
            return {"CANCELLED"}

        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        problem_objects = []
        total_bad_edges = 0

        for obj in selected_meshes:
            result = get_non_manifold_edges(obj)

            if result is None:
                continue

            bad_count = len(result["bad_edges"])

            if bad_count == 0:
                continue

            total_bad_edges += bad_count

            problem_objects.append({
                "object": obj,
                "result": result,
                "bad_count": bad_count,
            })

        if not problem_objects:
            context.scene.ta_validation_message = "Clean: no non-manifolds"
            self.report({"INFO"}, "No non-manifold geometry found")
            return {"FINISHED"}

        report_lines = []

        for item in problem_objects:
            obj = item["object"]
            result = item["result"]

            wire_count = len(result["wire_edges"])
            boundary_count = len(result["boundary_edges"])
            multiface_count = len(result["multiface_edges"])
            non_contiguous_count = len(result["non_contiguous_edges"])

            line = (
                f"{obj.name}: "
                f"{item['bad_count']} bad edges "
                f"(wire: {wire_count}, "
                f"boundary: {boundary_count}, "
                f"multiface: {multiface_count}, "
                f"other: {non_contiguous_count})"
            )

            report_lines.append(line)

        print("TA Tools - Non-Manifold Report")
        print("------------------------------")
        for line in report_lines:
            print(line)

        context.scene.ta_validation_message = " | ".join(report_lines)

        if self.select_first_issue:
            first_problem = problem_objects[0]
            obj = first_problem["object"]
            bad_edges = first_problem["result"]["bad_edges"]

            for selected_obj in context.selected_objects:
                selected_obj.select_set(False)

            obj.select_set(True)
            context.view_layer.objects.active = obj

            mesh = obj.data

            for vert in mesh.vertices:
                vert.select = False

            for edge in mesh.edges:
                edge.select = False

            for poly in mesh.polygons:
                poly.select = False

            for edge_index in bad_edges:
                if edge_index < len(mesh.edges):
                    mesh.edges[edge_index].select = True

            mesh.update()
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_mode(type="EDGE")

        self.report(
            {"WARNING"},
            f"Found {total_bad_edges} non-manifold edges "
            f"in {len(problem_objects)} object(s)"
        )

        return {"FINISHED"}


class TA_OT_calculate_budget(bpy.types.Operator):
    bl_label = "Calculate Budget"
    bl_idname = "ta.calculate_budget"
    bl_description = "Calculates the budget based on the user's settings."
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        scene = context.scene
        selected_meshes = get_selected_meshes(context)

        if not selected_meshes:
            self.report({"WARNING"}, "No mesh objects selected")
            return {"CANCELLED"}

        count = get_selection_metric(context)
        classification = get_poly_classification(count, scene)

        scene.ee_budget_cached_valid = True
        scene.ee_budget_cached_count = count
        scene.ee_budget_cached_mode = scene.ee_budget_count_mode
        scene.ee_budget_cached_signature = get_budget_signature(context)
        scene.ee_budget_cached_state = classification["state"]

        self.report(
            {"INFO"},
            f"Budget calculated: {count:,} {get_budget_count_label(scene)}"
        )

        return {"FINISHED"}


class TA_OT_planar_project_selected_uv(bpy.types.Operator):
    bl_idname = "ta.planar_project_selected_uv"
    bl_label = "Planar Project Selected UVs"
    bl_description = "Planar-project UVs for selected edit-mode faces"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode == "EDIT_MESH" and bool(ta_get_edit_mesh_objects(context))

    def execute(self, context):
        scene = context.scene
        edit_mesh_objects = ta_get_edit_mesh_objects(context)

        if not edit_mesh_objects:
            self.report({"WARNING"}, "Enter Edit Mode and select mesh faces")
            return {"CANCELLED"}

        total_faces = 0
        total_islands = 0

        for obj in edit_mesh_objects:
            mesh = obj.data
            bm = bmesh.from_edit_mesh(mesh)

            bm.faces.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.verts.ensure_lookup_table()

            selected_faces = [
                face for face in bm.faces
                if face.select and not face.hide
            ]

            if not selected_faces:
                continue

            uv_layer = ta_get_bmesh_uv_layer(
                bm,
                scene.ta_uv_map_name
            )

            if scene.ta_uv_fit_mode == "ISLANDS":
                face_groups = ta_get_selected_face_islands(selected_faces)
            else:
                face_groups = [selected_faces]

            for faces in face_groups:
                projected_count = ta_project_faces_to_uv(
                    obj=obj,
                    faces=faces,
                    uv_layer=uv_layer,
                    projection_mode=scene.ta_uv_projection_mode,
                    space=scene.ta_uv_projection_space,
                    preserve_aspect=scene.ta_uv_preserve_aspect,
                    padding=scene.ta_uv_padding,
                    rotation=scene.ta_uv_rotation,
                    flip_u=scene.ta_uv_flip_u,
                    flip_v=scene.ta_uv_flip_v
                )

                if projected_count > 0:
                    total_faces += projected_count
                    total_islands += 1

            bmesh.update_edit_mesh(mesh)

            if scene.ta_uv_map_name.strip():
                uv_map = mesh.uv_layers.get(scene.ta_uv_map_name)

                if uv_map:
                    mesh.uv_layers.active = uv_map

        if total_faces == 0:
            self.report({"WARNING"}, "No selected faces found")
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            f"Planar UV projected {total_faces} face(s) in {total_islands} group(s)"
        )

        return {"FINISHED"}


class TA_OT_save_defaults(bpy.types.Operator):
    bl_idname = "ta.save_defaults"
    bl_label = "Save Defaults"
    bl_description = (
        "Save the current TA Tools settings as defaults for all "
        "future sessions and files"
    )

    def execute(self, context):
        try:
            config_path = ta_save_defaults(context.scene)
        except OSError as error:
            self.report({"ERROR"}, f"Could not save defaults: {error}")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Defaults saved to {config_path}")
        return {"FINISHED"}


class TA_OT_apply_defaults(bpy.types.Operator):
    bl_idname = "ta.apply_defaults"
    bl_label = "Apply Defaults"
    bl_description = "Apply previously saved TA Tools defaults to this scene"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            applied = ta_apply_defaults(context.scene)
        except (OSError, ValueError) as error:
            self.report({"ERROR"}, f"Could not apply defaults: {error}")
            return {"CANCELLED"}

        if applied == 0:
            self.report({"INFO"}, "No saved defaults found")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Applied {applied} saved setting(s)")
        return {"FINISHED"}


class TA_OT_check_uvs(bpy.types.Operator):
    bl_idname = "ta.check_uvs"
    bl_label = "Check UVs"
    bl_description = (
        "Check selected meshes for missing UVs, UVs outside 0-1, "
        "flipped faces and zero-area faces"
    )
    bl_options = {"REGISTER", "UNDO"}

    select_first_issue: bpy.props.BoolProperty(
        name="Select First Issue",
        description="Select the first problematic object and its bad faces",
        default=True
    )

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        selected_meshes = get_selected_meshes(context)

        if not selected_meshes:
            self.report({"WARNING"}, "No mesh objects selected")
            return {"CANCELLED"}

        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        problem_objects = []
        report_lines = []

        for obj in selected_meshes:
            uv_result = ta_check_object_uvs(obj)

            if ta_uv_issue_count(uv_result) == 0:
                continue

            flags = []

            if uv_result["missing_uvs"]:
                flags.append("missing UVs")

            if uv_result["uvs_out_of_range"]:
                flags.append("UVs outside 0-1")

            if uv_result["flipped_faces"]:
                flags.append(f"flipped: {len(uv_result['flipped_faces'])}")

            if uv_result["zero_faces"]:
                flags.append(f"zero-area: {len(uv_result['zero_faces'])}")

            problem_objects.append({"object": obj, "result": uv_result})
            report_lines.append(f"{obj.name}: " + ", ".join(flags))

        if not problem_objects:
            context.scene.ta_validation_message = "Clean: no UV issues"
            self.report({"INFO"}, "No UV issues found")
            return {"FINISHED"}

        context.scene.ta_validation_message = " | ".join(report_lines)

        if self.select_first_issue:
            first = problem_objects[0]
            obj = first["object"]
            bad_faces = (
                first["result"]["flipped_faces"]
                + first["result"]["zero_faces"]
            )

            for selected_obj in context.selected_objects:
                selected_obj.select_set(False)

            obj.select_set(True)
            context.view_layer.objects.active = obj

            mesh = obj.data

            for vert in mesh.vertices:
                vert.select = False

            for edge in mesh.edges:
                edge.select = False

            for poly in mesh.polygons:
                poly.select = False

            for face_index in bad_faces:
                if face_index < len(mesh.polygons):
                    mesh.polygons[face_index].select = True

            mesh.update()

            if bad_faces:
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_mode(type="FACE")

        self.report(
            {"WARNING"},
            f"UV issues in {len(problem_objects)} object(s); see panel"
        )

        return {"FINISHED"}


class TA_OT_td_heatmap(bpy.types.Operator):
    bl_idname = "ta.td_heatmap"
    bl_label = "TD Heatmap"
    bl_description = "Color selected meshes by texel density: blue low, green target, red high"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        colored = 0
        for obj in get_selected_meshes(context):
            colored += ta_apply_td_heatmap(context, obj)
        if not colored:
            self.report({"WARNING"}, "No valid UV faces to visualize")
            return {"CANCELLED"}
        self.report({"INFO"}, "Texel-density heatmap applied to {0} face(s)".format(colored))
        return {"FINISHED"}


class TA_OT_clear_td_heatmap(bpy.types.Operator):
    bl_idname = "ta.clear_td_heatmap"
    bl_label = "Clear TD Heatmap"
    bl_description = "Remove the temporary JAM texel-density color attribute"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        cleared = sum(1 for obj in get_selected_meshes(context) if ta_clear_td_heatmap(obj))
        self.report({"INFO"}, "Cleared heatmap from {0} object(s)".format(cleared))
        return {"FINISHED"}


class TA_OT_check_texel_density(bpy.types.Operator):
    bl_idname = "ta.check_texel_density"
    bl_label = "Check TD"
    bl_description = (
        "Compute the area-weighted texel density of the selection "
        "with scene units normalized to metres"
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        scene = context.scene

        density = ta_get_selection_texel_density(
            context,
            scene.ta_td_texture_size
        )

        if density is None:
            scene.ta_td_result = "TD: no UV or surface area on selection"
            self.report({"WARNING"}, scene.ta_td_result)
            return {"CANCELLED"}

        scene.ta_td_result = (
            f"TD: {density:.3f} {ta_density_unit_label(scene)} "
            f"at {scene.ta_td_texture_size}px textures"
        )

        self.report({"INFO"}, scene.ta_td_result)
        return {"FINISHED"}


class TA_OT_set_texel_density(bpy.types.Operator):
    bl_idname = "ta.set_texel_density"
    bl_label = "Set TD"
    bl_description = (
        "Scale each selected object's UVs (around their UV bounds "
        "center) to match the target texel density"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        scene = context.scene
        target = scene.ta_td_target

        if target <= 0.0:
            self.report({"ERROR"}, "Target texel density must be > 0")
            return {"CANCELLED"}

        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        adjusted = 0

        for obj in get_selected_meshes(context):
            current = ta_get_object_texel_density(
                obj,
                scene.ta_td_texture_size,
                scene=scene,
            )

            if not current or current <= 0.0:
                continue

            factor = target / current

            if abs(factor - 1.0) < 0.0001:
                continue

            if ta_set_object_texel_density(obj, factor):
                adjusted += 1

        self.report({"INFO"}, f"Adjusted texel density on {adjusted} object(s)")

        bpy.ops.ta.check_texel_density()

        return {"FINISHED"}


class TA_OT_create_ik_pole(bpy.types.Operator):
    bl_idname = "ta.create_ik_pole"
    bl_label = "Create IK + Pole Vector"
    bl_description = (
        "Add an IK constraint to the active pose bone with target and "
        "pole empties; the pole is placed on the chain's bend plane and "
        "pole_angle is computed so the chain does not snap"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == "POSE"
            and context.active_pose_bone is not None
        )

    def execute(self, context):
        scene = context.scene
        armature = context.active_object
        end_bone = context.active_pose_bone

        chain_count = max(2, scene.ta_ik_chain_count)

        # Walk up the parents to collect the chain (end bone included)
        chain = [end_bone]
        current = end_bone

        while len(chain) < chain_count and current.parent:
            current = current.parent
            chain.append(current)

        if len(chain) < 2:
            self.report({"ERROR"}, "The active bone needs at least one parent")
            return {"CANCELLED"}

        base_bone = chain[-1]

        # Joint positions root->tip in armature space, e.g.
        # [shoulder, elbow, wrist] for a two-bone arm
        positions = [bone.head.copy() for bone in reversed(chain)]
        positions.append(end_bone.tail.copy())

        start_pos = positions[0]
        end_pos = positions[-1]
        mid_pos = positions[len(positions) // 2]

        axis = end_pos - start_pos
        to_mid = mid_pos - start_pos

        if axis.length < 1e-6:
            self.report({"ERROR"}, "Chain start and end are at the same position")
            return {"CANCELLED"}

        axis_normal = axis.normalized()

        # Vector rejection: component of to_mid perpendicular to the axis
        projection = axis_normal * to_mid.dot(axis_normal)
        pole_direction = to_mid - projection

        chain_length = sum(
            (positions[i + 1] - positions[i]).length
            for i in range(len(positions) - 1)
        )

        if pole_direction.length < 1e-6:
            fallback = Vector((0.0, 0.0, 1.0))

            if abs(axis_normal.dot(fallback)) > 0.999:
                fallback = Vector((0.0, 1.0, 0.0))

            pole_direction = axis_normal.cross(fallback)

            self.report(
                {"WARNING"},
                "Chain is straight; pole direction is arbitrary"
            )

        pole_pos = (
            mid_pos
            + pole_direction.normalized()
            * (chain_length * 0.5 * scene.ta_pole_distance)
        )

        world_matrix = armature.matrix_world

        target_empty = ta_create_empty(
            f"IK_{end_bone.name}",
            world_matrix @ end_bone.tail,
            context.collection
        )

        pole_empty = ta_create_empty(
            f"PV_{end_bone.name}",
            world_matrix @ pole_pos,
            context.collection
        )

        constraint = end_bone.constraints.new("IK")
        constraint.target = target_empty
        constraint.pole_target = pole_empty
        constraint.chain_count = len(chain)
        constraint.pole_angle = ta_get_pole_angle(
            base_bone,
            end_bone,
            pole_pos
        )

        self.report(
            {"INFO"},
            f"IK on {end_bone.name} (chain {len(chain)}) with pole "
            f"angle {math.degrees(constraint.pole_angle):.1f}°"
        )

        return {"FINISHED"}


class TA_OT_create_muscle_helper(bpy.types.Operator):
    bl_idname = "ta.create_muscle_helper"
    bl_label = "Create Muscle Helper"
    bl_description = (
        "Create a deform bone on the parent of the active pose bone, "
        "driven to bulge as the active bone bends -- the classic bicep "
        "setup. Add the new bone to your vertex groups to see the effect"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == "POSE"
            and context.active_pose_bone is not None
            and context.active_pose_bone.parent is not None
        )

    def execute(self, context):
        scene = context.scene
        armature = context.active_object

        bend_name = context.active_pose_bone.name
        upper_name = context.active_pose_bone.parent.name

        max_angle = scene.ta_muscle_max_angle

        if max_angle <= 0.0:
            self.report({"ERROR"}, "Max angle must be greater than zero")
            return {"CANCELLED"}

        helper_name = f"MUSCLE_{upper_name}"

        # Build the helper bone in edit mode, halfway along the upper bone
        bpy.ops.object.mode_set(mode="EDIT")

        edit_bones = armature.data.edit_bones
        upper_edit = edit_bones[upper_name]

        helper_edit = edit_bones.new(helper_name)
        helper_name = helper_edit.name  # Blender may append .001

        direction = upper_edit.tail - upper_edit.head

        helper_edit.head = upper_edit.head + direction * 0.5
        helper_edit.tail = helper_edit.head + direction * 0.25
        helper_edit.parent = upper_edit
        helper_edit.use_deform = True

        bpy.ops.object.mode_set(mode="POSE")

        # Drive the helper's scale from the bend bone's local rotation:
        # scale goes 1.0 -> bulge as |angle| goes 0 -> max_angle
        max_radians = math.radians(max_angle)
        bulge = scene.ta_muscle_bulge

        expression = (
            f"1.0 + ({bulge:.4f} - 1.0) "
            f"* min(abs(rot) / {max_radians:.6f}, 1.0)"
        )

        data_path = f'pose.bones["{helper_name}"].scale'

        for axis_index in range(3):
            fcurve = armature.driver_add(data_path, axis_index)
            driver = fcurve.driver
            driver.type = "SCRIPTED"

            variable = driver.variables.new()
            variable.name = "rot"
            variable.type = "TRANSFORMS"

            target = variable.targets[0]
            target.id = armature
            target.bone_target = bend_name
            target.transform_type = f"ROT_{scene.ta_muscle_axis}"
            target.transform_space = "LOCAL_SPACE"

            if hasattr(target, "rotation_mode"):
                target.rotation_mode = "AUTO"

            driver.expression = expression

        self.report(
            {"INFO"},
            f"Created {helper_name}; add it to the mesh's vertex groups "
            f"and paint weights to see the bulge"
        )

        return {"FINISHED"}


class TA_PT_easy_export(bpy.types.Panel):
    bl_label = "Easy Export"
    bl_idname = "TA_PT_easy_export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TA Tools"
    bl_parent_id = "TA_PT_tools_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        row = layout.row()
        row.prop(scene, "ee_unify")
        row.operator("ta.unify")

        layout.separator()

        box = layout.box()
        box.label(text="Budget")

        row = box.row(align=True)
        row.prop(scene, "ee_budget_count_mode", text="")
        row.prop(scene, "ee_poly_budget")

        if scene.ee_budget_count_mode == "ENGINE":
            box.prop(scene, "ee_count_material_splits")

        box.prop(scene, "ee_poly_margin")
        box.prop(scene, "ee_count_modifiers")
        box.prop(scene, "ee_auto_poly_prefix")

        row = box.row(align=True)
        row.prop(scene, "ee_lp_prefix")
        row.prop(scene, "ee_hp_prefix")

        box.operator("ta.calculate_budget", icon="FILE_REFRESH")

        try:
            draw_budget_marker(box, context)
        except Exception as error:
            error_row = box.row()
            error_row.alert = True
            error_row.label(text=f"Budget marker error: {error}", icon="ERROR")

        layout.separator()

        layout.label(text="Select origin axis")
        row = layout.row()
        row.prop(scene, "x_axis", text="")
        row.prop(scene, "y_axis", text="")
        row.prop(scene, "z_axis", text="")

        layout.operator("ta.move_origin")
        layout.prop(scene, "individual_toggle")

        layout.separator()
        validation_box = layout.box()
        validation_box.label(text="Validation", icon="CHECKMARK")

        row = validation_box.row(align=True)
        row.operator("ta.check_non_manifold", text="Check Non-Manifold", icon="ERROR")
        row.operator("ta.check_uvs", text="Check UVs", icon="UV")

        validation_box.prop(scene, "ta_uv_checks")

        if scene.ta_validation_message:
            warning_row = validation_box.row()
            warning_row.alert = "Clean:" not in scene.ta_validation_message
            warning_row.label(text=scene.ta_validation_message, icon="INFO")

        layout.separator()

        uv_box = layout.box()
        uv_box.label(text="UV Planar Projection", icon="GROUP_UVS")

        uv_box.prop(scene, "ta_uv_map_name")

        row = uv_box.row(align=True)
        row.prop(scene, "ta_uv_projection_mode", text="Projection")
        row.prop(scene, "ta_uv_projection_space", text="Space")

        uv_box.prop(scene, "ta_uv_fit_mode")
        uv_box.prop(scene, "ta_uv_preserve_aspect")
        uv_box.prop(scene, "ta_uv_padding")

        row = uv_box.row(align=True)
        row.prop(scene, "ta_uv_rotation")
        row.prop(scene, "ta_uv_flip_u")
        row.prop(scene, "ta_uv_flip_v")

        uv_box.operator(
            "ta.planar_project_selected_uv",
            text="Project Selected Faces",
            icon="GROUP_UVS"
        )

        layout.separator()

        export_box = layout.box()
        export_box.label(text="Export", icon="EXPORT")

        export_box.prop(scene, "ta_export_profile", text="Preset")
        export_box.prop(scene, "ee_export_path")

        row = export_box.row(align=True)
        row.prop(scene, "ee_validate_export")
        row.prop(scene, "ee_block_export")

        row = export_box.row(align=True)
        row.prop(scene, "ee_batch_export")
        row.prop(scene, "ee_write_sidecar", text="Sidecar")
        export_box.prop(scene, "ee_overwrite_existing", text="Overwrite Existing")

        sub = row.row(align=True)
        sub.enabled = scene.ee_batch_export
        sub.prop(scene, "ee_move_to_origin")

        row = export_box.row(align=True)
        op = row.operator("ta.preview_export", text="Preview")
        op.mode = "SELECTION"
        row.operator("ta.export_selected", text="Export")
        if scene.ta_export_preview:
            export_box.label(text=scene.ta_export_preview, icon="INFO")


class TA_PT_texel_density(bpy.types.Panel):
    bl_label = "Texel Density"
    bl_idname = "TA_PT_texel_density"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TA Tools"
    bl_parent_id = "TA_PT_tools_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        layout.label(text="Unit-normalized texel density")

        layout.prop(scene, "ta_td_texture_size")
        layout.prop(scene, "ta_td_unit", text="Display")
        layout.prop(scene, "ta_td_target")
        layout.prop(scene, "ta_td_tolerance")

        row = layout.row(align=True)
        row.operator("ta.check_texel_density")
        row.operator("ta.set_texel_density")

        row = layout.row(align=True)
        row.operator("ta.td_heatmap", text="Heatmap")
        row.operator("ta.clear_td_heatmap", text="Clear")
        layout.label(text="Heatmap: blue low · green target · red high", icon="COLOR")

        if scene.ta_td_result:
            layout.label(text=scene.ta_td_result, icon="INFO")


class TA_PT_rigging(bpy.types.Panel):
    bl_label = "Rigging Helpers"
    bl_idname = "TA_PT_rigging"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TA Tools"
    bl_parent_id = "TA_PT_tools_panel"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        layout.label(text="IK: active pose bone = chain end")

        layout.prop(scene, "ta_ik_chain_count")
        layout.prop(scene, "ta_pole_distance")
        layout.operator("ta.create_ik_pole", icon="CON_KINEMATIC")

        layout.separator()

        layout.label(text="Muscle: active pose bone = bend joint")

        layout.prop(scene, "ta_muscle_axis")
        layout.prop(scene, "ta_muscle_max_angle")
        layout.prop(scene, "ta_muscle_bulge")
        layout.operator("ta.create_muscle_helper", icon="BONE_DATA")


def ta_profile_enum_items(_self, _context):
    return list(profile_items())


class TA_OT_load_project_profiles(bpy.types.Operator):
    bl_idname = "ta.load_project_profiles"
    bl_label = "Load Project Profiles"
    bl_description = "Load project-specific Asset Doctor profiles from JSON"
    bl_options = {"REGISTER"}

    def execute(self, context):
        path = bpy.path.abspath(context.scene.ta_profile_file)
        if not path or not os.path.isfile(path):
            self.report({"ERROR"}, "Profile JSON does not exist")
            return {"CANCELLED"}
        try:
            loaded = load_profiles_json(path)
        except Exception as exc:
            self.report({"ERROR"}, "Could not load profiles: {0}".format(exc))
            return {"CANCELLED"}
        if not loaded:
            self.report({"WARNING"}, "No profiles found in JSON")
            return {"CANCELLED"}
        context.scene.ta_profile_id = loaded[0]
        self.report({"INFO"}, "Loaded {0} project profile(s)".format(len(loaded)))
        return {"FINISHED"}


class TA_OT_load_project_rules(bpy.types.Operator):
    bl_idname = "ta.load_project_rules"
    bl_label = "Load Project Rules"
    bl_description = "Load a Python module that registers project-specific validation rules"
    bl_options = {"REGISTER"}

    def execute(self, context):
        path = bpy.path.abspath(context.scene.ta_rule_file)
        if not path or not os.path.isfile(path):
            self.report({"ERROR"}, "Project rule file does not exist")
            return {"CANCELLED"}
        try:
            load_rule_module(path)
        except Exception as exc:
            self.report({"ERROR"}, "Could not load project rules: {0}".format(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, "Loaded project validation rules")
        return {"FINISHED"}


def register():
    bpy.utils.register_class(TA_OT_rename_selected)
    bpy.utils.register_class(TA_OT_export_selected)
    bpy.utils.register_class(TA_OT_move_origin)
    bpy.utils.register_class(TA_OT_unify)
    bpy.utils.register_class(TA_OT_check_non_manifold)
    bpy.utils.register_class(TA_OT_calculate_budget)
    bpy.utils.register_class(TA_OT_planar_project_selected_uv)
    bpy.utils.register_class(TA_OT_preview_export)

    bpy.utils.register_class(TA_OT_save_defaults)
    bpy.utils.register_class(TA_OT_apply_defaults)
    bpy.utils.register_class(TA_OT_check_uvs)
    bpy.utils.register_class(TA_OT_check_texel_density)
    bpy.utils.register_class(TA_OT_td_heatmap)
    bpy.utils.register_class(TA_OT_clear_td_heatmap)
    bpy.utils.register_class(TA_OT_set_texel_density)
    bpy.utils.register_class(TA_OT_create_ik_pole)
    bpy.utils.register_class(TA_OT_create_muscle_helper)
    bpy.utils.register_class(TA_OT_create_attachment)
    bpy.utils.register_class(TA_OT_modular_analyze)
    bpy.utils.register_class(TA_OT_modular_snap)
    bpy.utils.register_class(TA_OT_modular_align_anchor)
    bpy.utils.register_class(TA_OT_modular_match_bounds)
    bpy.utils.register_class(TA_OT_normalize_transforms)
    bpy.utils.register_class(TA_OT_asset_set_create)
    bpy.utils.register_class(TA_OT_asset_set_assign_role)
    bpy.utils.register_class(TA_OT_asset_set_analyze)
    bpy.utils.register_class(TA_OT_asset_set_select)
    bpy.utils.register_class(TA_OT_asset_set_create_box_collision)
    bpy.utils.register_class(TA_OT_asset_set_create_sphere_collision)
    bpy.utils.register_class(TA_OT_asset_set_generate_lod)
    bpy.utils.register_class(TA_OT_asset_set_export)
    bpy.utils.register_class(TA_OT_asset_doctor_analyze)
    bpy.utils.register_class(TA_OT_asset_doctor_fix_safe)
    bpy.utils.register_class(TA_OT_asset_doctor_select_issue)
    bpy.utils.register_class(TA_OT_asset_doctor_select_split_cause)
    bpy.utils.register_class(TA_OT_asset_doctor_export_report)
    bpy.utils.register_class(TA_OT_load_project_profiles)
    bpy.utils.register_class(TA_OT_load_project_rules)

    bpy.utils.register_class(TA_PT_tools_panel)
    bpy.utils.register_class(TA_PT_asset_doctor)
    bpy.utils.register_class(TA_PT_authoring)
    bpy.utils.register_class(TA_PT_asset_sets)
    bpy.utils.register_class(TA_PT_rename_tool_panel)
    bpy.utils.register_class(TA_PT_easy_export)
    bpy.utils.register_class(TA_PT_texel_density)
    bpy.utils.register_class(TA_PT_rigging)

    if ta_invalidate_analysis_cache not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(ta_invalidate_analysis_cache)

    bpy.types.Scene.rs_prefix = bpy.props.StringProperty(
        name="Prefix",
        default="SM"
    )

    bpy.types.Scene.rs_name = bpy.props.StringProperty(
        name="Item name",
        default="Prop"
    )

    bpy.types.Scene.ee_unify = bpy.props.BoolProperty(
        name="Unify objects?",
        default=False
    )

    bpy.types.Scene.x_axis = bpy.props.EnumProperty(
        items=[("NA", "None", ""), ("X", "X", ""), ("-X", "-X", ""), ("MID", "Mid", "")],
        update=update_axis
    )

    bpy.types.Scene.y_axis = bpy.props.EnumProperty(
        items=[("NA", "None", ""), ("Y", "Y", ""), ("-Y", "-Y", ""), ("MID", "Mid", "")],
        update=update_axis
    )

    bpy.types.Scene.z_axis = bpy.props.EnumProperty(
        items=[("NA", "None", ""), ("Z", "Z", ""), ("-Z", "-Z", ""), ("MID", "Mid", "")],
        update=update_axis
    )

    bpy.types.Scene.individual_toggle = bpy.props.BoolProperty(
        name="Apply individual origin transform?",
        default=False
    )

    bpy.types.Scene.ee_poly_budget = bpy.props.IntProperty(
        name="Poly Budget",
        description="Poly count threshold for LP/HP classification",
        default=3000,
        min=1
    )

    bpy.types.Scene.ee_poly_margin = bpy.props.FloatProperty(
        name="Margin (%)",
        description="Allowed percentage over the budget",
        default=10.0,
        min=0.0,
        soft_max=100.0,
    )

    bpy.types.Scene.ee_budget_count_mode = bpy.props.EnumProperty(
        name="Count Mode",
        items=[
            ("TRIS", "Triangles", "Use triangle count"),
            ("FACES", "Faces", "Use polygon/face count"),
            ("VERTS", "Vertices", "Use vertices count"),
            ("ENGINE", "Engine verts", "Estimated in-game/render vertex count")
        ],
        default="ENGINE"
    )

    bpy.types.Scene.ee_count_material_splits = bpy.props.BoolProperty(
        name="Count Material Splits",
        description=(
            "Include material section changes in the engine vertex "
            "estimate (recommended: ON)"
        ),
        default=True
    )

    bpy.types.Scene.ee_count_modifiers = bpy.props.BoolProperty(
        name="Count Modifiers",
        description="Count the evaluated mesh with modifiers applied",
        default=True
    )

    bpy.types.Scene.ee_auto_poly_prefix = bpy.props.BoolProperty(
        name="Budget-Based HP/LP Prefix",
        description=(
            "Infer HP/LP naming from the numeric budget. Disabled by default because "
            "asset role and budget compliance are separate concepts"
        ),
        default=False
    )

    bpy.types.Scene.ee_lp_prefix = bpy.props.StringProperty(
        name="LP Prefix",
        default="LP"
    )

    bpy.types.Scene.ee_hp_prefix = bpy.props.StringProperty(
        name="HP Prefix",
        default="HP"
    )

    bpy.types.Scene.ee_budget_cached_valid = bpy.props.BoolProperty(
        name="Budget Cached Valid",
        default=False
    )

    bpy.types.Scene.ee_budget_cached_count = bpy.props.IntProperty(
        name="Cached Budget Count",
        default=0,
        min=0
    )

    bpy.types.Scene.ee_budget_cached_mode = bpy.props.StringProperty(
        name="Cached Budget Mode",
        default=""
    )

    bpy.types.Scene.ee_budget_cached_signature = bpy.props.StringProperty(
        name="Cached Budget Signature",
        default=""
    )

    bpy.types.Scene.ee_budget_cached_state = bpy.props.StringProperty(
        name="Cached Budget State",
        default=""
    )

    bpy.types.Scene.ta_export_profile = bpy.props.EnumProperty(
        name="Export Preset",
        description="Reproducible export format/settings for the target workflow",
        items=list(export_profile_items()),
        default="GENERIC_FBX",
    )

    bpy.types.Scene.ee_export_path = bpy.props.StringProperty(
        name="Export to:",
        subtype="DIR_PATH",
        default="//"
    )
    bpy.types.Scene.ee_overwrite_existing = bpy.props.BoolProperty(
        name="Overwrite Existing", description="Allow export to replace an existing target file", default=False
    )
    bpy.types.Scene.ta_export_preview = bpy.props.StringProperty(name="Export Preview", default="")

    bpy.types.Scene.ta_validation_message = bpy.props.StringProperty(
        name="Validation Message",
        default=""
    )

    bpy.types.Scene.ta_profile_id = bpy.props.EnumProperty(
        name="Asset Profile",
        description="Profile-aware validation semantics used by Asset Doctor",
        items=ta_profile_enum_items,
    )
    bpy.types.Scene.ta_profile_file = bpy.props.StringProperty(
        name="Project Profiles",
        description="JSON file containing project-specific Asset Doctor profiles",
        subtype="FILE_PATH",
        default="",
    )
    bpy.types.Scene.ta_rule_file = bpy.props.StringProperty(
        name="Project Rules",
        description="Python file that registers project-specific Asset Doctor rules",
        subtype="FILE_PATH",
        default="",
    )

    bpy.types.Scene.ta_asset_doctor_summary = bpy.props.StringProperty(
        name="Asset Doctor Summary",
        default="",
    )

    bpy.types.Scene.ta_enforce_project_budget = bpy.props.BoolProperty(
        name="Enforce Project Budget",
        description="Use the Budget panel values as Asset Doctor performance limits",
        default=False,
    )

    bpy.types.Scene.ta_asset_report_path = bpy.props.StringProperty(
        name="Asset Report",
        description="Machine-readable JSON report path",
        subtype="FILE_PATH",
        default="//jam_asset_report.json",
    )

    bpy.types.Scene.ta_show_split_details = bpy.props.BoolProperty(
        name="Render Split Details",
        description="Show why modelling vertices split into additional render vertices",
        default=True,
    )

    bpy.types.Scene.ta_asset_set_root = bpy.props.StringProperty(
        name="Asset Set Root",
        description="Stable root name used to group deliverable asset members",
        default="",
    )
    bpy.types.Scene.ta_asset_set_summary = bpy.props.StringProperty(name="Asset Set Summary", default="")
    bpy.types.Scene.ta_asset_member_role = bpy.props.EnumProperty(
        name="Member Role",
        items=[
            (ROLE_RENDER, "Render", "Base render member / LOD0"),
            (ROLE_LOD, "LOD", "Reduced render member"),
            (ROLE_COLLISION, "Collision", "Collision geometry"),
            (ROLE_SOCKET, "Socket", "Socket / attachment helper"),
            (ROLE_SKELETON, "Skeleton", "Armature / skeleton"),
            (ROLE_HELPER, "Helper", "Other export helper"),
        ],
        default=ROLE_RENDER,
    )
    bpy.types.Scene.ta_asset_member_lod = bpy.props.IntProperty(name="LOD Level", default=0, min=0, max=16)
    bpy.types.Scene.ta_asset_collision_type = bpy.props.EnumProperty(
        name="Collision Type",
        items=[
            ("CONVEX", "Convex / UCX", "Custom convex collision"),
            ("BOX", "Box / UBX", "Box collision"),
            ("SPHERE", "Sphere / USP", "Sphere collision"),
            ("CAPSULE", "Capsule / UCP", "Capsule collision"),
        ],
        default="CONVEX",
    )
    bpy.types.Scene.ta_asset_member_export = bpy.props.BoolProperty(name="Export Member", default=True)
    bpy.types.Scene.ta_lod_generate_level = bpy.props.IntProperty(name="Generated LOD", default=1, min=1, max=16)
    bpy.types.Scene.ta_lod_generate_ratio = bpy.props.FloatProperty(name="LOD Ratio", default=0.5, min=0.01, max=1.0, subtype="FACTOR")
    bpy.types.Scene.ta_lod_min_reduction = bpy.props.FloatProperty(
        name="Minimum LOD Reduction",
        description="Warn when aggregate triangles fall by less than this percentage between consecutive LODs",
        default=20.0, min=0.0, max=99.0,
    )
    bpy.types.Scene.ta_require_contiguous_lods = bpy.props.BoolProperty(name="Require Contiguous LODs", default=True)
    bpy.types.Scene.ta_enforce_collision_budget = bpy.props.BoolProperty(name="Enforce Collision Budget", default=False)
    bpy.types.Scene.ta_collision_triangle_budget = bpy.props.IntProperty(name="Collision Triangle Budget", default=256, min=1)
    bpy.types.Scene.ta_asset_set_move_to_origin = bpy.props.BoolProperty(name="Move Asset Set to Origin", default=True)

    bpy.types.Scene.ta_modular_grid_cm = bpy.props.FloatProperty(
        name="Grid (cm)", description="Physical modular-kit grid size in centimetres", default=100.0, min=0.001
    )
    bpy.types.Scene.ta_modular_tolerance_cm = bpy.props.FloatProperty(
        name="Tolerance (cm)", description="Allowed distance from a modular-grid multiple", default=0.1, min=0.0
    )
    bpy.types.Scene.ta_modular_check_position = bpy.props.BoolProperty(
        name="Check world placement", description="Also require world-space bounds to land on grid lines", default=True
    )
    bpy.types.Scene.ta_modular_snap_x = bpy.props.BoolProperty(name="Snap X", default=True)
    bpy.types.Scene.ta_modular_snap_y = bpy.props.BoolProperty(name="Snap Y", default=True)
    bpy.types.Scene.ta_modular_snap_z = bpy.props.BoolProperty(name="Snap Z", default=True)
    bpy.types.Scene.ta_modular_snap_mode = bpy.props.EnumProperty(
        name="Snap mode", items=[("NEAREST", "Nearest", "Nearest grid cell"), ("FLOOR", "Floor", "Previous grid cell"), ("CEIL", "Ceil", "Next grid cell")], default="NEAREST"
    )
    bpy.types.Scene.ta_modular_align_anchor = bpy.props.EnumProperty(
        name="Bounds Anchor",
        items=[
            ("CENTER", "Center", "Bounds center"), ("BOTTOM", "Bottom", "Bottom-center"), ("TOP", "Top", "Top-center"),
            ("X_MIN", "X Min", "Minimum X face center"), ("X_MAX", "X Max", "Maximum X face center"),
            ("Y_MIN", "Y Min", "Minimum Y face center"), ("Y_MAX", "Y Max", "Maximum Y face center"),
            ("Z_MIN", "Z Min", "Minimum Z face center"), ("Z_MAX", "Z Max", "Maximum Z face center"),
        ],
        default="BOTTOM",
    )
    bpy.types.Scene.ta_modular_result = bpy.props.StringProperty(name="Modular Result", default="")
    bpy.types.Scene.ta_modular_match_x = bpy.props.BoolProperty(name="Match X", default=True)
    bpy.types.Scene.ta_modular_match_y = bpy.props.BoolProperty(name="Match Y", default=True)
    bpy.types.Scene.ta_modular_match_z = bpy.props.BoolProperty(name="Match Z", default=True)
    bpy.types.Scene.ta_attachment_role = bpy.props.EnumProperty(
        name="Attachment Role", items=[(ROLE_SOCKET, "Socket", "Engine attachment/socket"), (ROLE_HELPER, "Helper", "Generic export helper")], default=ROLE_SOCKET
    )
    bpy.types.Scene.ta_attachment_label = bpy.props.StringProperty(name="Attachment Label", default="Handle")
    bpy.types.Scene.ta_attachment_location = bpy.props.EnumProperty(
        name="Attachment Location",
        items=[("ACTIVE_ORIGIN", "Active Origin", "At the active object's origin"), ("CURSOR", "3D Cursor", "At the 3D cursor"), ("BOUNDS_CENTER", "Bounds Center", "Center of selected bounds"), ("BOUNDS_BOTTOM", "Bounds Bottom", "Bottom-center of selected bounds")],
        default="ACTIVE_ORIGIN",
    )
    bpy.types.Scene.ta_attachment_size_cm = bpy.props.FloatProperty(name="Display Size (cm)", default=10.0, min=0.01)
    bpy.types.Scene.ta_attachment_parent = bpy.props.BoolProperty(name="Parent to Active", default=True)
    bpy.types.Scene.ta_normalize_scale = bpy.props.BoolProperty(name="Scale", default=True)
    bpy.types.Scene.ta_normalize_rotation = bpy.props.BoolProperty(name="Rotation", default=False)
    bpy.types.Scene.ta_normalize_allow_negative = bpy.props.BoolProperty(
        name="Allow negative scale", description="Apply mirrored/negative scale too; off by default because this can change winding", default=False
    )

    bpy.types.Scene.ta_uv_map_name = bpy.props.StringProperty(
        name="UV Map",
        description="UV map to create or overwrite",
        default="TA_Planar"
    )

    bpy.types.Scene.ta_uv_projection_mode = bpy.props.EnumProperty(
        name="Projection",
        description="Planar projection mode",
        items=[
            ("XY", "XY", "Project onto XY plane"),
            ("XZ", "XZ", "Project onto XZ plane"),
            ("YZ", "YZ", "Project onto YZ plane"),
            ("NORMAL", "Normal", "Best-fit projection from selected face normals"),
        ],
        default="NORMAL"
    )

    bpy.types.Scene.ta_uv_projection_space = bpy.props.EnumProperty(
        name="Space",
        description="Use local or world coordinates for projection",
        items=[
            ("LOCAL", "Local", "Use object-local coordinates"),
            ("WORLD", "World", "Use world coordinates"),
        ],
        default="LOCAL"
    )

    bpy.types.Scene.ta_uv_fit_mode = bpy.props.EnumProperty(
        name="Fit",
        description="How selected faces are fitted into UV space",
        items=[
            ("SELECTION", "Selection", "Fit all selected faces as one projection"),
            ("ISLANDS", "Islands", "Fit each connected selected face island separately"),
        ],
        default="SELECTION"
    )

    bpy.types.Scene.ta_uv_preserve_aspect = bpy.props.BoolProperty(
        name="Preserve Aspect",
        description="Preserve projected proportions",
        default=True
    )

    bpy.types.Scene.ta_uv_padding = bpy.props.FloatProperty(
        name="Padding",
        description="Padding inside the 0-1 UV area",
        default=0.02,
        min=0.0,
        max=0.49,
        soft_max=0.25
    )

    bpy.types.Scene.ta_uv_rotation = bpy.props.EnumProperty(
        name="Rotate",
        description="Rotate projected UVs",
        items=[
            ("0", "0°", "No rotation"),
            ("90", "90°", "Rotate 90 degrees"),
            ("180", "180°", "Rotate 180 degrees"),
            ("270", "270°", "Rotate 270 degrees"),
        ],
        default="0"
    )

    bpy.types.Scene.ta_uv_flip_u = bpy.props.BoolProperty(
        name="Flip U",
        default=False
    )

    bpy.types.Scene.ta_uv_flip_v = bpy.props.BoolProperty(
        name="Flip V",
        default=False
    )

    bpy.types.Scene.ta_uv_checks = bpy.props.BoolProperty(
        name="Include UV checks on export",
        description=(
            "Also run UV validation (missing, 0-1 range, flipped, "
            "zero-area) when validating before export"
        ),
        default=True
    )

    bpy.types.Scene.ee_validate_export = bpy.props.BoolProperty(
        name="Validate",
        description="Run validation before exporting",
        default=True
    )

    bpy.types.Scene.ee_block_export = bpy.props.BoolProperty(
        name="Block on issues",
        description="Cancel the export if validation finds issues",
        default=True
    )

    bpy.types.Scene.ee_batch_export = bpy.props.BoolProperty(
        name="Batch (one file per object)",
        description="Export each selected object to its own FBX file",
        default=False
    )

    bpy.types.Scene.ee_move_to_origin = bpy.props.BoolProperty(
        name="Move to origin",
        description=(
            "Move each object to the world origin for its export and "
            "restore it afterwards (batch only)"
        ),
        default=True
    )

    bpy.types.Scene.ee_write_sidecar = bpy.props.BoolProperty(
        name="Write Engine Sidecar",
        description="Write a .jammeta.json file next to each FBX for engine-side verification",
        default=True,
    )

    bpy.types.Scene.ta_td_texture_size = bpy.props.IntProperty(
        name="Texture size",
        description="Texture resolution in pixels used for the density",
        default=2048,
        min=1
    )

    bpy.types.Scene.ta_td_target = bpy.props.FloatProperty(
        name="Target TD",
        description="Target texel density in the selected display unit",
        default=10.24,
        min=0.001,
        precision=3
    )

    bpy.types.Scene.ta_td_unit = bpy.props.EnumProperty(
        name="TD Unit",
        description="Display texel density in pixels per centimetre or metre",
        items=[
            ("PX_CM", "px/cm", "Pixels per centimetre"),
            ("PX_M", "px/m", "Pixels per metre"),
        ],
        default="PX_CM",
    )

    bpy.types.Scene.ta_td_tolerance = bpy.props.FloatProperty(
        name="TD tolerance (%)",
        description="Allowed per-face deviation from the target when Asset Doctor enforcement is enabled",
        default=15.0,
        min=0.0,
        soft_max=100.0,
    )

    bpy.types.Scene.ta_enforce_td_target = bpy.props.BoolProperty(
        name="Enforce TD Target",
        description="Report faces outside the configured texel-density tolerance",
        default=False,
    )

    bpy.types.Scene.ta_td_result = bpy.props.StringProperty(
        name="TD Result",
        default=""
    )

    bpy.types.Scene.ta_ik_chain_count = bpy.props.IntProperty(
        name="Chain length",
        description="How many bones the IK chain includes",
        default=2,
        min=2
    )

    bpy.types.Scene.ta_pole_distance = bpy.props.FloatProperty(
        name="Pole distance",
        description="Pole distance as a factor of half the chain length",
        default=1.0,
        min=0.1,
        precision=2
    )

    bpy.types.Scene.ta_muscle_axis = bpy.props.EnumProperty(
        name="Bend axis",
        description="Local rotation axis of the bend bone that drives the bulge",
        items=[
            ("X", "X", "Local X rotation"),
            ("Y", "Y", "Local Y rotation"),
            ("Z", "Z", "Local Z rotation"),
        ],
        default="X"
    )

    bpy.types.Scene.ta_muscle_max_angle = bpy.props.FloatProperty(
        name="Max angle",
        description="Bend angle (degrees) at which the bulge is fully applied",
        default=90.0,
        min=1.0,
        soft_max=180.0
    )

    bpy.types.Scene.ta_muscle_bulge = bpy.props.FloatProperty(
        name="Bulge scale",
        description="Helper bone scale at the max bend angle",
        default=1.4,
        min=0.1,
        precision=2
    )


def unregister():
    if ta_invalidate_analysis_cache in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(ta_invalidate_analysis_cache)

    del bpy.types.Scene.ta_muscle_bulge
    del bpy.types.Scene.ta_muscle_max_angle
    del bpy.types.Scene.ta_muscle_axis
    del bpy.types.Scene.ta_pole_distance
    del bpy.types.Scene.ta_ik_chain_count
    del bpy.types.Scene.ta_enforce_td_target
    del bpy.types.Scene.ta_td_tolerance
    del bpy.types.Scene.ta_td_unit
    del bpy.types.Scene.ta_td_result
    del bpy.types.Scene.ta_td_target
    del bpy.types.Scene.ta_td_texture_size
    del bpy.types.Scene.ee_write_sidecar
    del bpy.types.Scene.ee_move_to_origin
    del bpy.types.Scene.ee_batch_export
    del bpy.types.Scene.ee_block_export
    del bpy.types.Scene.ee_validate_export
    del bpy.types.Scene.ta_uv_checks
    del bpy.types.Scene.ta_uv_flip_v
    del bpy.types.Scene.ta_uv_flip_u
    del bpy.types.Scene.ta_uv_rotation
    del bpy.types.Scene.ta_uv_padding
    del bpy.types.Scene.ta_uv_preserve_aspect
    del bpy.types.Scene.ta_uv_fit_mode
    del bpy.types.Scene.ta_uv_projection_space
    del bpy.types.Scene.ta_uv_projection_mode
    del bpy.types.Scene.ta_normalize_allow_negative
    del bpy.types.Scene.ta_normalize_rotation
    del bpy.types.Scene.ta_normalize_scale
    del bpy.types.Scene.ta_attachment_parent
    del bpy.types.Scene.ta_attachment_size_cm
    del bpy.types.Scene.ta_attachment_location
    del bpy.types.Scene.ta_attachment_label
    del bpy.types.Scene.ta_attachment_role
    del bpy.types.Scene.ta_modular_result
    del bpy.types.Scene.ta_modular_match_x
    del bpy.types.Scene.ta_modular_match_y
    del bpy.types.Scene.ta_modular_match_z
    del bpy.types.Scene.ta_modular_align_anchor
    del bpy.types.Scene.ta_modular_snap_mode
    del bpy.types.Scene.ta_modular_snap_z
    del bpy.types.Scene.ta_modular_snap_y
    del bpy.types.Scene.ta_modular_snap_x
    del bpy.types.Scene.ta_modular_check_position
    del bpy.types.Scene.ta_modular_tolerance_cm
    del bpy.types.Scene.ta_modular_grid_cm
    del bpy.types.Scene.ta_asset_set_move_to_origin
    del bpy.types.Scene.ta_collision_triangle_budget
    del bpy.types.Scene.ta_enforce_collision_budget
    del bpy.types.Scene.ta_require_contiguous_lods
    del bpy.types.Scene.ta_lod_min_reduction
    del bpy.types.Scene.ta_lod_generate_ratio
    del bpy.types.Scene.ta_lod_generate_level
    del bpy.types.Scene.ta_asset_member_export
    del bpy.types.Scene.ta_asset_collision_type
    del bpy.types.Scene.ta_asset_member_lod
    del bpy.types.Scene.ta_asset_member_role
    del bpy.types.Scene.ta_asset_set_summary
    del bpy.types.Scene.ta_asset_set_root
    del bpy.types.Scene.ta_uv_map_name
    del bpy.types.Scene.ta_show_split_details
    del bpy.types.Scene.ta_asset_report_path
    del bpy.types.Scene.ta_enforce_project_budget
    del bpy.types.Scene.ta_asset_doctor_summary
    del bpy.types.Scene.ta_rule_file
    del bpy.types.Scene.ta_profile_file
    del bpy.types.Scene.ta_profile_id
    del bpy.types.Scene.ta_validation_message
    del bpy.types.Scene.ta_export_preview
    del bpy.types.Scene.ee_overwrite_existing
    del bpy.types.Scene.ee_export_path
    del bpy.types.Scene.ta_export_profile
    del bpy.types.Scene.ee_budget_cached_state
    del bpy.types.Scene.ee_budget_cached_signature
    del bpy.types.Scene.ee_budget_cached_mode
    del bpy.types.Scene.ee_budget_cached_count
    del bpy.types.Scene.ee_budget_cached_valid
    del bpy.types.Scene.ee_hp_prefix
    del bpy.types.Scene.ee_lp_prefix
    del bpy.types.Scene.ee_auto_poly_prefix
    del bpy.types.Scene.ee_count_modifiers
    del bpy.types.Scene.ee_count_material_splits
    del bpy.types.Scene.ee_budget_count_mode
    del bpy.types.Scene.ee_poly_margin
    del bpy.types.Scene.ee_poly_budget
    del bpy.types.Scene.individual_toggle
    del bpy.types.Scene.z_axis
    del bpy.types.Scene.y_axis
    del bpy.types.Scene.x_axis
    del bpy.types.Scene.ee_unify
    del bpy.types.Scene.rs_name
    del bpy.types.Scene.rs_prefix

    bpy.utils.unregister_class(TA_PT_rigging)
    bpy.utils.unregister_class(TA_PT_texel_density)
    bpy.utils.unregister_class(TA_PT_easy_export)
    bpy.utils.unregister_class(TA_PT_rename_tool_panel)
    bpy.utils.unregister_class(TA_PT_asset_sets)
    bpy.utils.unregister_class(TA_PT_authoring)
    bpy.utils.unregister_class(TA_PT_asset_doctor)
    bpy.utils.unregister_class(TA_PT_tools_panel)

    bpy.utils.unregister_class(TA_OT_load_project_rules)
    bpy.utils.unregister_class(TA_OT_load_project_profiles)
    bpy.utils.unregister_class(TA_OT_asset_doctor_export_report)
    bpy.utils.unregister_class(TA_OT_asset_doctor_select_split_cause)
    bpy.utils.unregister_class(TA_OT_asset_doctor_select_issue)
    bpy.utils.unregister_class(TA_OT_asset_doctor_fix_safe)
    bpy.utils.unregister_class(TA_OT_asset_doctor_analyze)
    bpy.utils.unregister_class(TA_OT_asset_set_export)
    bpy.utils.unregister_class(TA_OT_asset_set_generate_lod)
    bpy.utils.unregister_class(TA_OT_asset_set_create_sphere_collision)
    bpy.utils.unregister_class(TA_OT_asset_set_create_box_collision)
    bpy.utils.unregister_class(TA_OT_asset_set_select)
    bpy.utils.unregister_class(TA_OT_asset_set_analyze)
    bpy.utils.unregister_class(TA_OT_asset_set_assign_role)
    bpy.utils.unregister_class(TA_OT_asset_set_create)
    bpy.utils.unregister_class(TA_OT_normalize_transforms)
    bpy.utils.unregister_class(TA_OT_modular_match_bounds)
    bpy.utils.unregister_class(TA_OT_modular_align_anchor)
    bpy.utils.unregister_class(TA_OT_modular_snap)
    bpy.utils.unregister_class(TA_OT_modular_analyze)
    bpy.utils.unregister_class(TA_OT_create_attachment)
    bpy.utils.unregister_class(TA_OT_create_muscle_helper)
    bpy.utils.unregister_class(TA_OT_create_ik_pole)
    bpy.utils.unregister_class(TA_OT_set_texel_density)
    bpy.utils.unregister_class(TA_OT_clear_td_heatmap)
    bpy.utils.unregister_class(TA_OT_td_heatmap)
    bpy.utils.unregister_class(TA_OT_check_texel_density)
    bpy.utils.unregister_class(TA_OT_check_uvs)
    bpy.utils.unregister_class(TA_OT_apply_defaults)
    bpy.utils.unregister_class(TA_OT_save_defaults)

    bpy.utils.unregister_class(TA_OT_preview_export)
    bpy.utils.unregister_class(TA_OT_planar_project_selected_uv)
    bpy.utils.unregister_class(TA_OT_calculate_budget)
    bpy.utils.unregister_class(TA_OT_check_non_manifold)
    bpy.utils.unregister_class(TA_OT_unify)
    bpy.utils.unregister_class(TA_OT_move_origin)
    bpy.utils.unregister_class(TA_OT_export_selected)
    bpy.utils.unregister_class(TA_OT_rename_selected)


if __name__ == "__main__":
    register()
