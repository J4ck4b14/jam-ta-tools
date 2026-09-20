# -*- coding: utf-8 -*-
"""
JAM TA Tools for Autodesk Maya
Author: Juan Abia Merino

JAM TA Tools for game-art workflows in Maya.
but the suite now includes a shared profile/report core, Asset Doctor analysis
and detailed render-vertex split attribution alongside renaming, pivots, UVs,
texel density, multi-format export and rigging helpers.

Install/Run:
    1. Save this file somewhere Maya can access
       (e.g. your Documents/maya/scripts folder).
    2. In Maya's Script Editor (Python tab):

        import sys
        sys.path.append(r"path/to/folder/containing/this/file")
        import ta_tools as jamta
        jamta.show()
"""

from __future__ import annotations

import glob
import math
import os
import re
import sys
from functools import wraps
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

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

try:
    import maya.cmds as cmds
    import maya.mel as mel
    import maya.api.OpenMaya as om
    import maya.api.OpenMayaAnim as oma
except Exception:
    # Allow syntax checking / linting outside Maya
    cmds = None
    mel = None
    om = None
    oma = None

WINDOW_NAME = "JAM_TA_TOOLS_MAYA_WINDOW"
WORKSPACE_CONTROL_NAME = "JAMTAToolsWorkspaceControl"
WINDOW_TITLE = "JAM TA Tools"

X_AXIS_ITEMS = ["None", "X", "-X", "Mid"]
Y_AXIS_ITEMS = ["None", "Y", "-Y", "Mid"]
Z_AXIS_ITEMS = ["None", "Z", "-Z", "Mid"]
COUNT_MODES = ["Estimated Game Verts", "Tris", "Faces", "Verts"]
PROFILE_ITEMS = []
PROFILE_LABEL_TO_ID = {}
PROFILE_ID_TO_LABEL = {}


def _refresh_profile_catalog() -> None:
    global PROFILE_ITEMS, PROFILE_LABEL_TO_ID, PROFILE_ID_TO_LABEL
    PROFILE_ITEMS = list(profile_items())
    PROFILE_LABEL_TO_ID = {label: profile_id for profile_id, label, _description in PROFILE_ITEMS}
    PROFILE_ID_TO_LABEL = {profile_id: label for profile_id, label, _description in PROFILE_ITEMS}


_refresh_profile_catalog()
EXPORT_PROFILE_ITEMS = list(export_profile_items())
EXPORT_PROFILE_LABEL_TO_ID = {label: profile_id for profile_id, label, _description in EXPORT_PROFILE_ITEMS}
EXPORT_PROFILE_ID_TO_LABEL = {profile_id: label for profile_id, label, _description in EXPORT_PROFILE_ITEMS}
MAYA_LAST_REPORTS = []
MAYA_LAST_ASSET_SET = None
MAYA_TD_HEATMAP_NAME = "JAM_TD_HEATMAP"

# ---
# General Helpers
# ---


def _require_maya() -> None:
    if cmds is None or mel is None or om is None or oma is None:
        raise RuntimeError("This tool must be run inside Autodesk Maya")


def _long_name(node: str) -> str:
    result = cmds.ls(node, long=True) or []
    return result[0] if result else node


def _short_name(node: str) -> str:
    return node.split("|")[-1]


def _strip_namespace(name: str) -> str:
    return name.split(":")[-1]


def _unique_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []

    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)

    return out


def _round_tuple(values: Sequence[float], precision: int = 6) -> Tuple[float, ...]:
    return tuple(round(float(v), precision) for v in values)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _status(message: str, warning: bool = False) -> None:
    if warning:
        cmds.warning(message)
    else:
        print("JAM TA Tools: " + message)


def undoable(func):
    """
    Wrap an operation in a single undo chunk so a whole batch
    (e.g. renaming 50 objects) undoes with one Ctrl+Z.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        if cmds is None:
            return func(*args, **kwargs)

        cmds.undoInfo(openChunk=True, chunkName=func.__name__)

        try:
            return func(*args, **kwargs)
        finally:
            cmds.undoInfo(closeChunk=True)

    return wrapper


# ---
# Settings persistence (optionVar)
# ---

_OPTIONVAR_PREFIX = "JAMTATools_"


def _save_option(name: str, value) -> None:
    if cmds is None:
        return

    cmds.optionVar(stringValue=(_OPTIONVAR_PREFIX + name, str(value)))


def _load_option(name: str, default: str = "") -> str:
    full_name = _OPTIONVAR_PREFIX + name

    if cmds is not None and cmds.optionVar(exists=full_name):
        return str(cmds.optionVar(query=full_name))

    return default

# ---
# Mesh selection and geometry helpers
# ---


def get_mesh_shapes(transform: str, no_intermediate: bool = True) -> List[str]:
    """
    Return mesh shape nodes below a transform
    """

    transform = _long_name(transform)

    shapes = cmds.listRelatives(
        transform,
        shapes=True,
        fullPath=True,
        type="mesh",
    ) or []

    if not no_intermediate:
        return shapes

    out = []

    for shape in shapes:
        try:
            if not cmds.getAttr(shape + ".intermediateObject"):
                out.append(shape)
        except Exception:
            out.append(shape)

    return out


def node_to_mesh_transform(node: str) -> Optional[str]:
    """
    Return the transform if node is/contains a visible mesh shape
    """

    if not cmds.objExists(node):
        return None

    node_type = cmds.nodeType(node)

    if node_type == "mesh":
        parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
        return parents[0] if parents else None

    if node_type == "transform" and get_mesh_shapes(node):
        return _long_name(node)

    return None


def get_selected_mesh_transforms() -> List[str]:
    """
    Return selected transforms that contain non-intermediate mesh shapes
    """

    selection = cmds.ls(selection=True, long=True) or []

    # If components are selected, try to resolve them back to owning objects
    if selection:
        converted = (
            cmds.ls(
                cmds.polyListComponentConversion(selection, toVertex=True),
                objectsOnly=True,
                long=True
            )
            or []
        )
        selection.extend(converted)

    transforms = []

    for node in selection:
        transform = node_to_mesh_transform(node)

        if transform:
            transforms.append(transform)

    return _unique_preserve_order(transforms)


def get_active_mesh_transform() -> Optional[str]:
    selection = cmds.ls(selection=True, long=True) or []

    if not selection:
        return None

    for node in selection:
        transform = node_to_mesh_transform(node)

        if transform:
            return transform

    return None


def get_world_bbox(obj: str) -> Optional[Tuple[float, float, float,
                                                float, float, float]]:
    """
    Return world bounding box as xMin, yMin, zMin, xMax, yMax, zMax
    """

    try:
        bbox = cmds.xform(obj, query=True, worldSpace=True, boundingBox=True)
    except Exception:
        return None

    if not bbox or len(bbox) != 6:
        return None

    return tuple(float(v) for v in bbox)


def combine_bboxes(
        bboxes: Iterable[Sequence[float]],
) -> Optional[Tuple[float, float, float, float, float, float]]:
    bboxes = [b for b in bboxes if b and len(b) == 6]

    if not bboxes:
        return None

    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        min(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
        max(b[4] for b in bboxes),
        max(b[5] for b in bboxes),
    )


def bbox_axis_value(bbox: Sequence[float], axis_index: int, mode: str) -> float:
    """
    Resolve one axis value from bbox using Maya/World axes
    """

    if mode == "Mid":
        return (float(bbox[axis_index]) + float(bbox[axis_index + 3])) * 0.5

    # Positive = max, negative = min
    if mode.startswith("-"):
        return float(bbox[axis_index])

    return float(bbox[axis_index + 3])


def current_pivot(obj: str) -> Tuple[float, float, float]:
    pivot = cmds.xform(
        obj,
        query=True,
        worldSpace=True,
        rotatePivot=True,
    )

    return float(pivot[0]), float(pivot[1]), float(pivot[2])


def set_pivot_world(obj: str, pivot: Sequence[float]) -> None:
    """
    Set rotate and scale pivot in world space without moving the object
    """

    cmds.xform(
        obj,
        worldSpace=True,
        rotatePivot=pivot,
        scalePivot=pivot,
    )

# ---
# RENAME
# ---


@undoable
def rename_selected(
        prefix: str,
        item_name: str,
        start_index: int = 1,
        rename_shapes: bool = True,
) -> List[str]:
    meshes = get_selected_mesh_transforms()

    if not meshes:
        raise RuntimeError("No mesh transforms selected")

    # Track objects by UUID: renaming a parent transform invalidates the
    # stored DAG paths of any selected children, so paths captured up front
    # can go stale mid-loop. UUIDs survive renames.
    uuids = [cmds.ls(obj, uuid=True)[0] for obj in meshes]

    renamed = []
    width = max(2, len(str(start_index + len(meshes) - 1)))

    for offset, uuid in enumerate(uuids):
        index = start_index + offset

        current_paths = cmds.ls(uuid, long=True) or []
        if not current_paths:
            continue
        obj = current_paths[0]

        new_name = "{0}_{1}_{2:0{3}d}".format(
            prefix,
            item_name,
            index,
            width,
        )

        new_transform = cmds.rename(obj, new_name)
        new_transform = _long_name(new_transform)

        renamed.append(new_transform)

        if rename_shapes:
            shapes = get_mesh_shapes(new_transform, no_intermediate=False)

            for shape_index, shape in enumerate(shapes, start=1):
                suffix = "Shape" if shape_index == 1 else "Shape{0}".format(shape_index)

                try:
                    cmds.rename(shape, new_name + suffix)
                except Exception:
                    pass

    return renamed

# ---
# Budget / metric counting
# ---


def _get_dag_path(shape: str):
    sel = om.MSelectionList()
    sel.add(shape)

    return sel.getDagPath(0)


def get_shape_metric(
        shape: str,
        mode: str,
        count_material_splits: bool = True,
) -> int:
    if mode == "Verts":
        return _safe_int(cmds.polyEvaluate(shape, vertex=True), 0)

    if mode == "Faces":
        return _safe_int(cmds.polyEvaluate(shape, face=True), 0)

    if mode == "Tris":
        return _safe_int(cmds.polyEvaluate(shape, triangle=True), 0)

    return estimate_game_vertices(
        shape,
        count_material_splits=count_material_splits,
    )


def get_mesh_metric(
        obj: str,
        mode: str,
        count_material_splits: bool = True,
) -> int:
    total = 0

    for shape in get_mesh_shapes(obj):
        total += get_shape_metric(
            shape,
            mode,
            count_material_splits=count_material_splits,
        )

    return total


def get_selection_metric(
        mode: str,
        count_material_splits: bool = True,
) -> int:
    return sum(
        get_mesh_metric(
            obj,
            mode,
            count_material_splits=count_material_splits
        )
        for obj in get_selected_mesh_transforms()
    )


def get_shape_render_split_breakdown(
        shape: str,
        count_material_splits: bool = True,
) -> Dict[str, object]:
    """Detailed render-vertex estimate for one Maya mesh shape."""

    dag = _get_dag_path(shape)
    mesh_fn = om.MFnMesh(dag)
    iterator = om.MItMeshPolygon(dag)

    try:
        uv_sets = list(mesh_fn.getUVSetNames())
    except Exception:
        uv_sets = []

    shader_indices = []
    if count_material_splits:
        try:
            _shaders, shader_indices = mesh_fn.getConnectedShaders(dag.instanceNumber())
        except Exception:
            shader_indices = []

    corners = []

    while not iterator.isDone():
        face_index = iterator.index()
        vertex_ids = iterator.getVertices()

        material_index = -1
        if count_material_splits and face_index < len(shader_indices):
            material_index = int(shader_indices[face_index])

        for local_index, vertex_id in enumerate(vertex_ids):
            try:
                normal = iterator.getNormal(local_index, om.MSpace.kWorld)
                rounded_normal = _round_tuple((normal.x, normal.y, normal.z))
            except Exception:
                rounded_normal = (0.0, 0.0, 0.0)

            uvs = []
            for uv_set in uv_sets:
                try:
                    uv = iterator.getUV(local_index, uv_set)
                    uvs.append(_round_tuple((uv[0], uv[1])))
                except Exception:
                    uvs.append(None)

            corners.append({
                "vertex": int(vertex_id),
                "normal": rounded_normal,
                "uvs": uvs,
                "material": material_index,
            })

        iterator.next()

    result = analyze_render_splits(
        corners,
        uv_sets,
        include_material=count_material_splits,
    )
    result["triangle_count"] = _safe_int(cmds.polyEvaluate(shape, triangle=True), 0)
    result["face_count"] = _safe_int(cmds.polyEvaluate(shape, face=True), 0)
    return result


def get_mesh_render_split_breakdown(
        obj: str,
        count_material_splits: bool = True,
) -> Dict[str, object]:
    """Aggregate render-split metrics across all non-intermediate mesh shapes."""

    totals = {
        "base_vertices": 0,
        "render_vertices": 0,
        "extra_vertices": 0,
        "corner_count": 0,
        "triangle_count": 0,
        "face_count": 0,
        "uv_layer_names": [],
        "stages": [],
    }
    stage_totals = {}
    uv_names = []

    for shape in get_mesh_shapes(obj):
        result = get_shape_render_split_breakdown(
            shape,
            count_material_splits=count_material_splits,
        )
        for key in ("base_vertices", "render_vertices", "extra_vertices", "corner_count", "triangle_count", "face_count"):
            totals[key] += int(result.get(key, 0))

        for uv_name in result.get("uv_layer_names", []):
            if uv_name not in uv_names:
                uv_names.append(uv_name)

        for stage in result.get("stages", []):
            stage_id = str(stage.get("id", "unknown"))
            data = stage_totals.setdefault(stage_id, {
                "id": stage_id,
                "label": str(stage.get("label", stage_id)),
                "count": 0,
                "added": 0,
            })
            data["count"] += int(stage.get("count", 0))
            data["added"] += int(stage.get("added", 0))

    totals["uv_layer_names"] = uv_names
    totals["stages"] = list(stage_totals.values())
    base = int(totals["base_vertices"])
    totals["split_ratio"] = (
        float(totals["render_vertices"]) / base if base else 0.0
    )
    return totals


def estimate_game_vertices(
        shape: str,
        count_material_splits: bool = True,
) -> int:
    """Return the render-vertex estimate used by the budget display."""
    return int(get_shape_render_split_breakdown(
        shape,
        count_material_splits=count_material_splits,
    )["render_vertices"])

def classify_budget(
        count: int,
        budget: int,
        margin_percent: float,
        lp_prefix: str,
        hp_prefix: str,
) -> Dict[str, object]:
    budget = max(int(budget), 1)
    margin = int(budget * (float(margin_percent) / 100.0))

    warning_start = max(0, budget - margin)
    high_limit = budget + margin

    if count > high_limit:
        return {
            "prefix": hp_prefix,
            "state": "HIGH",
            "label": "High",
            "limit": high_limit,
        }

    if count >= warning_start:
        return {
            "prefix": lp_prefix,
            "state": "MARGIN",
            "label": "Near budget",
            "limit": high_limit,
        }

    return {
        "prefix": lp_prefix,
        "state": "LOW",
        "label": "Low",
        "limit": high_limit,
    }


def remove_existing_poly_prefix(
        name: str,
        prefixes: Sequence[str],
) -> str:
    clean = name

    for prefix in prefixes:
        if not prefix:
            continue

        token = prefix + "_"

        if clean.startswith(token):
            clean = clean[len(token):]

    return clean


def export_name_with_prefix(
        base_name: str,
        count: int,
        budget: int,
        margin_percent: float,
        lp_prefix: str,
        hp_prefix: str,
):
    classification = classify_budget(
        count, budget, margin_percent, lp_prefix, hp_prefix,
    )

    clean_name = remove_existing_poly_prefix(
        _strip_namespace(_short_name(base_name)),
        [lp_prefix, hp_prefix],
    )

    return "{0}_{1}".format(classification["prefix"], clean_name), classification

# ---
# Pivot tool
# ---


@undoable
def set_selected_pivots(
        x_axis: str = "None",
        y_axis: str = "None",
        z_axis: str = "None",
        individual: bool = False,
) -> int:
    meshes = get_selected_mesh_transforms()

    if not meshes:
        raise RuntimeError("No mesh transforms selected")

    axis_settings = [
            (x_axis, 0),
            (y_axis, 1),
            (z_axis, 2)
    ]

    axis_settings = [(mode, index) for mode, index in axis_settings if mode != "None"]

    if not axis_settings:
        raise RuntimeError("No pivot axis selected")

    moved = 0

    if individual:
        for obj in meshes:
            bbox = get_world_bbox(obj)

            if not bbox:
                continue

            pivot = list(current_pivot(obj))

            for mode, axis_index in axis_settings:
                pivot[axis_index] = bbox_axis_value(
                    bbox, axis_index, mode,
                )

            set_pivot_world(obj, pivot)
            moved += 1

    else:
        shared_bbox = combine_bboxes(get_world_bbox(obj) for obj in meshes)

        if not shared_bbox:
            raise RuntimeError("Could not calculate selection bounding box")

        shared_values = {}

        for mode, axis_index in axis_settings:
            shared_values[axis_index] = bbox_axis_value(
                shared_bbox, axis_index, mode,
            )

        for obj in meshes:
            pivot = list(current_pivot(obj))

            for axis_index, value in shared_values.items():
                pivot[axis_index] = value

            set_pivot_world(obj, pivot)
            moved += 1

    return moved


@undoable
def move_pivot_to_selected_vertices() -> int:
    """
    Move each selected mesh object's pivot to its selected vertex position.

    If multiple vertices are selected, the pivot is moved to their
    average world position.
    """

    selected_vertices = cmds.ls(
        selection=True,
        flatten=True,
        long=True,
    ) or []

    selected_vertices = [
        component for component in selected_vertices if ".vtx[" in component
    ]

    if not selected_vertices:
        raise RuntimeError("No vertices selected")

    vertices_by_object = {}

    for vertex in selected_vertices:
        node = vertex.split(".")[0]
        transform = node_to_mesh_transform(node)

        if not transform:
            continue

        vertices_by_object.setdefault(transform, []).append(vertex)

    if not vertices_by_object:
        raise RuntimeError("Could not find a mesh object from the selected vertices")

    moved_count = 0

    for obj, vertices in vertices_by_object.items():
        points = []

        for vertex in vertices:
            try:
                point = cmds.pointPosition(vertex, world=True)
                points.append(point)

            except Exception:
                pass

        if not points:
            continue

        pivot = [
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
            sum(point[2] for point in points) / len(points),
        ]

        set_pivot_world(obj, pivot)
        moved_count += 1

    return moved_count

# ---
# Validation -> Manifolds
# ---


_COMPONENT_RE = re.compile(r"[^\s#]+\.(?:e|vtx|f)\[[^\]]+\]")


def _polyinfo_components(lines: Optional[Sequence[str]]) -> List[str]:
    if not lines:
        return []

    components = []

    for line in lines:
        components.extend(_COMPONENT_RE.findall(line))

    return components


def _uv_signed_area(us: Sequence[float], vs: Sequence[float]) -> float:
    """
    Signed area of a UV polygon via the shoelace formula.
    A negative value means the face's UV winding is flipped.
    """

    area = 0.0
    count = len(us)

    for i in range(count):
        j = (i + 1) % count
        area += us[i] * vs[j] - us[j] * vs[i]

    return area * 0.5


def maya_collect_uv_faces(obj: str, uv_channel_index: int = 0) -> List[Dict[str, object]]:
    """Collect face vertex ids and UVs for a specific Maya UV set index."""
    faces = []
    for shape in get_mesh_shapes(obj):
        try:
            dag = _get_dag_path(shape)
            mesh_fn = om.MFnMesh(dag)
            uv_sets = list(mesh_fn.getUVSetNames())
            if uv_channel_index < 0 or uv_channel_index >= len(uv_sets):
                continue
            uv_set = uv_sets[uv_channel_index]
            iterator = om.MItMeshPolygon(dag)
        except Exception:
            continue

        while not iterator.isDone():
            try:
                face_index = int(iterator.index())
                vertices = [int(v) for v in iterator.getVertices()]
                uvs = []
                valid = True
                for vertex_id in vertices:
                    try:
                        uv = mesh_fn.getPolygonUV(face_index, vertex_id, uv_set)
                        uvs.append((float(uv[0]), float(uv[1])))
                    except Exception:
                        valid = False
                        break
                if valid and len(uvs) == len(vertices):
                    faces.append({
                        "component": "{0}.f[{1}]".format(shape, face_index),
                        "vertices": vertices,
                        "uvs": uvs,
                    })
            except Exception:
                pass
            iterator.next()
    return faces


def maya_uv_layout_analysis(obj: str, texture_size: int, uv_channel_index: int = 0) -> Dict[str, object]:
    return analyze_uv_layout(
        maya_collect_uv_faces(obj, uv_channel_index),
        texture_size=texture_size,
    )


def check_shape_uvs(shape: str, tolerance: float = 0.001) -> Dict[str, object]:
    """
    UV checks for one mesh shape: missing UVs, UVs outside the 0-1
    range, flipped faces and zero-area faces (in the default UV set).

    Note: full UV overlap detection is intentionally out of scope here;
    it needs spatial acceleration to stay usable on production meshes.
    """

    result = {
        "missing_uvs": False,
        "uvs_out_of_range": False,
        "flipped_uv_faces": [],
        "zero_uv_faces": [],
    }

    if _safe_int(cmds.polyEvaluate(shape, uvcoord=True), 0) == 0:
        result["missing_uvs"] = True
        return result

    bbox2d = None

    try:
        bbox2d = cmds.polyEvaluate(shape, boundingBox2d=True)
    except Exception:
        bbox2d = None

    if bbox2d and len(bbox2d) == 2:
        (u_min, u_max), (v_min, v_max) = bbox2d

        if (u_min < -tolerance or v_min < -tolerance
                or u_max > 1.0 + tolerance or v_max > 1.0 + tolerance):
            result["uvs_out_of_range"] = True

    try:
        dag = _get_dag_path(shape)
        iterator = om.MItMeshPolygon(dag)
    except Exception:
        return result

    flipped = []
    zero_area = []

    while not iterator.isDone():
        face_index = iterator.index()

        try:
            if iterator.hasUVs():
                us, vs = iterator.getUVs()
                area = _uv_signed_area(us, vs)

                if abs(area) < 1e-9:
                    zero_area.append("{0}.f[{1}]".format(shape, face_index))
                elif area < 0.0:
                    flipped.append("{0}.f[{1}]".format(shape, face_index))
            else:
                result["missing_uvs"] = True
        except Exception:
            pass

        iterator.next()

    result["flipped_uv_faces"] = flipped
    result["zero_uv_faces"] = zero_area

    return result


def check_mesh_validation(
        obj: str,
        include_uv_checks: bool = True,
) -> Dict[str, object]:
    """
    Return validation data for one mesh transform
    """

    non_manifold_edges = []
    non_manifold_vertices = []
    lamina_faces = []

    try:
        non_manifold_edges = _polyinfo_components(
            cmds.polyInfo(obj, nonManifoldEdges=True),
        )
    except Exception:
        non_manifold_edges = []

    try:
        non_manifold_vertices = _polyinfo_components(
            cmds.polyInfo(obj, nonManifoldVertices=True),
        )
    except Exception:
        non_manifold_vertices = []

    try:
        lamina_faces = _polyinfo_components(
            cmds.polyInfo(obj, laminaFaces=True),
        )
    except Exception:
        lamina_faces = []

    # Transform-level checks for game export
    scale = cmds.xform(obj, query=True, relative=True, scale=True)

    negative_scale = any(float(v) < 0.0 for v in scale)
    non_frozen_scale = any(abs(float(v) - 1.0) > 0.0001 for v in scale)

    # UV-level checks, aggregated across the object's shapes
    missing_uvs = False
    uvs_out_of_range = False
    flipped_uv_faces = []
    zero_uv_faces = []

    if include_uv_checks:
        for shape in get_mesh_shapes(obj):
            uv_result = check_shape_uvs(shape)

            missing_uvs = missing_uvs or bool(uv_result["missing_uvs"])
            uvs_out_of_range = (
                uvs_out_of_range or bool(uv_result["uvs_out_of_range"])
            )
            flipped_uv_faces.extend(uv_result["flipped_uv_faces"])
            zero_uv_faces.extend(uv_result["zero_uv_faces"])

    return {
        "object": obj,
        "non_manifold_edges": non_manifold_edges,
        "non_manifold_vertices": non_manifold_vertices,
        "lamina_faces": lamina_faces,
        "negative_scale": negative_scale,
        "non_frozen_scale": non_frozen_scale,
        "missing_uvs": missing_uvs,
        "uvs_out_of_range": uvs_out_of_range,
        "flipped_uv_faces": flipped_uv_faces,
        "zero_uv_faces": zero_uv_faces,
        "issue_count": (
            len(non_manifold_edges) + len(non_manifold_vertices) + len(lamina_faces)
            + int(negative_scale) + int(non_frozen_scale)
            + int(missing_uvs) + int(uvs_out_of_range)
            + len(flipped_uv_faces) + len(zero_uv_faces)
        ),
    }


def validate_selection(
        select_first_issue: bool = True,
        include_uv_checks: bool = True,
) -> Tuple[List[Dict[str, object]], str]:
    meshes = get_selected_mesh_transforms()

    if not meshes:
        raise RuntimeError("No mesh transforms selected")

    results = [
        check_mesh_validation(obj, include_uv_checks=include_uv_checks)
        for obj in meshes
    ]

    problem_results = [r for r in results if int(r["issue_count"]) > 0]

    if not problem_results:
        checked = "geometry and UV" if include_uv_checks else "geometry"

        return [], (
            "Clean: no {0} issues found on {1} object(s)".format(
                checked, len(meshes),
            )
        )

    lines = []

    for r in problem_results:
        flags = []

        if r["non_manifold_edges"]:
            flags.append(
                "non-manifold edges: {0}".format(len(r["non_manifold_edges"]))
            )

        if r["non_manifold_vertices"]:
            flags.append(
                "non-manifold verts: {0}".format(len(r["non_manifold_vertices"]))
            )

        if r["lamina_faces"]:
            flags.append(
                "lamina faces: {0}".format(len(r["lamina_faces"]))
            )

        if r["negative_scale"]:
            flags.append("negative scale")

        if r["non_frozen_scale"]:
            flags.append("non-frozen scale")

        if r.get("missing_uvs"):
            flags.append("missing UVs")

        if r.get("uvs_out_of_range"):
            flags.append("UVs outside 0-1")

        if r.get("flipped_uv_faces"):
            flags.append(
                "flipped UVs: {0}".format(len(r["flipped_uv_faces"]))
            )

        if r.get("zero_uv_faces"):
            flags.append(
                "zero-area UVs: {0}".format(len(r["zero_uv_faces"]))
            )

        lines.append(
            "{0}: {1}".format(_short_name(str(r["object"])), ", ".join(flags))
        )

    if select_first_issue:
        first = problem_results[0]

        components = []
        components.extend(first["non_manifold_edges"])
        components.extend(first["non_manifold_vertices"])
        components.extend(first["lamina_faces"])
        components.extend(first.get("flipped_uv_faces", []))
        components.extend(first.get("zero_uv_faces", []))

        if components:
            cmds.select(components, replace=True)

            try:
                cmds.selectMode(component=True)
            except Exception:
                pass

        else:
            cmds.select(first["object"], replace=True)

    return problem_results, " | ".join(lines)

# ---
# Combine / export
# ---


@undoable
def combine_selected_meshes() -> Optional[str]:
    meshes = get_selected_mesh_transforms()

    if len(meshes) < 2:
        raise RuntimeError("Select at least two mesh transforms to combine")

    result = cmds.polyUnite(
        meshes, constructionHistory=False, mergeUVSets=True,
    )

    if not result:
        return None

    combined = result[0]

    cmds.delete(combined, constructionHistory=True)
    cmds.select(combined, replace=True)

    return _long_name(combined)


def _ensure_fbx_plugin() -> None:
    if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
        cmds.loadPlugin("fbxmaya")


def _ensure_obj_plugin() -> None:
    # objExport ships with Maya, but studios often disable autoload on startup.
    if not cmds.pluginInfo("objExport", query=True, loaded=True):
        cmds.loadPlugin("objExport")


def _ensure_usd_plugin() -> None:
    candidates = ("mayaUsdPlugin", "mayaUsdPlugin.mll", "mayaUsdPlugin.so", "mayaUsdPlugin.bundle")
    for plugin in candidates:
        try:
            if cmds.pluginInfo(plugin, query=True, loaded=True):
                return
        except Exception:
            continue
    last_error = None
    for plugin in candidates:
        try:
            cmds.loadPlugin(plugin)
            return
        except Exception as exc:
            last_error = exc
    raise RuntimeError("Maya USD plugin is unavailable: {0}".format(last_error or "not installed"))


def _mel_string(path: str) -> str:
    return path.replace("\\", "/").replace('"', '\\"')


def _validate_selected_for_export(
        meshes: Sequence[str], validate_before_export: bool, block_on_validation_errors: bool,
        include_uv_checks: bool, asset_doctor_options: Optional[Dict[str, object]],
) -> None:
    if not validate_before_export:
        return
    if asset_doctor_options is not None:
        reports = [maya_asset_doctor_analyze_object(obj, **asset_doctor_options) for obj in meshes]
        blocking = []
        warnings = []
        for report in reports:
            for issue in report.sorted_issues():
                if issue.category == "UV" and not include_uv_checks:
                    continue
                line = "{0}: {1}".format(report.object_name, issue.title)
                if issue.severity == "ERROR":
                    blocking.append(line)
                elif issue.severity == "WARNING":
                    warnings.append(line)
        if blocking and block_on_validation_errors:
            raise RuntimeError("Export blocked by Asset Doctor: {0}".format(" | ".join(blocking)))
        if blocking or warnings:
            cmds.warning("Asset Doctor export warnings: {0}".format(" | ".join(blocking + warnings)))
        return

    problems, message = validate_selection(
        select_first_issue=False,
        include_uv_checks=include_uv_checks,
    )
    if problems and block_on_validation_errors:
        raise RuntimeError("Export blocked by validation: {0}".format(message))


def export_selected_file(
        export_folder: str,
        export_name: str,
        validate_before_export: bool = True,
        block_on_validation_errors: bool = True,
        include_uv_checks: bool = True,
        asset_doctor_options: Optional[Dict[str, object]] = None,
        export_profile_id: str = "GENERIC_FBX",
) -> str:
    meshes = get_selected_mesh_transforms()
    if not meshes:
        raise RuntimeError("No mesh transforms selected for export")

    export_folder = os.path.abspath(os.path.expanduser(export_folder))
    if not os.path.isdir(export_folder):
        raise RuntimeError("Invalid export folder: {0}".format(export_folder))

    _validate_selected_for_export(
        meshes, validate_before_export, block_on_validation_errors,
        include_uv_checks, asset_doctor_options,
    )

    export_profile = get_export_profile(export_profile_id)
    file_format = export_format(export_profile_id)
    filepath = os.path.normpath(os.path.join(export_folder, export_name + export_extension(export_profile_id)))

    heatmapped = [obj for obj in meshes if maya_has_td_heatmap(obj)]
    for obj in heatmapped:
        maya_clear_td_heatmap(obj)

    try:
        if file_format == "FBX":
            _ensure_fbx_plugin()
            try:
                mel.eval("FBXResetExport")
            except Exception:
                pass

            fbx_commands = [
                "FBXExportSmoothingGroups -v {0}".format("true" if export_profile.get("export_smoothing", True) else "false"),
                "FBXExportSmoothMesh -v false",
                "FBXExportTangents -v {0}".format("true" if export_profile.get("export_tangents", True) else "false"),
                "FBXExportTriangulate -v {0}".format("true" if export_profile.get("triangulate", False) else "false"),
                "FBXExportInputConnections -v false",
                "FBXExportConstraints -v false",
                "FBXExportCameras -v false",
                "FBXExportLights -v false",
                "FBXExportEmbeddedTextures -v {0}".format("true" if export_profile.get("embed_textures", False) else "false"),
                "FBXExportSkins -v {0}".format("true" if export_profile.get("include_armatures", True) else "false"),
                "FBXExportShapes -v {0}".format("true" if export_profile.get("include_armatures", True) else "false"),
                "FBXExportBakeComplexAnimation -v {0}".format("true" if export_profile.get("include_animation", False) else "false"),
            ]
            failed_settings = []
            for command in fbx_commands:
                try:
                    mel.eval(command)
                except Exception as exc:
                    failed_settings.append("{0} ({1})".format(command.split(" -v")[0], exc))
            if failed_settings:
                cmds.warning("JAM TA export preset could not apply: {0}".format("; ".join(failed_settings)))
            mel.eval('FBXExport -f "{0}" -s'.format(_mel_string(filepath)))

        elif file_format == "OBJ":
            _ensure_obj_plugin()
            cmds.file(
                filepath,
                force=True,
                options="groups=1;ptgroups=1;materials=1;smoothing=1;normals=1",
                type="OBJexport",
                exportSelected=True,
                preserveReferences=True,
            )

        elif file_format == "USD":
            _ensure_usd_plugin()
            if hasattr(cmds, "mayaUSDExport"):
                # MayaUSD changes flags occasionally; keep this small and explicit.
                cmds.mayaUSDExport(file=filepath, selection=True)
            else:
                cmds.file(
                    filepath,
                    force=True,
                    options="",
                    type="USD Export",
                    exportSelected=True,
                    preserveReferences=True,
                )

        elif file_format in {"GLB", "GLTF"}:
            raise RuntimeError(
                "Maya has no bundled glTF exporter in JAM. Install a studio glTF plugin or use Blender for this preset."
            )
        else:
            raise RuntimeError("Unsupported export format: {0}".format(file_format))
    finally:
        options = asset_doctor_options or {}
        texture_size = int(options.get("td_texture_size", 2048))
        target_px_per_m = float(options.get("td_target_px_per_m", 1024.0))
        for obj in heatmapped:
            maya_apply_td_heatmap(obj, texture_size, target_px_per_m)

    return filepath


def export_object_file(
        obj: str,
        export_folder: str,
        export_name: str,
        move_to_origin: bool = True,
        validate_before_export: bool = True,
        block_on_validation_errors: bool = True,
        include_uv_checks: bool = True,
        asset_doctor_options: Optional[Dict[str, object]] = None,
        export_profile_id: str = "GENERIC_FBX",
) -> str:
    """
    Export a single object to its own file, optionally moving its
    pivot to the world origin for the export and restoring the original
    position afterwards (the standard game-art batch export workflow).
    """

    original_selection = cmds.ls(selection=True, long=True) or []

    cmds.select(obj, replace=True)

    original_translation = None

    try:
        if move_to_origin:
            pivot = cmds.xform(
                obj, query=True, worldSpace=True, rotatePivot=True,
            )
            original_translation = cmds.xform(
                obj, query=True, worldSpace=True, translation=True,
            )

            cmds.xform(
                obj,
                worldSpace=True,
                translation=(
                    original_translation[0] - pivot[0],
                    original_translation[1] - pivot[1],
                    original_translation[2] - pivot[2],
                ),
            )

        return export_selected_file(
            export_folder,
            export_name,
            validate_before_export=validate_before_export,
            block_on_validation_errors=block_on_validation_errors,
            include_uv_checks=include_uv_checks,
            asset_doctor_options=asset_doctor_options,
            export_profile_id=export_profile_id,
        )

    finally:
        if move_to_origin and original_translation is not None:
            cmds.xform(
                obj, worldSpace=True, translation=original_translation,
            )

        if original_selection:
            try:
                cmds.select(original_selection, replace=True)
            except Exception:
                pass


# ---
# Texel density
# ---

def _object_area_totals(obj: str) -> Tuple[float, float]:
    """
    Total world-space surface area and UV area across an object's
    mesh shapes (default UV set).
    """

    total_world = 0.0
    total_uv = 0.0

    for shape in get_mesh_shapes(obj):
        try:
            dag = _get_dag_path(shape)
            iterator = om.MItMeshPolygon(dag)
        except Exception:
            continue

        while not iterator.isDone():
            try:
                total_world += iterator.getArea(om.MSpace.kWorld)

                if iterator.hasUVs():
                    us, vs = iterator.getUVs()
                    total_uv += abs(_uv_signed_area(us, vs))
            except Exception:
                pass

            iterator.next()

    return total_world, total_uv


def maya_density_to_display(px_per_m: float, unit: str = "px/cm") -> float:
    return float(px_per_m) / 100.0 if unit == "px/cm" else float(px_per_m)


def maya_density_from_display(value: float, unit: str = "px/cm") -> float:
    return float(value) * 100.0 if unit == "px/cm" else float(value)


def get_object_texel_density(obj: str, texture_size: int, unit: str = "px/cm") -> Optional[float]:
    analysis = maya_texel_density_analysis(obj, texture_size=texture_size)
    if not analysis.get("sample_count"):
        return None
    return maya_density_to_display(float(analysis["density_px_per_m"]), unit)


def get_selection_texel_density(texture_size: int, unit: str = "px/cm") -> Optional[float]:
    samples = []
    for obj in get_selected_mesh_transforms():
        samples.extend(maya_face_texel_samples(obj))
    analysis = summarize_texel_density(samples, texture_size=texture_size)
    if not analysis.get("sample_count"):
        return None
    return maya_density_to_display(float(analysis["density_px_per_m"]), unit)


@undoable
def set_selection_texel_density(
        target_density: float,
        texture_size: int,
        unit: str = "px/cm",
) -> int:
    """
    Uniformly scale each selected object's UVs (around their UV
    bounding-box center) so its texel density matches the target.
    """

    meshes = get_selected_mesh_transforms()

    if not meshes:
        raise RuntimeError("No mesh transforms selected")

    if target_density <= 0.0:
        raise RuntimeError("Target texel density must be greater than zero")

    adjusted = 0

    for obj in meshes:
        current = get_object_texel_density(obj, texture_size, unit=unit)

        if not current or current <= 0.0:
            _status(
                "Skipped {0}: no UV or surface area".format(_short_name(obj)),
                warning=True,
            )
            continue

        factor = float(target_density) / current

        if abs(factor - 1.0) < 0.0001:
            continue

        for shape in get_mesh_shapes(obj):
            try:
                bbox2d = cmds.polyEvaluate(shape, boundingBox2d=True)
                (u_min, u_max), (v_min, v_max) = bbox2d
            except Exception:
                continue

            cmds.polyEditUV(
                shape + ".map[*]",
                pivotU=(u_min + u_max) * 0.5,
                pivotV=(v_min + v_max) * 0.5,
                scaleU=factor,
                scaleV=factor,
            )

        adjusted += 1

    return adjusted


# ---
# Rigging helpers
# ---

@undoable
def create_ik_with_pole_vector(pole_distance: float = 1.0) -> Tuple[str, str]:
    """
    Create an RP-solver IK handle between the first and last selected
    joints, with a pole vector locator placed on the chain's bend plane.

    Placement math: the middle joint is projected onto the start->end
    axis; the rejection vector (mid - projection) points away from the
    chain on its bend plane, which is exactly where the pole vector
    belongs to preserve the current bend direction.
    """

    joints = cmds.ls(selection=True, type="joint", long=True) or []

    if len(joints) < 2:
        raise RuntimeError(
            "Select the start joint and the end joint of the chain"
        )

    start = joints[0]
    end = joints[-1]

    # Walk up from the end joint to confirm it descends from the start
    chain = [end]
    current = end

    while True:
        parents = cmds.listRelatives(
            current, parent=True, fullPath=True, type="joint",
        ) or []

        if not parents:
            break

        current = parents[0]
        chain.append(current)

        if current == start:
            break

    if chain[-1] != start:
        raise RuntimeError(
            "The last selected joint must be a descendant of the first"
        )

    chain.reverse()  # start -> end

    if len(chain) < 3:
        raise RuntimeError(
            "The chain needs at least one middle joint for a pole vector"
        )

    positions = [
        om.MVector(
            *cmds.xform(j, query=True, worldSpace=True, rotatePivot=True)
        )
        for j in chain
    ]

    start_pos = positions[0]
    end_pos = positions[-1]
    mid_pos = positions[len(positions) // 2]

    axis = end_pos - start_pos
    to_mid = mid_pos - start_pos

    if axis.length() < 1e-6:
        raise RuntimeError("Start and end joints are at the same position")

    axis_normal = axis.normal()

    # Vector rejection: component of to_mid perpendicular to the chain axis
    projection = axis_normal * (to_mid * axis_normal)
    pole_direction = to_mid - projection

    chain_length = sum(
        (positions[i + 1] - positions[i]).length()
        for i in range(len(positions) - 1)
    )

    if pole_direction.length() < 1e-6:
        # Perfectly straight chain: any perpendicular works, so warn
        fallback = om.MVector(0.0, 0.0, 1.0)

        if abs(axis_normal * fallback) > 0.999:
            fallback = om.MVector(0.0, 1.0, 0.0)

        pole_direction = axis_normal ^ fallback

        _status(
            "Chain is straight; pole vector direction is arbitrary",
            warning=True,
        )

    pole_position = (
        mid_pos
        + pole_direction.normal() * (chain_length * 0.5 * float(pole_distance))
    )

    base_name = _strip_namespace(_short_name(chain[0]))

    handle, _effector = cmds.ikHandle(
        startJoint=chain[0],
        endEffector=chain[-1],
        solver="ikRPsolver",
        name="IK_{0}".format(base_name),
    )

    locator = cmds.spaceLocator(name="PV_{0}".format(base_name))[0]

    cmds.xform(
        locator,
        worldSpace=True,
        translation=(pole_position.x, pole_position.y, pole_position.z),
    )

    cmds.poleVectorConstraint(locator, handle)
    cmds.select(handle, replace=True)

    return handle, locator


@undoable
def create_muscle_helper(
        rotate_axis: str = "Z",
        max_angle: float = 90.0,
        bulge_scale: float = 1.4,
) -> str:
    """
    Create a pose-driven 'muscle' joint between two selected joints --
    the classic bicep setup.

    Select the upper joint (e.g. shoulder), then the bend joint
    (e.g. elbow). A helper joint is parented under the upper joint at
    the muscle position, and set-driven keys make it bulge as the bend
    joint rotates. Keys are set at both +max_angle and -max_angle so
    the rig's bend direction does not matter.

    Assumes the bend joint's rotation is zero in the rest pose. To see
    the bulge on a mesh, add the helper joint to the skinCluster and
    paint its weights over the muscle area.
    """

    joints = cmds.ls(selection=True, type="joint", long=True) or []

    if len(joints) != 2:
        raise RuntimeError(
            "Select exactly two joints: the upper joint, then the bend joint"
        )

    upper, bend = joints
    axis = str(rotate_axis).upper()

    if axis not in ("X", "Y", "Z"):
        raise RuntimeError("Rotate axis must be X, Y or Z")

    upper_pos = om.MVector(
        *cmds.xform(upper, query=True, worldSpace=True, rotatePivot=True)
    )
    bend_pos = om.MVector(
        *cmds.xform(bend, query=True, worldSpace=True, rotatePivot=True)
    )

    muscle_pos = (upper_pos + bend_pos) * 0.5

    base_name = _strip_namespace(_short_name(upper))

    cmds.select(clear=True)

    helper = cmds.joint(name="MUSCLE_{0}".format(base_name))
    helper = cmds.parent(helper, upper)[0]
    helper = _long_name(helper)

    cmds.xform(
        helper,
        worldSpace=True,
        translation=(muscle_pos.x, muscle_pos.y, muscle_pos.z),
    )

    driver_attribute = "{0}.rotate{1}".format(bend, axis)

    for scale_axis in ("X", "Y", "Z"):
        driven_attribute = "{0}.scale{1}".format(helper, scale_axis)

        cmds.setDrivenKeyframe(
            driven_attribute,
            currentDriver=driver_attribute,
            driverValue=0.0,
            value=1.0,
        )
        cmds.setDrivenKeyframe(
            driven_attribute,
            currentDriver=driver_attribute,
            driverValue=float(max_angle),
            value=float(bulge_scale),
        )
        cmds.setDrivenKeyframe(
            driven_attribute,
            currentDriver=driver_attribute,
            driverValue=-float(max_angle),
            value=float(bulge_scale),
        )

    cmds.select(helper, replace=True)

    return helper



def maya_face_texel_samples(obj: str) -> List[Dict[str, object]]:
    """Collect per-face geometric area in m² and default-set UV area."""
    samples = []
    for shape in get_mesh_shapes(obj):
        try:
            dag = _get_dag_path(shape)
            iterator = om.MItMeshPolygon(dag)
        except Exception:
            continue

        while not iterator.isDone():
            try:
                # Maya API's internal linear unit is centimetres, so world-area
                # values are cm² regardless of the visible UI-unit preference.
                world_area_m2 = float(iterator.getArea(om.MSpace.kWorld)) * 0.0001
                uv_area = 0.0
                if iterator.hasUVs():
                    us, vs = iterator.getUVs()
                    uv_area = abs(_uv_signed_area(us, vs))
                samples.append({
                    "component": "{0}.f[{1}]".format(shape, iterator.index()),
                    "world_area_m2": world_area_m2,
                    "uv_area": float(uv_area),
                })
            except Exception:
                pass
            iterator.next()
    return samples


def maya_texel_density_analysis(
        obj: str,
        texture_size: int,
        target_px_per_m: float = 0.0,
        tolerance_percent: float = 15.0,
) -> Dict[str, object]:
    return summarize_texel_density(
        maya_face_texel_samples(obj),
        texture_size=texture_size,
        target_px_per_m=target_px_per_m,
        tolerance_percent=tolerance_percent,
    )


def maya_render_split_components(obj: str, cause: str) -> List[str]:
    """Return Maya edge components that visibly create one split category."""
    result = []
    for shape in get_mesh_shapes(obj):
        try:
            dag = _get_dag_path(shape)
            mesh_fn = om.MFnMesh(dag)
            edge_it = om.MItMeshEdge(dag)
            try:
                uv_sets = list(mesh_fn.getUVSetNames())
            except Exception:
                uv_sets = []
            shader_indices = []
            if cause == "MATERIAL":
                try:
                    _shaders, shader_indices = mesh_fn.getConnectedShaders(dag.instanceNumber())
                except Exception:
                    shader_indices = []

            while not edge_it.isDone():
                try:
                    face_ids = list(edge_it.getConnectedFaces())
                except Exception:
                    face_ids = []
                if len(face_ids) == 2:
                    face_a, face_b = int(face_ids[0]), int(face_ids[1])
                    split = False

                    if cause == "MATERIAL":
                        if face_a < len(shader_indices) and face_b < len(shader_indices):
                            split = int(shader_indices[face_a]) != int(shader_indices[face_b])
                    else:
                        for vertex_id in (int(edge_it.vertexId(0)), int(edge_it.vertexId(1))):
                            if cause == "NORMAL":
                                try:
                                    na = mesh_fn.getFaceVertexNormal(face_a, vertex_id, om.MSpace.kWorld)
                                    nb = mesh_fn.getFaceVertexNormal(face_b, vertex_id, om.MSpace.kWorld)
                                    if (na - nb).length() > 1e-5:
                                        split = True
                                        break
                                except Exception:
                                    pass
                            elif cause == "UV":
                                for uv_set in uv_sets:
                                    try:
                                        uva = mesh_fn.getPolygonUV(face_a, vertex_id, uv_set)
                                        uvb = mesh_fn.getPolygonUV(face_b, vertex_id, uv_set)
                                        if abs(float(uva[0]) - float(uvb[0])) > 1e-6 or abs(float(uva[1]) - float(uvb[1])) > 1e-6:
                                            split = True
                                            break
                                    except Exception:
                                        # Missing face-vertex UV data itself is a discontinuity.
                                        split = True
                                        break
                                if split:
                                    break

                    if split:
                        result.append("{0}.e[{1}]".format(shape, edge_it.index()))
                edge_it.next()
        except Exception:
            continue
    return _unique_preserve_order(result)


def maya_has_td_heatmap(obj: str) -> bool:
    for shape in get_mesh_shapes(obj):
        try:
            sets = cmds.polyColorSet(shape, query=True, allColorSets=True) or []
            if MAYA_TD_HEATMAP_NAME in sets:
                return True
        except Exception:
            pass
    return False


def maya_clear_td_heatmap(obj: str) -> int:
    cleared = 0
    for shape in get_mesh_shapes(obj):
        try:
            sets = cmds.polyColorSet(shape, query=True, allColorSets=True) or []
            if MAYA_TD_HEATMAP_NAME not in sets:
                continue
            cmds.polyColorSet(shape, delete=True, colorSet=MAYA_TD_HEATMAP_NAME)
            remaining = cmds.polyColorSet(shape, query=True, allColorSets=True) or []
            if not remaining and cmds.attributeQuery("displayColors", node=shape, exists=True):
                cmds.setAttr(shape + ".displayColors", 0)
            cleared += 1
        except Exception:
            pass
    return cleared


def maya_apply_td_heatmap(
        obj: str,
        texture_size: int,
        target_px_per_m: float,
) -> int:
    analysis = maya_texel_density_analysis(
        obj,
        texture_size=texture_size,
        target_px_per_m=target_px_per_m,
    )
    samples = analysis.get("samples", [])
    if not samples:
        return 0

    target = float(target_px_per_m or analysis.get("median_px_per_m", 0.0) or 1.0)
    grouped = {}
    for sample in samples:
        component = str(sample.get("component", ""))
        shape = component.split(".f[")[0] if ".f[" in component else ""
        if shape:
            grouped.setdefault(shape, []).append(sample)

    colored = 0
    for shape, shape_samples in grouped.items():
        try:
            sets = cmds.polyColorSet(shape, query=True, allColorSets=True) or []
            if MAYA_TD_HEATMAP_NAME in sets:
                cmds.polyColorSet(shape, delete=True, colorSet=MAYA_TD_HEATMAP_NAME)
            cmds.polyColorSet(
                shape, create=True, colorSet=MAYA_TD_HEATMAP_NAME, representation="RGB"
            )
            cmds.polyColorSet(
                shape, currentColorSet=True, colorSet=MAYA_TD_HEATMAP_NAME
            )
            for sample in shape_samples:
                rgb = texel_density_color(sample.get("density_px_per_m", 0.0), target)
                cmds.polyColorPerVertex(
                    sample["component"],
                    rgb=(float(rgb[0]), float(rgb[1]), float(rgb[2])),
                    colorDisplayOption=True,
                )
                colored += 1
            if cmds.attributeQuery("displayColors", node=shape, exists=True):
                cmds.setAttr(shape + ".displayColors", 1)
        except Exception:
            continue
    return colored


# ---
# Asset Doctor
# ---

def maya_topology_details(obj: str) -> Dict[str, List[str]]:
    details = {
        "wire_edges": [],
        "boundary_edges": [],
        "multiface_edges": [],
        "zero_area_faces": [],
    }

    for shape in get_mesh_shapes(obj):
        dag = _get_dag_path(shape)

        try:
            edge_it = om.MItMeshEdge(dag)
            while not edge_it.isDone():
                edge_index = edge_it.index()
                face_count = edge_it.numConnectedFaces()
                component = "{0}.e[{1}]".format(shape, edge_index)
                if face_count == 0:
                    details["wire_edges"].append(component)
                elif face_count == 1:
                    details["boundary_edges"].append(component)
                elif face_count > 2:
                    details["multiface_edges"].append(component)
                edge_it.next()
        except Exception:
            pass

        try:
            face_it = om.MItMeshPolygon(dag)
            while not face_it.isDone():
                try:
                    area = float(face_it.getArea(om.MSpace.kObject))
                except Exception:
                    area = 1.0
                if area <= 1e-12:
                    details["zero_area_faces"].append(
                        "{0}.f[{1}]".format(shape, face_it.index())
                    )
                face_it.next()
        except Exception:
            pass

    return details


def maya_material_slot_count(obj: str) -> int:
    shaders = set()
    for shape in get_mesh_shapes(obj):
        try:
            dag = _get_dag_path(shape)
            mesh_fn = om.MFnMesh(dag)
            connected, _indices = mesh_fn.getConnectedShaders(dag.instanceNumber())
            for shader in connected:
                try:
                    shaders.add(om.MFnDependencyNode(shader).name())
                except Exception:
                    pass
        except Exception:
            pass
    return len(shaders)


def maya_collect_material_inventory(obj: str) -> List[Dict[str, object]]:
    """Collect shader/file-node facts and leave policy to the shared core."""
    materials: List[Dict[str, object]] = []
    seen_shaders = set()

    for shape in get_mesh_shapes(obj):
        shading_engines = cmds.listConnections(shape, source=False, destination=True, type="shadingEngine") or []
        for shading_engine in _unique_preserve_order(shading_engines):
            shaders = cmds.listConnections(
                shading_engine + ".surfaceShader",
                source=True,
                destination=False,
            ) or []
            for shader in shaders:
                if shader in seen_shaders:
                    continue
                seen_shaders.add(shader)

                textures = []
                history = cmds.listHistory(shader, pruneDagObjects=True) or []
                file_nodes = cmds.ls(history, type="file") or []
                for file_node in _unique_preserve_order(file_nodes):
                    path = ""
                    color_space = ""
                    width = 0
                    height = 0
                    uv_tiling_mode = 0
                    try:
                        path = str(cmds.getAttr(file_node + ".fileTextureName") or "")
                    except Exception:
                        pass
                    try:
                        color_space = str(cmds.getAttr(file_node + ".colorSpace") or "")
                    except Exception:
                        pass
                    try:
                        width = int(cmds.getAttr(file_node + ".outSizeX") or 0)
                        height = int(cmds.getAttr(file_node + ".outSizeY") or 0)
                    except Exception:
                        pass
                    try:
                        uv_tiling_mode = int(cmds.getAttr(file_node + ".uvTilingMode") or 0)
                    except Exception:
                        pass

                    downstream_plugs = cmds.listConnections(
                        file_node,
                        source=False,
                        destination=True,
                        plugs=True,
                    ) or []
                    hint = " ".join([file_node, os.path.basename(path)] + downstream_plugs)

                    tiled = uv_tiling_mode != 0 or "<UDIM>" in path.upper()
                    tile_count = 1
                    exists = os.path.isfile(path) if path else False
                    if tiled and path:
                        pattern = path.replace("<UDIM>", "1???").replace("<udim>", "1???")
                        if pattern == path:
                            pattern = re.sub(r"(?<!\d)1\d{3}(?!\d)", "1???", path, count=1)
                        matches = glob.glob(pattern)
                        tile_count = max(1, len(matches))
                        exists = bool(matches) or exists

                    textures.append({
                        "name": _short_name(file_node),
                        "path": path,
                        "width": width,
                        "height": height,
                        "channels": 4,
                        "bits_per_channel": 8,
                        "color_space": color_space,
                        "semantic": infer_texture_semantic(hint, path),
                        "semantic_hint": hint,
                        "exists": exists,
                        "used": bool(downstream_plugs),
                        "is_udim": tiled,
                        "udim_tiles": tile_count,
                        "source": "file",
                    })

                materials.append({
                    "name": _short_name(shader),
                    "shading_engine": _short_name(shading_engine),
                    "textures": textures,
                })

    return materials


def maya_material_texture_analysis(obj: str, profile: Dict[str, object]) -> Dict[str, object]:
    return analyze_material_inventory(
        maya_collect_material_inventory(obj),
        profile=profile,
        object_name=_short_name(obj),
    )


def maya_color_channel_count(obj: str) -> int:
    color_sets = set()
    for shape in get_mesh_shapes(obj):
        try:
            for name in cmds.polyColorSet(shape, query=True, allColorSets=True) or []:
                if name != MAYA_TD_HEATMAP_NAME:
                    color_sets.add(name)
        except Exception:
            pass
    return len(color_sets)


def maya_can_freeze_scale_safely(obj: str) -> bool:
    try:
        scale = cmds.xform(obj, query=True, relative=True, scale=True)
        if any(float(value) <= 0.0 for value in scale):
            return False

        history = cmds.listHistory(obj, pruneDagObjects=True) or []
        if cmds.ls(history, type="skinCluster"):
            return False
        if cmds.ls(history, type="blendShape"):
            return False

        parents = cmds.listRelatives(obj, parent=True, fullPath=True) or []
        if parents and cmds.nodeType(parents[0]) == "joint":
            return False

        if cmds.referenceQuery(obj, isNodeReferenced=True):
            return False
    except Exception:
        return False

    return True


def maya_skinning_details(
        obj: str,
        max_influences: Optional[int] = None,
        tolerance: float = 0.01,
) -> Dict[str, object]:
    result = {
        "has_skinning": False,
        "influence_count": 0,
        "max_influences_found": 0,
        "unweighted_vertices": [],
        "over_influence_vertices": [],
        "non_normalized_vertices": [],
    }

    history = cmds.listHistory(obj, pruneDagObjects=True) or []
    skin_clusters = cmds.ls(history, type="skinCluster") or []
    if not skin_clusters:
        return result

    result["has_skinning"] = True
    all_influences = set()

    for shape in get_mesh_shapes(obj):
        # Find the skin cluster that actually drives this shape.
        shape_history = cmds.listHistory(shape, pruneDagObjects=True) or []
        shape_clusters = cmds.ls(shape_history, type="skinCluster") or []
        if not shape_clusters:
            continue

        cluster = shape_clusters[0]
        try:
            selection = om.MSelectionList()
            selection.add(cluster)
            cluster_object = selection.getDependNode(0)
            skin_fn = oma.MFnSkinCluster(cluster_object)
            influences = skin_fn.influenceObjects()
            influence_count = len(influences)
            for influence in influences:
                all_influences.add(influence.fullPathName())

            dag = _get_dag_path(shape)
            vertex_count = _safe_int(cmds.polyEvaluate(shape, vertex=True), 0)
            if vertex_count <= 0 or influence_count <= 0:
                continue

            component_fn = om.MFnSingleIndexedComponent()
            component = component_fn.create(om.MFn.kMeshVertComponent)
            component_fn.addElements(list(range(vertex_count)))
            weights, returned_influence_count = skin_fn.getWeights(dag, component)
            influence_count = int(returned_influence_count)

            for vertex_index in range(vertex_count):
                start = vertex_index * influence_count
                values = [
                    float(weights[start + offset])
                    for offset in range(influence_count)
                    if float(weights[start + offset]) > 1e-8
                ]
                count = len(values)
                result["max_influences_found"] = max(
                    int(result["max_influences_found"]), count,
                )
                component_name = "{0}.vtx[{1}]".format(shape, vertex_index)

                if count == 0:
                    result["unweighted_vertices"].append(component_name)
                    continue

                if max_influences is not None and count > int(max_influences):
                    result["over_influence_vertices"].append(component_name)

                if abs(sum(values) - 1.0) > float(tolerance):
                    result["non_normalized_vertices"].append(component_name)
        except Exception:
            # Some construction-history arrangements expose the skinCluster in
            # listHistory but do not accept the visible shape DAG in getWeights.
            # Do not turn a host API edge case into a false validation error.
            continue

    result["influence_count"] = len(all_influences)
    return result


def maya_budget_metric(report: AssetReport, mode: str) -> int:
    return {
        "Estimated Game Verts": int(report.metrics.get("render_vertices", 0)),
        "Tris": int(report.metrics.get("triangles", 0)),
        "Faces": int(report.metrics.get("faces", 0)),
        "Verts": int(report.metrics.get("model_vertices", 0)),
    }.get(mode, 0)


def maya_asset_doctor_analyze_object(
        obj: str,
        profile_id: str = "GENERIC_GAME",
        budget_mode: str = "Estimated Game Verts",
        budget: int = 3000,
        margin_percent: float = 10.0,
        count_material_splits: bool = True,
        enforce_budget: bool = False,
        td_texture_size: int = 2048,
        td_target_px_per_m: float = 1024.0,
        td_tolerance_percent: float = 15.0,
        enforce_td_target: bool = False,
) -> AssetReport:
    profile = get_profile(profile_id)
    report = AssetReport(_short_name(obj), "Maya", profile["id"])
    validation = check_mesh_validation(obj, include_uv_checks=True)
    topology = maya_topology_details(obj)
    split = get_mesh_render_split_breakdown(
        obj,
        count_material_splits=count_material_splits,
    )
    skinning = maya_skinning_details(
        obj,
        max_influences=profile.get("max_skin_influences"),
        tolerance=profile.get("weight_sum_tolerance", 0.01),
    )

    material_slots = maya_material_slot_count(obj)
    material_analysis = maya_material_texture_analysis(obj, profile)
    td_analysis = maya_texel_density_analysis(
        obj,
        texture_size=td_texture_size,
        target_px_per_m=td_target_px_per_m,
        tolerance_percent=td_tolerance_percent,
    )
    uv_layout = maya_uv_layout_analysis(obj, td_texture_size, 0)
    island_td = summarize_island_texel_density(
        maya_face_texel_samples(obj),
        uv_layout.get("islands", []),
        td_texture_size,
    )
    lightmap_index = int(profile.get("lightmap_uv_channel", 1))
    memory = estimate_mesh_memory(
        int(split.get("render_vertices", 0)),
        int(split.get("triangle_count", 0)),
        uv_channels=len(list(split.get("uv_layer_names", []))),
        has_skinning=bool(skinning.get("has_skinning")),
        color_channels=maya_color_channel_count(obj),
    )
    model_vertices = get_mesh_metric(
        obj,
        "Verts",
        count_material_splits=count_material_splits,
    )
    uv_names = list(split.get("uv_layer_names", []))
    lightmap_layout = (
        maya_uv_layout_analysis(obj, td_texture_size, lightmap_index)
        if len(uv_names) > lightmap_index else None
    )
    modular = None
    if profile.get("modular_grid_cm"):
        modular = maya_modular_analysis(
            obj,
            grid_cm=float(profile.get("modular_grid_cm")),
            tolerance_cm=float(profile.get("modular_grid_tolerance_cm", 0.1)),
            check_position=bool(profile.get("modular_check_position", False)),
        )

    report.metrics.update({
        "model_vertices": int(model_vertices),
        "faces": int(split.get("face_count", 0)),
        "triangles": int(split.get("triangle_count", 0)),
        "render_vertices": int(split.get("render_vertices", 0)),
        "render_vertex_overhead": int(split.get("extra_vertices", 0)),
        "render_vertex_ratio": float(split.get("split_ratio", 0.0)),
        "material_slots": material_slots,
        "uv_channels": len(uv_names),
        "uv_channel_names": uv_names,
        "render_split_stages": split.get("stages", []),
        "has_skinning": bool(skinning.get("has_skinning")),
        "deform_bones": int(skinning.get("influence_count", 0)),
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
    report.metadata["maya_node"] = obj
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
            object_name=_short_name(obj),
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
            "{0} boundary edge(s). This can be intentional for shells, cards and modular seams.".format(len(topology["boundary_edges"])),
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

    if validation["non_manifold_edges"] or validation["non_manifold_vertices"]:
        components = list(validation["non_manifold_edges"]) + list(validation["non_manifold_vertices"])
        add(
            "geometry.non_manifold",
            "Non-manifold geometry",
            "Maya reports {0} non-manifold component(s).".format(len(components)),
            "Geometry",
            "ERROR",
            "EDGE",
            components,
        )

    if validation["lamina_faces"]:
        add(
            "geometry.lamina_faces",
            "Lamina faces",
            "{0} lamina face component(s) found.".format(len(validation["lamina_faces"])),
            "Geometry",
            profile.get("lamina_severity", "ERROR"),
            "FACE",
            validation["lamina_faces"],
        )

    if topology["zero_area_faces"]:
        add(
            "geometry.zero_area_faces",
            "Zero-area faces",
            "{0} face(s) have effectively zero geometric area.".format(len(topology["zero_area_faces"])),
            "Geometry",
            profile.get("zero_geo_area_severity", "ERROR"),
            "FACE",
            topology["zero_area_faces"],
        )

    scale = tuple(float(v) for v in cmds.xform(obj, query=True, relative=True, scale=True))
    if validation["negative_scale"]:
        add(
            "transform.negative_scale",
            "Negative scale",
            "Object scale is ({0:.4g}, {1:.4g}, {2:.4g}).".format(*scale),
            "Transform",
            profile.get("negative_scale_severity", "ERROR"),
        )
    elif validation["non_frozen_scale"]:
        safe_fix = "freeze_positive_scale" if maya_can_freeze_scale_safely(obj) else None
        add(
            "transform.non_unit_scale",
            "Scale not frozen",
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
                "World bound axis/axes {0} miss the {1:g} cm grid. This can be intentional while blocking out a kit.".format(
                    ", ".join(modular["position_error_axes"]), modular["grid_cm"]
                ),
                "Modular",
                profile.get("modular_position_severity", "INFO"),
                metadata={"axes": list(modular["position_error_axes"]), "grid_cm": modular["grid_cm"]},
            )

    if profile.get("require_uvs") and validation["missing_uvs"]:
        add(
            "uv.missing",
            "Missing UVs",
            "The selected profile requires at least one UV channel.",
            "UV",
            "ERROR",
        )

    if profile.get("require_uv_01") and validation["uvs_out_of_range"]:
        add(
            "uv.outside_01",
            "UVs outside 0-1",
            "This profile requires the default UV set to remain inside the 0-1 tile.",
            "UV",
            "ERROR",
        )

    if validation["flipped_uv_faces"]:
        add(
            "uv.flipped_faces",
            "Flipped UV winding",
            "{0} face(s) have negative signed UV area. Mirroring may be intentional.".format(len(validation["flipped_uv_faces"])),
            "UV",
            profile.get("flipped_uv_severity", "INFO"),
            "FACE",
            validation["flipped_uv_faces"],
        )

    if validation["zero_uv_faces"]:
        add(
            "uv.zero_area_faces",
            "Zero-area UV faces",
            "{0} face(s) collapse to zero area in the default UV set.".format(len(validation["zero_uv_faces"])),
            "UV",
            profile.get("zero_uv_area_severity", "ERROR"),
            "FACE",
            validation["zero_uv_faces"],
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
                "Profile audits UV channel {0} for lightmap suitability, but the mesh only has {1} channel(s).".format(lightmap_index, len(uv_names)),
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
                    "Closest lightmap islands are about {0:.2f}px apart at {1}px; profile target is {2:.2f}px.".format(measured_padding, td_texture_size, minimum_padding),
                    "UV", profile.get("lightmap_padding_severity", "INFO"),
                )

    if enforce_td_target and td_analysis.get("outlier_components"):
        outliers = list(td_analysis.get("outlier_components", []))
        add(
            "uv.texel_density_outliers",
            "Texel-density outliers",
            "{0} face(s) fall outside ±{1:.1f}% of the {2:.2f} px/cm target.".format(
                len(outliers), td_tolerance_percent, td_target_px_per_m / 100.0
            ),
            "UV",
            "WARNING",
            "FACE",
            outliers,
            metadata={
                "target_px_per_m": td_target_px_per_m,
                "tolerance_percent": td_tolerance_percent,
            },
        )

    if skinning.get("has_skinning"):
        if skinning["unweighted_vertices"]:
            add(
                "skinning.unweighted_vertices",
                "Unweighted vertices",
                "{0} vertex/vertices have no effective skin weight.".format(len(skinning["unweighted_vertices"])),
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
                "{0} vertex/vertices do not sum to 1 within tolerance.".format(
                    len(skinning["non_normalized_vertices"])
                ),
                "Skinning",
                profile.get("weight_normalization_severity", "WARNING"),
                "VERT",
                skinning["non_normalized_vertices"],
            )

    max_material_slots = profile.get("max_material_slots")
    if max_material_slots is not None and material_slots > int(max_material_slots):
        add(
            "performance.material_slots",
            "High material-section count",
            "{0} material section(s); profile target is at most {1}.".format(material_slots, max_material_slots),
            "Performance",
            "WARNING",
        )

    if enforce_budget:
        count = maya_budget_metric(report, budget_mode)
        budget = max(int(budget), 1)
        margin = int(budget * (float(margin_percent) / 100.0))
        warn_at = max(0, budget - margin)
        error_at = budget + margin

        if count > error_at:
            add(
                "performance.project_budget",
                "Project budget exceeded",
                "{0:,} {1}; configured upper limit with margin is {2:,}.".format(count, budget_mode.lower(), error_at),
                "Performance",
                "ERROR",
                metadata={"count": count, "limit": error_at, "mode": budget_mode},
            )
        elif count >= warn_at:
            add(
                "performance.project_budget_margin",
                "Near project budget",
                "{0:,} {1}; configured budget is {2:,}.".format(count, budget_mode.lower(), budget),
                "Performance",
                "WARNING",
                metadata={"count": count, "limit": budget, "mode": budget_mode},
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


def maya_analyze_selection(
        profile_id: str,
        budget_mode: str,
        budget: int,
        margin_percent: float,
        count_material_splits: bool,
        enforce_budget: bool = False,
        td_texture_size: int = 2048,
        td_target_px_per_m: float = 1024.0,
        td_tolerance_percent: float = 15.0,
        enforce_td_target: bool = False,
) -> List[AssetReport]:
    global MAYA_LAST_REPORTS
    MAYA_LAST_REPORTS = [
        maya_asset_doctor_analyze_object(
            obj,
            profile_id=profile_id,
            budget_mode=budget_mode,
            budget=budget,
            margin_percent=margin_percent,
            count_material_splits=count_material_splits,
            enforce_budget=enforce_budget,
            td_texture_size=td_texture_size,
            td_target_px_per_m=td_target_px_per_m,
            td_tolerance_percent=td_tolerance_percent,
            enforce_td_target=enforce_td_target,
        )
        for obj in get_selected_mesh_transforms()
    ]
    return MAYA_LAST_REPORTS


def maya_asset_doctor_summary(reports: Sequence[AssetReport]) -> str:
    if not reports:
        return "No mesh objects selected"
    return "{0} asset(s) · {1} error(s) · {2} warning(s) · {3} safe fix(es)".format(
        len(reports),
        sum(report.error_count for report in reports),
        sum(report.warning_count for report in reports),
        sum(report.safe_fix_count for report in reports),
    )


@undoable
def maya_apply_safe_fixes(reports: Sequence[AssetReport]) -> int:
    fixed = 0
    for report in reports:
        obj = report.metadata.get("maya_node")
        if not obj or not cmds.objExists(obj):
            continue
        for issue in report.sorted_issues():
            if issue.safe_fix_id == "freeze_positive_scale" and maya_can_freeze_scale_safely(obj):
                cmds.makeIdentity(
                    obj,
                    apply=True,
                    translate=False,
                    rotate=False,
                    scale=True,
                    normal=0,
                )
                fixed += 1
                break
    return fixed


def maya_select_report_issue(report: AssetReport, issue: ValidationIssue) -> bool:
    obj = report.metadata.get("maya_node")
    components = [component for component in issue.components if isinstance(component, str) and cmds.objExists(component)]
    if components:
        cmds.select(components, replace=True)
        try:
            cmds.selectMode(component=True)
        except Exception:
            pass
        return True
    if obj and cmds.objExists(obj):
        cmds.select(obj, replace=True)
        return True
    return False


def maya_format_asset_doctor_report(reports: Sequence[AssetReport], show_split_details: bool = True) -> str:
    if not reports:
        return "No Asset Doctor report yet."

    lines = [maya_asset_doctor_summary(reports), ""]
    for report in reports:
        metrics = report.metrics
        lines.append("{0} [{1}]".format(report.object_name, report.status))
        lines.append(
            "  {0:,} tris | {1:,} model verts | {2:,} render verts | {3} mats | {4} UVs".format(
                int(metrics.get("triangles", 0)),
                int(metrics.get("model_vertices", 0)),
                int(metrics.get("render_vertices", 0)),
                int(metrics.get("material_slots", 0)),
                int(metrics.get("uv_channels", 0)),
            )
        )
        td_value = float(metrics.get("texel_density_px_per_cm", 0.0))
        memory_bytes = int(metrics.get("mesh_buffer_bytes", 0))
        if td_value > 0.0 or memory_bytes > 0:
            lines.append(
                "  TD {0:.2f} px/cm | spread {1:.1f}% | mesh buffers ~{2}".format(
                    td_value,
                    float(metrics.get("texel_density_spread_percent", 0.0)),
                    format_bytes(memory_bytes),
                )
            )
        if int(metrics.get("uv_island_count", 0)):
            lines.append(
                "  UV islands {0} | util ~{1:.1f}% | overlap faces {2} | min pad ~{3:.1f}px".format(
                    int(metrics.get("uv_island_count", 0)),
                    float(metrics.get("uv_utilization_percent", 0.0)),
                    int(metrics.get("uv_overlap_faces", 0)),
                    float(metrics.get("uv_min_padding_px", 0.0)),
                )
            )
        texture_count = int(metrics.get("texture_count", 0))
        if texture_count or int(metrics.get("material_count", 0)):
            lines.append(
                "  Materials {0} | textures {1} | missing {2} | tex ref ~{3}".format(
                    int(metrics.get("material_count", 0)),
                    texture_count,
                    int(metrics.get("missing_texture_count", 0)),
                    format_bytes(int(metrics.get("texture_reference_bytes", 0))),
                )
            )
        if metrics.get("has_skinning"):
            lines.append(
                "  Skin: {0} influences | max {1}/vertex | {2} unweighted".format(
                    int(metrics.get("deform_bones", 0)),
                    int(metrics.get("max_skin_influences", 0)),
                    int(metrics.get("unweighted_vertices", 0)),
                )
            )
        if show_split_details:
            lines.append(
                "  Render split ratio: {0:.2f}x".format(float(metrics.get("render_vertex_ratio", 0.0)))
            )
            for stage in metrics.get("render_split_stages", []):
                added = int(stage.get("added", 0))
                if added:
                    share = float(stage.get("share_of_extra", 0.0)) * 100.0
                    lines.append("    {0}: +{1:,} ({2:.0f}% of extra)".format(stage.get("label", "Split"), added, share))

        for issue in report.sorted_issues():
            fix = " [safe fix]" if issue.is_safe_fixable else ""
            lines.append("  {0} | {1}: {2}{3}".format(issue.severity, issue.category, issue.title, fix))
            lines.append("    " + issue.message)
        if not report.issues:
            lines.append("  Clean")
        lines.append("")

    return "\n".join(lines).rstrip()

# ---
# Modular / attachment authoring
# ---

_MAYA_UNIT_TO_CM = {
    "mm": 0.1, "cm": 1.0, "m": 100.0, "km": 100000.0,
    "in": 2.54, "ft": 30.48, "yd": 91.44, "mi": 160934.4,
}


def maya_cm_per_unit() -> float:
    try:
        unit = str(cmds.currentUnit(query=True, linear=True) or "cm")
    except Exception:
        unit = "cm"
    return float(_MAYA_UNIT_TO_CM.get(unit, 1.0))


def maya_world_bounds(nodes: Sequence[str]) -> Tuple[Optional[Tuple[float, float, float]], Optional[Tuple[float, float, float]]]:
    nodes = [node for node in nodes if cmds.objExists(node)]
    if not nodes:
        return None, None
    bbox = cmds.exactWorldBoundingBox(nodes)
    return (float(bbox[0]), float(bbox[1]), float(bbox[2])), (float(bbox[3]), float(bbox[4]), float(bbox[5]))


def maya_modular_analysis(
        node: str, grid_cm: float = 100.0, tolerance_cm: float = 0.1, check_position: bool = True,
) -> Optional[Dict[str, object]]:
    minimum, maximum = maya_world_bounds([node])
    if minimum is None:
        return None
    factor = maya_cm_per_unit()
    return analyze_modular_bounds(
        tuple(v * factor for v in minimum),
        tuple(v * factor for v in maximum),
        grid_cm, tolerance_cm, check_position,
    )


def maya_attachment_location(nodes: Sequence[str], mode: str) -> Tuple[float, float, float]:
    nodes = [node for node in nodes if cmds.objExists(node)]
    active = nodes[-1] if nodes else None
    if mode == "BOUNDS_CENTER" or mode == "BOUNDS_BOTTOM":
        minimum, maximum = maya_world_bounds(nodes)
        if minimum is not None:
            return bounds_anchor(minimum, maximum, "BOTTOM" if mode == "BOUNDS_BOTTOM" else "CENTER")
    if mode == "WORLD_ORIGIN":
        return (0.0, 0.0, 0.0)
    if active:
        value = cmds.xform(active, query=True, worldSpace=True, rotatePivot=True)
        return (float(value[0]), float(value[1]), float(value[2]))
    return (0.0, 0.0, 0.0)


# ---
# Asset Sets
# ---

MAYA_ASSET_ROOT_ATTR = "jamAssetRoot"
MAYA_ASSET_ROLE_ATTR = "jamAssetRole"
MAYA_ASSET_LOD_ATTR = "jamLodLevel"
MAYA_ASSET_COLLISION_ATTR = "jamCollisionType"
MAYA_ASSET_EXPORT_ATTR = "jamExportEnabled"


def _maya_ensure_attr(node: str, name: str, kind: str, default=None) -> None:
    if cmds.attributeQuery(name, node=node, exists=True):
        return
    if kind == "string":
        cmds.addAttr(node, longName=name, dataType="string")
        if default is not None:
            cmds.setAttr("{0}.{1}".format(node, name), str(default), type="string")
    elif kind == "bool":
        cmds.addAttr(node, longName=name, attributeType="bool", defaultValue=bool(default))
    else:
        cmds.addAttr(node, longName=name, attributeType="long", defaultValue=int(default or 0))


def _maya_get_attr(node: str, name: str, default=None):
    if not cmds.attributeQuery(name, node=node, exists=True):
        return default
    try:
        value = cmds.getAttr("{0}.{1}".format(node, name))
        return default if value is None else value
    except Exception:
        return default


def maya_selected_asset_nodes() -> List[str]:
    nodes = []
    for node in cmds.ls(selection=True, long=True) or []:
        node_type = cmds.nodeType(node)
        if node_type in {"mesh", "nurbsSurface"}:
            parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
            if parents:
                node = parents[0]
        if cmds.nodeType(node) in {"transform", "joint"}:
            nodes.append(_long_name(node))
    return _unique_preserve_order(nodes)


def maya_asset_set_root_for_node(node: str) -> str:
    root = str(_maya_get_attr(node, MAYA_ASSET_ROOT_ATTR, "") or "").strip()
    return root or infer_asset_member(_short_name(node)).root_name


def maya_set_asset_membership(
        node: str,
        root_name: str,
        role: Optional[str] = None,
        lod_level: Optional[int] = None,
        collision_type: Optional[str] = None,
        export_enabled: bool = True,
) -> None:
    inferred = infer_asset_member(_short_name(node), root_name)
    role = role or inferred.role
    if cmds.nodeType(node) == "joint":
        role = ROLE_SKELETON
    elif not get_mesh_shapes(node) and role == ROLE_RENDER:
        role = ROLE_SOCKET

    _maya_ensure_attr(node, MAYA_ASSET_ROOT_ATTR, "string", root_name)
    _maya_ensure_attr(node, MAYA_ASSET_ROLE_ATTR, "string", role)
    _maya_ensure_attr(node, MAYA_ASSET_LOD_ATTR, "long", inferred.lod_level)
    _maya_ensure_attr(node, MAYA_ASSET_COLLISION_ATTR, "string", collision_type or inferred.collision_type)
    _maya_ensure_attr(node, MAYA_ASSET_EXPORT_ATTR, "bool", export_enabled)

    cmds.setAttr("{0}.{1}".format(node, MAYA_ASSET_ROOT_ATTR), str(root_name), type="string")
    cmds.setAttr("{0}.{1}".format(node, MAYA_ASSET_ROLE_ATTR), str(role), type="string")
    cmds.setAttr("{0}.{1}".format(node, MAYA_ASSET_LOD_ATTR), int(inferred.lod_level if lod_level is None else lod_level))
    cmds.setAttr("{0}.{1}".format(node, MAYA_ASSET_COLLISION_ATTR), str(collision_type if collision_type is not None else inferred.collision_type), type="string")
    cmds.setAttr("{0}.{1}".format(node, MAYA_ASSET_EXPORT_ATTR), bool(export_enabled))


def maya_get_asset_set_members(root_name: str, include_disabled: bool = True) -> List[str]:
    candidates = (cmds.ls(type="transform", long=True) or []) + (cmds.ls(type="joint", long=True) or [])
    members = []
    for node in _unique_preserve_order(candidates):
        if str(_maya_get_attr(node, MAYA_ASSET_ROOT_ATTR, "") or "") != root_name:
            continue
        if not include_disabled and not bool(_maya_get_attr(node, MAYA_ASSET_EXPORT_ATTR, True)):
            continue
        members.append(node)
    return members


def maya_collision_quality(node: str, epsilon: float = 1e-5) -> Tuple[Optional[bool], Optional[bool]]:
    shapes = get_mesh_shapes(node)
    if not shapes:
        return None, None
    try:
        dag = _get_dag_path(shapes[0])
        edge_it = om.MItMeshEdge(dag)
        closed = True
        while not edge_it.isDone():
            if len(edge_it.getConnectedFaces()) != 2:
                closed = False
                break
            edge_it.next()
        if not closed:
            return False, False

        fn = om.MFnMesh(dag)
        points = fn.getPoints(om.MSpace.kWorld)
        if not points:
            return True, None
        cx = sum(point.x for point in points) / len(points)
        cy = sum(point.y for point in points) / len(points)
        cz = sum(point.z for point in points) / len(points)
        centroid = om.MPoint(cx, cy, cz)

        poly_it = om.MItMeshPolygon(dag)
        convex = True
        while not poly_it.isDone():
            indices = poly_it.getVertices()
            if not indices:
                poly_it.next()
                continue
            normal = poly_it.getNormal(om.MSpace.kWorld)
            plane = points[indices[0]]
            center_vec = centroid - plane
            center_side = normal.x * center_vec.x + normal.y * center_vec.y + normal.z * center_vec.z
            for point in points:
                vec = point - plane
                side = normal.x * vec.x + normal.y * vec.y + normal.z * vec.z
                if center_side <= 0.0:
                    if side > epsilon:
                        convex = False
                        break
                elif side < -epsilon:
                    convex = False
                    break
            if not convex:
                break
            poly_it.next()
        return True, convex
    except Exception:
        return None, None


def maya_asset_member_spec(node: str, root_name: str = "") -> AssetMemberSpec:
    root = str(_maya_get_attr(node, MAYA_ASSET_ROOT_ATTR, root_name) or root_name or "")
    inferred = infer_asset_member(_short_name(node), root)
    role = str(_maya_get_attr(node, MAYA_ASSET_ROLE_ATTR, inferred.role) or inferred.role)
    lod_level = int(_maya_get_attr(node, MAYA_ASSET_LOD_ATTR, inferred.lod_level) or 0)
    collision_type = str(_maya_get_attr(node, MAYA_ASSET_COLLISION_ATTR, inferred.collision_type) or inferred.collision_type)
    export_enabled = bool(_maya_get_attr(node, MAYA_ASSET_EXPORT_ATTR, True))
    triangles = int(get_mesh_metric(node, "Tris")) if get_mesh_shapes(node) else 0
    is_closed = None
    is_convex = None
    if role == ROLE_COLLISION and collision_type == "CONVEX":
        is_closed, is_convex = maya_collision_quality(node)
    return AssetMemberSpec(
        name=_short_name(node),
        root_name=root or inferred.root_name,
        role=role,
        lod_level=lod_level,
        collision_type=collision_type,
        export_enabled=export_enabled,
        triangles=triangles,
        is_closed=is_closed,
        is_convex=is_convex,
        metadata={"node_type": cmds.nodeType(node), "long_name": node},
    )


def maya_build_asset_set_analysis(
        root_name: str,
        min_lod_reduction_percent: float = 20.0,
        require_contiguous_lods: bool = True,
        collision_triangle_budget: Optional[int] = None,
) -> Dict[str, object]:
    global MAYA_LAST_ASSET_SET
    members = maya_get_asset_set_members(root_name, include_disabled=True)
    specs = [maya_asset_member_spec(node, root_name) for node in members]
    analysis = analyze_asset_set(
        root_name,
        specs,
        min_lod_reduction_percent=min_lod_reduction_percent,
        require_contiguous_lods=require_contiguous_lods,
        collision_triangle_budget=collision_triangle_budget,
    )
    analysis["nodes"] = members
    MAYA_LAST_ASSET_SET = analysis
    return analysis


def maya_asset_set_primary(nodes: Sequence[str]) -> Optional[str]:
    for node in nodes:
        role = str(_maya_get_attr(node, MAYA_ASSET_ROLE_ATTR, "") or "")
        level = int(_maya_get_attr(node, MAYA_ASSET_LOD_ATTR, 0) or 0)
        if role in {ROLE_RENDER, ROLE_LOD} and level == 0 and get_mesh_shapes(node):
            return node
    return nodes[0] if nodes else None


def maya_asset_set_sidecar_data(analysis: Dict[str, object]) -> Dict[str, object]:
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


# ---
# UI
# ---


# Controls whose values persist between Maya sessions via optionVar.
# "allow_combine" is deliberately excluded: destructive toggles should
# always reset to off.
_PERSISTED_CONTROLS = (
    ("doctor_profile", "optionMenuGrp", "value"),
    ("project_profile_file", "textFieldButtonGrp", "text"),
    ("doctor_report_path", "textFieldButtonGrp", "text"),
    ("doctor_split_details", "checkBox", "value"),
    ("doctor_enforce_budget", "checkBox", "value"),
    ("doctor_enforce_td", "checkBox", "value"),
    ("mod_grid", "floatFieldGrp", "value1"),
    ("mod_tolerance", "floatFieldGrp", "value1"),
    ("mod_check_position", "checkBox", "value"),
    ("mod_match_x", "checkBox", "value"),
    ("mod_match_y", "checkBox", "value"),
    ("mod_match_z", "checkBox", "value"),
    ("mod_snap_x", "checkBox", "value"),
    ("mod_snap_y", "checkBox", "value"),
    ("mod_snap_z", "checkBox", "value"),
    ("mod_snap_mode", "optionMenuGrp", "value"),
    ("mod_align_anchor", "optionMenuGrp", "value"),
    ("attachment_role", "optionMenuGrp", "value"),
    ("attachment_label", "textFieldGrp", "text"),
    ("attachment_location", "optionMenuGrp", "value"),
    ("attachment_size", "floatFieldGrp", "value1"),
    ("attachment_parent", "checkBox", "value"),
    ("normalize_scale", "checkBox", "value"),
    ("normalize_rotation", "checkBox", "value"),
    ("normalize_negative", "checkBox", "value"),
    ("asset_root", "textFieldGrp", "text"),
    ("asset_role", "optionMenuGrp", "value"),
    ("asset_lod", "intFieldGrp", "value1"),
    ("asset_collision_type", "optionMenuGrp", "value"),
    ("asset_export", "checkBox", "value"),
    ("asset_lod_generate_level", "intFieldGrp", "value1"),
    ("asset_lod_generate_ratio", "floatFieldGrp", "value1"),
    ("asset_lod_min_reduction", "floatFieldGrp", "value1"),
    ("asset_lod_contiguous", "checkBox", "value"),
    ("asset_collision_budget_enabled", "checkBox", "value"),
    ("asset_collision_budget", "intFieldGrp", "value1"),
    ("asset_move_origin", "checkBox", "value"),
    ("rs_prefix", "textFieldGrp", "text"),
    ("rs_name", "textFieldGrp", "text"),
    ("budget_mode", "optionMenuGrp", "value"),
    ("budget", "intFieldGrp", "value1"),
    ("margin", "floatFieldGrp", "value1"),
    ("mat_splits", "checkBox", "value"),
    ("auto_prefix", "checkBox", "value"),
    ("lp_prefix", "textFieldGrp", "text"),
    ("hp_prefix", "textFieldGrp", "text"),
    ("select_issue", "checkBox", "value"),
    ("uv_checks", "checkBox", "value"),
    ("export_path", "textFieldButtonGrp", "text"),
    ("export_profile", "optionMenuGrp", "value"),
    ("validate_export", "checkBox", "value"),
    ("block_export", "checkBox", "value"),
    ("batch_mode", "checkBox", "value"),
    ("move_origin", "checkBox", "value"),
    ("write_sidecar", "checkBox", "value"),
    ("overwrite_existing", "checkBox", "value"),
    ("td_size", "intFieldGrp", "value1"),
    ("td_target", "floatFieldGrp", "value1"),
    ("td_unit", "optionMenuGrp", "value"),
    ("td_tolerance", "floatFieldGrp", "value1"),
    ("pole_mult", "floatFieldGrp", "value1"),
    ("muscle_axis", "optionMenuGrp", "value"),
    ("muscle_angle", "floatFieldGrp", "value1"),
    ("muscle_bulge", "floatFieldGrp", "value1"),
)


class JAMTAToolsUI(object):
    def __init__(self):
        _require_maya()
        self.controls: Dict[str, str] = {}

    def _load_project_profile_on_start(self) -> None:
        saved_profile_file = _load_option("project_profile_file", "")
        if saved_profile_file and os.path.isfile(saved_profile_file):
            try:
                load_profiles_json(saved_profile_file)
                _refresh_profile_catalog()
            except Exception:
                # A moved profile file should not make the UI disappear.
                pass

    def _build_content(self, parent: str, deletion_target: str) -> None:
        self.controls.clear()
        self._load_project_profile_on_start()

        scroll = cmds.scrollLayout(childResizable=True, parent=parent)
        root = cmds.columnLayout(
            adjustableColumn=True,
            rowSpacing=8,
            parent=scroll,
        )

        cmds.text(
            label="JAM TA Tools",
            align="center",
            height=28,
            font="boldLabelFont",
        )
        cmds.separator(style="in")

        self._build_asset_doctor_section(root)
        self._build_authoring_section(root)
        self._build_asset_set_section(root)
        self._build_rename_section(root)
        self._build_budget_section(root)
        self._build_pivot_section(root)
        self._build_validation_section(root)
        self._build_export_section(root)
        self._build_texel_density_section(root)
        self._build_rigging_section(root)
        self._build_status_section(root)

        self._apply_saved_settings()
        self.on_asset_doctor_profile_changed()

        cmds.scriptJob(
            uiDeleted=[deletion_target, self.save_settings],
            runOnce=True,
        )
        self.refresh_budget_marker()

    def show(self) -> None:
        """Open the floating tool window. ``show_docked`` is preferred in Maya 2025+."""
        if cmds.window(WINDOW_NAME, exists=True):
            cmds.deleteUI(WINDOW_NAME, window=True)

        window = cmds.window(
            WINDOW_NAME,
            title=WINDOW_TITLE,
            sizeable=True,
            widthHeight=(430, 780),
        )
        self._build_content(window, WINDOW_NAME)
        cmds.showWindow(window)

    def show_docked(self) -> None:
        """Open JAM in Maya's workspace docking system."""
        if cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True):
            cmds.deleteUI(WORKSPACE_CONTROL_NAME, control=True)

        # Maya may execute uiScript immediately *or* during workspace restore.
        # Both paths converge on the same builder to avoid duplicated restore logic.
        control = cmds.workspaceControl(
            WORKSPACE_CONTROL_NAME,
            label=WINDOW_TITLE,
            retain=True,
            initialWidth=430,
            minimumWidth=340,
            dockToMainWindow=("right", False),
            uiScript="import ta_tools; ta_tools._restore_docked_workspace()",
        )
        if not self.controls:
            self._build_content(control, WORKSPACE_CONTROL_NAME)

    # --- UI BUILDERS ---

    def _build_asset_doctor_section(self, parent: str) -> None:
        frame = cmds.frameLayout(
            label="Asset Doctor",
            collapsable=True,
            collapse=False,
            marginWidth=8,
            marginHeight=8,
            parent=parent,
        )

        col = cmds.columnLayout(
            adjustableColumn=True,
            rowSpacing=5,
            parent=frame,
        )

        self.controls["doctor_profile"] = cmds.optionMenuGrp(
            label="Profile",
            columnWidth=[(1, 90), (2, 260)],
            changeCommand=lambda *_: self.on_asset_doctor_profile_changed(),
            parent=col,
        )
        for _profile_id, label, _description in PROFILE_ITEMS:
            cmds.menuItem(label=label)

        default_label = PROFILE_ID_TO_LABEL.get("GENERIC_GAME", "Generic Game Asset")
        cmds.optionMenuGrp(
            self.controls["doctor_profile"],
            edit=True,
            value=default_label,
        )

        self.controls["doctor_profile_description"] = cmds.text(
            label=get_profile("GENERIC_GAME").get("description", ""),
            align="left",
            wordWrap=True,
            parent=col,
        )

        self.controls["project_profile_file"] = cmds.textFieldButtonGrp(
            label="Project profiles",
            buttonLabel="Browse",
            columnWidth=[(1, 90), (2, 210), (3, 60)],
            buttonCommand=lambda *_: self.on_browse_project_profiles(),
            parent=col,
        )
        cmds.button(
            label="Load Project Profiles",
            command=lambda *_: self.on_load_project_profiles(),
            parent=col,
        )

        row = cmds.rowLayout(
            numberOfColumns=4,
            adjustableColumn=4,
            columnWidth4=(100, 100, 100, 100),
            parent=col,
        )
        cmds.button(
            label="Analyze",
            command=lambda *_: self.on_asset_doctor_analyze(),
            parent=row,
        )
        cmds.button(
            label="Fix Safe",
            command=lambda *_: self.on_asset_doctor_fix_safe(),
            parent=row,
        )
        cmds.button(
            label="Select First",
            command=lambda *_: self.on_asset_doctor_select_first(),
            parent=row,
        )
        cmds.button(
            label="Export JSON",
            command=lambda *_: self.on_asset_doctor_export(),
            parent=row,
        )
        cmds.setParent(col)

        self.controls["doctor_split_details"] = cmds.checkBox(
            label="Show render split attribution",
            value=True,
            changeCommand=lambda *_: self.refresh_asset_doctor_output(),
            parent=col,
        )

        self.controls["doctor_enforce_budget"] = cmds.checkBox(
            label="Enforce values from Budget panel",
            value=False,
            parent=col,
        )

        self.controls["doctor_enforce_td"] = cmds.checkBox(
            label="Enforce texel-density target",
            value=False,
            parent=col,
        )

        split_row = cmds.rowLayout(
            numberOfColumns=4,
            adjustableColumn=4,
            columnWidth4=(100, 100, 100, 100),
            parent=col,
        )
        cmds.text(label="Split edges:", align="left", parent=split_row)
        cmds.button(label="Normals", command=lambda *_: self.on_select_render_split("NORMAL"), parent=split_row)
        cmds.button(label="UVs", command=lambda *_: self.on_select_render_split("UV"), parent=split_row)
        cmds.button(label="Materials", command=lambda *_: self.on_select_render_split("MATERIAL"), parent=split_row)
        cmds.setParent(col)

        default_report = os.path.join(
            cmds.workspace(query=True, rootDirectory=True),
            "jam_asset_report.json",
        )
        self.controls["doctor_report_path"] = cmds.textFieldButtonGrp(
            label="Report",
            text=default_report,
            buttonLabel="Browse",
            buttonCommand=lambda *_: self.on_browse_asset_report_path(),
            columnWidth=[(1, 90), (2, 235), (3, 65)],
            parent=col,
        )

        self.controls["doctor_output"] = cmds.scrollField(
            editable=False,
            wordWrap=False,
            height=230,
            text="Select one or more mesh assets and press Analyze.",
            parent=col,
        )

    def _build_authoring_section(self, parent: str) -> None:
        frame = cmds.frameLayout(
            label="Modular / Attachments",
            collapsable=True,
            collapse=True,
            marginWidth=8,
            marginHeight=8,
            parent=parent,
        )
        col = cmds.columnLayout(adjustableColumn=True, rowSpacing=5, parent=frame)

        grid = cmds.frameLayout(label="Physical Modular Grid", collapsable=True, collapse=False, parent=col)
        grid_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4, parent=grid)
        self.controls["mod_grid"] = cmds.floatFieldGrp(label="Grid cm", value1=100.0, columnWidth=[(1, 90), (2, 100)], parent=grid_col)
        self.controls["mod_tolerance"] = cmds.floatFieldGrp(label="Tolerance cm", value1=0.1, columnWidth=[(1, 90), (2, 100)], parent=grid_col)
        self.controls["mod_check_position"] = cmds.checkBox(label="Check world placement too", value=True, parent=grid_col)
        snap_row = cmds.rowLayout(numberOfColumns=4, adjustableColumn=4, columnWidth4=(70, 70, 70, 170), parent=grid_col)
        self.controls["mod_snap_x"] = cmds.checkBox(label="X", value=True, parent=snap_row)
        self.controls["mod_snap_y"] = cmds.checkBox(label="Y", value=True, parent=snap_row)
        self.controls["mod_snap_z"] = cmds.checkBox(label="Z", value=True, parent=snap_row)
        self.controls["mod_snap_mode"] = cmds.optionMenuGrp(label="Mode", columnWidth=[(1, 45), (2, 90)], parent=snap_row)
        for item in ("Nearest", "Floor", "Ceil"):
            cmds.menuItem(label=item)
        cmds.setParent(grid_col)
        row = cmds.rowLayout(numberOfColumns=2, adjustableColumn=2, columnWidth2=(190, 190), parent=grid_col)
        cmds.button(label="Check Bounds", command=lambda *_: self.on_modular_analyze(), parent=row)
        cmds.button(label="Snap Origins", command=lambda *_: self.on_modular_snap(), parent=row)
        cmds.setParent(grid_col)
        self.controls["mod_result"] = cmds.text(label="", align="left", parent=grid_col)

        align = cmds.frameLayout(label="Bounds Alignment", collapsable=True, collapse=True, parent=col)
        align_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4, parent=align)
        self.controls["mod_align_anchor"] = cmds.optionMenuGrp(label="Anchor", columnWidth=[(1, 90), (2, 220)], parent=align_col)
        for item in ("CENTER", "BOTTOM", "TOP", "X_MIN", "X_MAX", "Y_MIN", "Y_MAX", "Z_MIN", "Z_MAX"):
            cmds.menuItem(label=item)
        cmds.optionMenuGrp(self.controls["mod_align_anchor"], edit=True, value="BOTTOM")
        cmds.button(label="Align Selected to Active", command=lambda *_: self.on_modular_align(), parent=align_col)
        match_row = cmds.rowLayout(numberOfColumns=4, adjustableColumn=4, columnWidth4=(60, 60, 60, 200), parent=align_col)
        self.controls["mod_match_x"] = cmds.checkBox(label="X", value=True, parent=match_row)
        self.controls["mod_match_y"] = cmds.checkBox(label="Y", value=True, parent=match_row)
        self.controls["mod_match_z"] = cmds.checkBox(label="Z", value=True, parent=match_row)
        cmds.button(label="Match Bounds Size", command=lambda *_: self.on_modular_match_bounds(), parent=match_row)
        cmds.setParent(align_col)

        attach = cmds.frameLayout(label="Socket / Helper", collapsable=True, collapse=True, parent=col)
        attach_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4, parent=attach)
        self.controls["attachment_role"] = cmds.optionMenuGrp(label="Role", columnWidth=[(1, 90), (2, 220)], parent=attach_col)
        cmds.menuItem(label=ROLE_SOCKET)
        cmds.menuItem(label=ROLE_HELPER)
        self.controls["attachment_label"] = cmds.textFieldGrp(label="Label", text="Handle", columnWidth=[(1, 90), (2, 220)], parent=attach_col)
        self.controls["attachment_location"] = cmds.optionMenuGrp(label="Place at", columnWidth=[(1, 90), (2, 220)], parent=attach_col)
        for item in ("ACTIVE_PIVOT", "BOUNDS_CENTER", "BOUNDS_BOTTOM", "WORLD_ORIGIN"):
            cmds.menuItem(label=item)
        self.controls["attachment_size"] = cmds.floatFieldGrp(label="Display cm", value1=10.0, columnWidth=[(1, 90), (2, 100)], parent=attach_col)
        self.controls["attachment_parent"] = cmds.checkBox(label="Parent to active", value=True, parent=attach_col)
        cmds.button(label="Create Attachment", command=lambda *_: self.on_create_attachment(), parent=attach_col)

        normalize = cmds.frameLayout(label="Batch Transform Normalization", collapsable=True, collapse=True, parent=col)
        norm_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4, parent=normalize)
        self.controls["normalize_scale"] = cmds.checkBox(label="Freeze scale", value=True, parent=norm_col)
        self.controls["normalize_rotation"] = cmds.checkBox(label="Freeze rotation", value=False, parent=norm_col)
        self.controls["normalize_negative"] = cmds.checkBox(label="Allow negative scale", value=False, parent=norm_col)
        cmds.button(label="Apply Selected Channels", command=lambda *_: self.on_normalize_transforms(), parent=norm_col)

    def _build_asset_set_section(self, parent: str) -> None:
        frame = cmds.frameLayout(
            label="Asset Sets / LOD / Collision",
            collapsable=True,
            collapse=False,
            marginWidth=8,
            marginHeight=8,
            parent=parent,
        )
        col = cmds.columnLayout(adjustableColumn=True, rowSpacing=5, parent=frame)

        self.controls["asset_root"] = cmds.textFieldGrp(
            label="Root", text="", columnWidth=[(1, 90), (2, 280)], parent=col,
        )

        row = cmds.rowLayout(numberOfColumns=3, adjustableColumn=3, columnWidth3=(125, 125, 125), parent=col)
        cmds.button(label="Create / Update", command=lambda *_: self.on_asset_set_create(), parent=row)
        cmds.button(label="Select Set", command=lambda *_: self.on_asset_set_select(), parent=row)
        cmds.button(label="Analyze", command=lambda *_: self.on_asset_set_analyze(), parent=row)
        cmds.setParent(col)

        assign = cmds.frameLayout(label="Member Assignment", collapsable=True, collapse=True, parent=col)
        assign_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4, parent=assign)
        self.controls["asset_role"] = cmds.optionMenuGrp(label="Role", columnWidth=[(1, 90), (2, 220)], parent=assign_col)
        for role in (ROLE_RENDER, ROLE_LOD, ROLE_COLLISION, ROLE_SOCKET, ROLE_SKELETON, ROLE_HELPER):
            cmds.menuItem(label=role)
        self.controls["asset_lod"] = cmds.intFieldGrp(label="LOD level", value1=0, columnWidth=[(1, 90), (2, 100)], parent=assign_col)
        self.controls["asset_collision_type"] = cmds.optionMenuGrp(label="Collision", columnWidth=[(1, 90), (2, 220)], parent=assign_col)
        for collision_type in ("CONVEX", "BOX", "SPHERE", "CAPSULE"):
            cmds.menuItem(label=collision_type)
        self.controls["asset_export"] = cmds.checkBox(label="Include member in export", value=True, parent=assign_col)
        cmds.button(label="Assign Selected", command=lambda *_: self.on_asset_set_assign_role(), parent=assign_col)

        lod = cmds.frameLayout(label="LOD", collapsable=True, collapse=True, parent=col)
        lod_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4, parent=lod)
        self.controls["asset_lod_generate_level"] = cmds.intFieldGrp(label="Generate level", value1=1, columnWidth=[(1, 90), (2, 100)], parent=lod_col)
        self.controls["asset_lod_generate_ratio"] = cmds.floatFieldGrp(label="Keep ratio", value1=0.5, columnWidth=[(1, 90), (2, 100)], parent=lod_col)
        cmds.button(label="Duplicate Selected as LOD", command=lambda *_: self.on_asset_set_generate_lod(), parent=lod_col)
        self.controls["asset_lod_min_reduction"] = cmds.floatFieldGrp(label="Min reduction %", value1=20.0, columnWidth=[(1, 90), (2, 100)], parent=lod_col)
        self.controls["asset_lod_contiguous"] = cmds.checkBox(label="Require contiguous LOD chain", value=True, parent=lod_col)

        collision = cmds.frameLayout(label="Collision", collapsable=True, collapse=True, parent=col)
        collision_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4, parent=collision)
        row = cmds.rowLayout(numberOfColumns=2, adjustableColumn=2, parent=collision_col)
        cmds.button(label="Create UBX Box", command=lambda *_: self.on_asset_set_create_box_collision(), parent=row)
        cmds.button(label="Create USP Sphere", command=lambda *_: self.on_asset_set_create_sphere_collision(), parent=row)
        cmds.setParent(collision_col)
        self.controls["asset_collision_budget_enabled"] = cmds.checkBox(label="Enforce collision triangle budget", value=False, parent=collision_col)
        self.controls["asset_collision_budget"] = cmds.intFieldGrp(label="Collision tris", value1=256, columnWidth=[(1, 90), (2, 100)], parent=collision_col)

        export = cmds.frameLayout(label="Orchestrated Export", collapsable=True, collapse=True, parent=col)
        export_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4, parent=export)
        self.controls["asset_move_origin"] = cmds.checkBox(label="Move set to LOD0 origin for export", value=True, parent=export_col)
        cmds.button(label="Export Asset Set", command=lambda *_: self.on_asset_set_export(), parent=export_col)

        self.controls["asset_output"] = cmds.scrollField(
            editable=False,
            wordWrap=False,
            height=180,
            text="Create an Asset Set from a selection, then Analyze.",
            parent=col,
        )

    def _build_rename_section(self, parent: str) -> None:
        frame = cmds.frameLayout(
            label="Rename Objects",
            collapsable=True,
            collapse=False,
            marginWidth=8,
            marginHeight=8,
            parent=parent,
        )

        col = cmds.columnLayout(
            adjustableColumn=True,
            rowSpacing=4,
            parent=frame,
        )

        self.controls["rs_prefix"] = cmds.textFieldGrp(
            label="Prefix",
            text="SM",
            columnWidth=[(1, 90), (2, 280),],
            parent=col,
        )

        self.controls["rs_name"] = cmds.textFieldGrp(
            label="Item name",
            text="Prop",
            columnWidth=[(1, 90), (2, 280),],
            parent=col,
        )

        self.controls["rs_start"] = cmds.intFieldGrp(
            label="Start index",
            value1=1,
            columnWidth=[(1, 90), (2, 80),],
            parent=col,
        )

        self.controls["rs_shapes"] = cmds.checkBox(
            label="Rename shape nodes too?",
            value=True,
            parent=col,
        )

        cmds.button(
            label="Rename Selected",
            command=lambda *_: self.on_rename_selected(),
            parent=col,
        )

    def _build_budget_section(self, parent: str) -> None:
        frame = cmds.frameLayout(
            label="Budget",
            collapsable=True,
            collapse=False,
            marginWidth=8,
            marginHeight=8,
            parent=parent,
        )

        col = cmds.columnLayout(
            adjustableColumn=True,
            rowSpacing=4,
            parent=frame,
        )

        self.controls["budget_mode"] = cmds.optionMenuGrp(
            label="Count mode",
            columnWidth=[
                (1, 90),
                (2, 220),
            ],
            changeCommand=lambda *_: self.refresh_budget_marker(),
            parent=col,
        )

        for item in COUNT_MODES:
            cmds.menuItem(label=item)

        self.controls["budget"] = cmds.intFieldGrp(
            label="Budget",
            value1=3000,
            columnWidth=[
                (1, 90),
                (2, 100),
            ],
            changeCommand=lambda *_: self.refresh_budget_marker(),
            parent=col,
        )

        self.controls["margin"] = cmds.floatFieldGrp(
            label="Margin %",
            value1=10.0,
            columnWidth=[
                (1, 90),
                (2, 100),
            ],
            changeCommand=lambda *_: self.refresh_budget_marker(),
            parent=col,
        )

        self.controls["mat_splits"] = cmds.checkBox(
            label="Count material/shader splits",
            value=True,
            changeCommand=lambda *_: self.refresh_budget_marker(),
            parent=col,
        )

        self.controls["auto_prefix"] = cmds.checkBox(
            label="Budget-based LP/HP prefix on export",
            value=False,
            parent=col,
        )

        row = cmds.rowLayout(
            numberOfColumns=2,
            adjustableColumn=2,
            columnWidth2=(200, 200),
            parent=col,
        )

        self.controls["lp_prefix"] = cmds.textFieldGrp(
            label="LP",
            text="LP",
            columnWidth=[
                (1, 35),
                (2, 110),
            ],
            parent=row,
        )

        self.controls["hp_prefix"] = cmds.textFieldGrp(
            label="HP",
            text="HP",
            columnWidth=[
                (1, 35),
                (2, 110),
            ],
            parent=row,
        )

        cmds.setParent(col)

        cmds.button(
            label="Refresh Budget",
            command=lambda *_: self.refresh_budget_marker(),
            parent=col,
        )

        self.controls["budget_marker"] = cmds.text(
            label="Poly budget: No mesh selected",
            align="left",
            parent=col,
        )

    def _build_pivot_section(self, parent: str) -> None:
        frame = cmds.frameLayout(
            label="Pivot / Origin Placement",
            collapsable=True,
            collapse=False,
            marginWidth=8,
            marginHeight=8,
            parent=parent,
        )

        col = cmds.columnLayout(
            adjustableColumn=True,
            rowSpacing=4,
            parent=frame,
        )

        cmds.text(
            label="Set pivot by world-space selection/object bounding box.",
            align="left",
            parent=col,
        )

        row = cmds.rowLayout(
            numberOfColumns=3,
            adjustableColumn=3,
            columnWidth3=(130, 130, 130),
            parent=col,
        )

        self.controls["x_axis"] = self._axis_menu(
            "X axis",
            X_AXIS_ITEMS,
            parent=row,
        )

        self.controls["y_axis"] = self._axis_menu(
            "Y axis",
            Y_AXIS_ITEMS,
            parent=row,
        )

        self.controls["z_axis"] = self._axis_menu(
            "Z axis",
            Z_AXIS_ITEMS,
            parent=row,
        )

        cmds.setParent(col)

        self.controls["individual"] = cmds.checkBox(
            label="Apply individually per object",
            value=False,
            parent=col,
        )

        cmds.button(
            label="Move Pivot",
            command=lambda *_: self.on_move_pivot(),
            parent=col,
        )

        cmds.button(
            label="Move Pivot to Selected Vertex",
            command=lambda *_: self.on_pivot_to_selected_vertex(),
            parent=col
        )

    def _axis_menu(self, label: str, items: List[str], parent: str) -> str:
        control = cmds.optionMenuGrp(
            label=label,
            columnWidth=[
                (1, 45),
                (2, 70),
            ],
            parent=parent,
        )

        for item in items:
            cmds.menuItem(label=item)

        cmds.optionMenuGrp(
            control,
            edit=True,
            value="None",
        )

        return control

    def _build_validation_section(self, parent: str) -> None:
        frame = cmds.frameLayout(
            label="Validation",
            collapsable=True,
            collapse=False,
            marginWidth=8,
            marginHeight=8,
            parent=parent,
        )

        col = cmds.columnLayout(
            adjustableColumn=True,
            rowSpacing=4,
            parent=frame,
        )

        self.controls["select_issue"] = cmds.checkBox(
            label="Select first issue",
            value=True,
            parent=col,
        )

        self.controls["uv_checks"] = cmds.checkBox(
            label="Include UV checks (missing, 0-1 range, flipped)",
            value=True,
            parent=col,
        )

        cmds.button(
            label="Check Non-Manifold / Export Issues",
            command=lambda *_: self.on_validate(),
            parent=col,
        )

        self.controls["validation_message"] = cmds.text(
            label="",
            align="left",
            parent=col,
        )

    def _build_export_section(self, parent: str) -> None:
        frame = cmds.frameLayout(
            label="Easy Export",
            collapsable=True,
            collapse=False,
            marginWidth=8,
            marginHeight=8,
            parent=parent,
        )

        col = cmds.columnLayout(
            adjustableColumn=True,
            rowSpacing=4,
            parent=frame,
        )

        self.controls["allow_combine"] = cmds.checkBox(
            label="Allow destructive combine",
            value=False,
            parent=col,
        )

        cmds.button(
            label="Combine Selected Meshes",
            command=lambda *_: self.on_combine(),
            parent=col,
        )

        cmds.separator(
            style="in",
            parent=col,
        )

        self.controls["export_profile"] = cmds.optionMenuGrp(
            label="Export preset",
            columnWidth=[(1, 90), (2, 260)],
            parent=col,
        )
        for _profile_id, label, _description in EXPORT_PROFILE_ITEMS:
            cmds.menuItem(label=label)
        cmds.optionMenuGrp(
            self.controls["export_profile"],
            edit=True,
            value=EXPORT_PROFILE_ID_TO_LABEL.get("GENERIC_FBX", "Generic FBX"),
        )

        self.controls["export_path"] = cmds.textFieldButtonGrp(
            label="Export to",
            text=cmds.workspace(query=True, rootDirectory=True),
            buttonLabel="Browse",
            columnWidth=[
                (1, 90),
                (2, 230),
                (3, 70),
            ],
            buttonCommand=lambda *_: self.on_browse_export_path(),
            parent=col,
        )

        self.controls["validate_export"] = cmds.checkBox(
            label="Validate before export",
            value=True,
            parent=col,
        )

        self.controls["block_export"] = cmds.checkBox(
            label="Block export on validation issues",
            value=True,
            parent=col,
        )

        self.controls["batch_mode"] = cmds.checkBox(
            label="Batch: one file per selected object",
            value=False,
            parent=col,
        )

        self.controls["move_origin"] = cmds.checkBox(
            label="Move to origin on export (batch only)",
            value=True,
            parent=col,
        )

        self.controls["write_sidecar"] = cmds.checkBox(
            label="Write .jammeta.json engine sidecar",
            value=True,
            parent=col,
        )
        self.controls["overwrite_existing"] = cmds.checkBox(
            label="Overwrite existing target",
            value=False,
            parent=col,
        )
        preview_row = cmds.rowLayout(
            numberOfColumns=2, adjustableColumn=2, columnWidth2=(190, 190), parent=col,
        )
        cmds.button(label="Preview Export", command=lambda *_: self.on_preview_export(False), parent=preview_row)
        cmds.button(label="Preview Asset Set", command=lambda *_: self.on_preview_export(True), parent=preview_row)
        cmds.setParent(col)
        self.controls["export_preview"] = cmds.text(label="", align="left", wordWrap=True, parent=col)

        cmds.button(
            label="Export Selected",
            command=lambda *_: self.on_export(),
            parent=col,
        )

    def _build_status_section(self, parent: str) -> None:
        cmds.separator(
            style="in",
            parent=parent,
        )

        self.controls["status"] = cmds.text(
            label="Ready.",
            align="left",
            parent=parent,
        )

    def _build_texel_density_section(self, parent: str) -> None:
        frame = cmds.frameLayout(
            label="Texel Density",
            collapsable=True,
            collapse=True,
            marginWidth=8,
            marginHeight=8,
            parent=parent,
        )

        col = cmds.columnLayout(
            adjustableColumn=True,
            rowSpacing=4,
            parent=frame,
        )

        cmds.text(
            label="Density is unit-normalized; default display is px/cm.",
            align="left",
            parent=col,
        )

        self.controls["td_size"] = cmds.intFieldGrp(
            label="Texture size",
            value1=2048,
            columnWidth=[(1, 90), (2, 100)],
            parent=col,
        )

        self.controls["td_unit"] = cmds.optionMenuGrp(
            label="Display",
            columnWidth=[(1, 90), (2, 100)],
            parent=col,
        )
        cmds.menuItem(label="px/cm")
        cmds.menuItem(label="px/m")
        cmds.optionMenuGrp(self.controls["td_unit"], edit=True, value="px/cm")

        self.controls["td_target"] = cmds.floatFieldGrp(
            label="Target TD",
            value1=10.24,
            precision=3,
            columnWidth=[(1, 90), (2, 100)],
            parent=col,
        )

        self.controls["td_tolerance"] = cmds.floatFieldGrp(
            label="Tolerance %",
            value1=15.0,
            precision=1,
            columnWidth=[(1, 90), (2, 100)],
            parent=col,
        )

        row = cmds.rowLayout(
            numberOfColumns=2,
            adjustableColumn=2,
            columnWidth2=(200, 200),
            parent=col,
        )

        cmds.button(
            label="Check Selection TD",
            command=lambda *_: self.on_check_texel_density(),
            parent=row,
        )

        cmds.button(
            label="Set Selection TD",
            command=lambda *_: self.on_set_texel_density(),
            parent=row,
        )

        cmds.setParent(col)

        heat_row = cmds.rowLayout(
            numberOfColumns=2,
            adjustableColumn=2,
            columnWidth2=(200, 200),
            parent=col,
        )
        cmds.button(
            label="TD Heatmap",
            command=lambda *_: self.on_td_heatmap(),
            parent=heat_row,
        )
        cmds.button(
            label="Clear Heatmap",
            command=lambda *_: self.on_clear_td_heatmap(),
            parent=heat_row,
        )
        cmds.setParent(col)
        cmds.text(
            label="Heatmap: blue low · green target · red high",
            align="left",
            parent=col,
        )

        self.controls["td_result"] = cmds.text(
            label="",
            align="left",
            parent=col,
        )

    def _build_rigging_section(self, parent: str) -> None:
        frame = cmds.frameLayout(
            label="Rigging Helpers",
            collapsable=True,
            collapse=True,
            marginWidth=8,
            marginHeight=8,
            parent=parent,
        )

        col = cmds.columnLayout(
            adjustableColumn=True,
            rowSpacing=4,
            parent=frame,
        )

        cmds.text(
            label="IK: select the start joint, then the end joint.",
            align="left",
            parent=col,
        )

        self.controls["pole_mult"] = cmds.floatFieldGrp(
            label="Pole distance",
            value1=1.0,
            precision=2,
            columnWidth=[(1, 90), (2, 100)],
            parent=col,
        )

        cmds.button(
            label="Create IK Handle + Pole Vector",
            command=lambda *_: self.on_create_ik(),
            parent=col,
        )

        cmds.separator(style="in", parent=col)

        cmds.text(
            label="Muscle: select the upper joint, then the bend joint.",
            align="left",
            parent=col,
        )

        self.controls["muscle_axis"] = cmds.optionMenuGrp(
            label="Bend axis",
            columnWidth=[(1, 90), (2, 100)],
            parent=col,
        )

        for item in ("X", "Y", "Z"):
            cmds.menuItem(label=item)

        cmds.optionMenuGrp(
            self.controls["muscle_axis"],
            edit=True,
            value="Z",
        )

        self.controls["muscle_angle"] = cmds.floatFieldGrp(
            label="Max angle",
            value1=90.0,
            precision=1,
            columnWidth=[(1, 90), (2, 100)],
            parent=col,
        )

        self.controls["muscle_bulge"] = cmds.floatFieldGrp(
            label="Bulge scale",
            value1=1.4,
            precision=2,
            columnWidth=[(1, 90), (2, 100)],
            parent=col,
        )

        cmds.button(
            label="Create Muscle Helper Joint",
            command=lambda *_: self.on_create_muscle(),
            parent=col,
        )

    # --- UI Getters --

    def get_count_mode(self) -> str:
        return cmds.optionMenuGrp(
            self.controls["budget_mode"],
            query=True,
            value=True,
        )

    def get_budget(self) -> int:
        return cmds.intFieldGrp(
            self.controls["budget"],
            query=True,
            value1=True,
        )

    def get_margin(self) -> float:
        return cmds.floatFieldGrp(
            self.controls["margin"],
            query=True,
            value1=True,
        )

    def get_lp_prefix(self) -> str:
        return (
            cmds.textFieldGrp(
                self.controls["lp_prefix"],
                query=True,
                text=True,
            ).strip()
            or "LP"
        )

    def get_hp_prefix(self) -> str:
        return (
            cmds.textFieldGrp(
                self.controls["hp_prefix"],
                query=True,
                text=True,
            ).strip()
            or "HP"
        )

    def get_count_material_splits(self) -> bool:
        return cmds.checkBox(
            self.controls["mat_splits"],
            query=True,
            value=True,
        )

    def get_asset_doctor_profile_id(self) -> str:
        label = cmds.optionMenuGrp(
            self.controls["doctor_profile"],
            query=True,
            value=True,
        )
        return PROFILE_LABEL_TO_ID.get(label, "GENERIC_GAME")

    def get_asset_doctor_report_path(self) -> str:
        return cmds.textFieldButtonGrp(
            self.controls["doctor_report_path"],
            query=True,
            text=True,
        ).strip()

    def get_asset_doctor_split_details(self) -> bool:
        return cmds.checkBox(
            self.controls["doctor_split_details"],
            query=True,
            value=True,
        )

    def get_asset_doctor_enforce_budget(self) -> bool:
        return cmds.checkBox(
            self.controls["doctor_enforce_budget"],
            query=True,
            value=True,
        )

    def get_asset_doctor_enforce_td(self) -> bool:
        return cmds.checkBox(
            self.controls["doctor_enforce_td"],
            query=True,
            value=True,
        )

    def get_export_profile_id(self) -> str:
        label = cmds.optionMenuGrp(
            self.controls["export_profile"], query=True, value=True,
        )
        return EXPORT_PROFILE_LABEL_TO_ID.get(label, "GENERIC_FBX")

    def get_td_unit(self) -> str:
        return cmds.optionMenuGrp(
            self.controls["td_unit"],
            query=True,
            value=True,
        )

    def get_td_texture_size(self) -> int:
        return int(cmds.intFieldGrp(
            self.controls["td_size"], query=True, value1=True,
        ))

    def get_td_target(self) -> float:
        return float(cmds.floatFieldGrp(
            self.controls["td_target"], query=True, value1=True,
        ))

    def get_td_tolerance(self) -> float:
        return float(cmds.floatFieldGrp(
            self.controls["td_tolerance"], query=True, value1=True,
        ))

    def set_status(self, message: str) -> None:
        cmds.text(
            self.controls["status"],
            edit=True,
            label=message,
        )

        _status(message)

    def set_validation_message(self, message: str) -> None:
        cmds.text(
            self.controls["validation_message"],
            edit=True,
            label=message,
        )

    # ---------------- Settings persistence ----------------

    def save_settings(self) -> None:
        for key, control_type, flag in _PERSISTED_CONTROLS:
            control = self.controls.get(key)
            command = getattr(cmds, control_type, None)

            if not control or command is None:
                continue

            try:
                if not command(control, exists=True):
                    continue

                value = command(control, query=True, **{flag: True})
            except Exception:
                continue

            _save_option(key, value)

    def _apply_saved_settings(self) -> None:
        for key, control_type, flag in _PERSISTED_CONTROLS:
            raw = _load_option(key, "")

            if raw == "":
                continue

            control = self.controls.get(key)
            command = getattr(cmds, control_type, None)

            if not control or command is None:
                continue

            try:
                if control_type == "checkBox":
                    command(control, edit=True, value=raw in ("1", "True"))
                elif control_type == "intFieldGrp":
                    command(control, edit=True, value1=int(float(raw)))
                elif control_type == "floatFieldGrp":
                    command(control, edit=True, value1=float(raw))
                else:
                    # textFieldGrp / textFieldButtonGrp / optionMenuGrp;
                    # optionMenuGrp raises if the saved value is no longer
                    # a menu item, which the except silently absorbs
                    command(control, edit=True, **{flag: raw})
            except Exception:
                pass

    # ---------------- UI callbacks ----------------

    def on_browse_project_profiles(self) -> None:
        result = cmds.fileDialog2(
            fileMode=1,
            caption="Load JAM TA Project Profiles",
            fileFilter="JSON (*.json)",
        ) or []
        if result:
            cmds.textFieldButtonGrp(
                self.controls["project_profile_file"], edit=True, text=result[0]
            )

    def on_load_project_profiles(self) -> None:
        path = cmds.textFieldButtonGrp(
            self.controls["project_profile_file"], query=True, text=True
        )
        if not path or not os.path.isfile(path):
            self.set_status("Project profile JSON does not exist")
            return
        try:
            loaded = load_profiles_json(path)
            _refresh_profile_catalog()
        except Exception as exc:
            self.set_status("Could not load project profiles: {0}".format(exc))
            return
        if not loaded:
            self.set_status("No project profiles found in JSON")
            return

        # Rebuild the small window instead of poking Maya's internal optionMenu
        # child hierarchy. Rebuilding is more robust across Maya UI changes.
        _save_option("project_profile_file", path)
        self.save_settings()
        self.show()
        label = PROFILE_ID_TO_LABEL.get(loaded[0])
        if label:
            try:
                cmds.optionMenuGrp(self.controls["doctor_profile"], edit=True, value=label)
                _save_option("doctor_profile", label)
                self.on_asset_doctor_profile_changed()
            except Exception:
                pass
        self.set_status("Loaded {0} project profile(s)".format(len(loaded)))

    def on_asset_doctor_profile_changed(self) -> None:
        try:
            profile = get_profile(self.get_asset_doctor_profile_id())
            cmds.text(
                self.controls["doctor_profile_description"],
                edit=True,
                label=str(profile.get("description", "")),
            )
        except Exception:
            pass

    def _run_asset_doctor(self) -> List[AssetReport]:
        return maya_analyze_selection(
            profile_id=self.get_asset_doctor_profile_id(),
            budget_mode=self.get_count_mode(),
            budget=self.get_budget(),
            margin_percent=self.get_margin(),
            count_material_splits=self.get_count_material_splits(),
            enforce_budget=self.get_asset_doctor_enforce_budget(),
            td_texture_size=self.get_td_texture_size(),
            td_target_px_per_m=maya_density_from_display(self.get_td_target(), self.get_td_unit()),
            td_tolerance_percent=self.get_td_tolerance(),
            enforce_td_target=self.get_asset_doctor_enforce_td(),
        )

    def refresh_asset_doctor_output(self) -> None:
        if "doctor_output" not in self.controls:
            return
        try:
            text = maya_format_asset_doctor_report(
                MAYA_LAST_REPORTS,
                show_split_details=self.get_asset_doctor_split_details(),
            )
            cmds.scrollField(
                self.controls["doctor_output"],
                edit=True,
                text=text,
            )
        except Exception as exc:
            cmds.scrollField(
                self.controls["doctor_output"],
                edit=True,
                text="Asset Doctor display failed: {0}".format(exc),
            )

    def on_asset_doctor_analyze(self) -> None:
        try:
            reports = self._run_asset_doctor()
            self.refresh_asset_doctor_output()
            self.set_status(maya_asset_doctor_summary(reports))
        except Exception as exc:
            self.set_status("Asset Doctor failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_asset_doctor_fix_safe(self) -> None:
        try:
            reports = self._run_asset_doctor()
            fixed = maya_apply_safe_fixes(reports)
            reports = self._run_asset_doctor()
            self.refresh_asset_doctor_output()
            self.set_status(
                "Applied {0} safe fix(es). {1}".format(
                    fixed,
                    maya_asset_doctor_summary(reports),
                )
            )
        except Exception as exc:
            self.set_status("Safe fix failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_asset_doctor_select_first(self) -> None:
        try:
            reports = MAYA_LAST_REPORTS or self._run_asset_doctor()
            for report in reports:
                issues = report.sorted_issues()
                if issues:
                    maya_select_report_issue(report, issues[0])
                    self.set_status(
                        "Selected {0}: {1}".format(
                            report.object_name,
                            issues[0].title,
                        )
                    )
                    return
            self.set_status("Asset Doctor: no issues to select.")
        except Exception as exc:
            self.set_status("Could not select issue: {0}".format(exc))
            cmds.warning(str(exc))

    def on_select_render_split(self, cause: str) -> None:
        try:
            objects = get_selected_mesh_transforms()
            if not objects:
                raise RuntimeError("Select a mesh asset first")
            obj = objects[0]
            components = maya_render_split_components(obj, cause)
            if components:
                cmds.select(components, replace=True)
                try:
                    cmds.selectMode(component=True)
                except Exception:
                    pass
            else:
                cmds.select(obj, replace=True)
            self.set_status(
                "Selected {0} {1} split edge(s) on {2}.".format(
                    len(components), cause.lower(), _short_name(obj)
                )
            )
        except Exception as exc:
            self.set_status("Render-split selection failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_browse_asset_report_path(self) -> None:
        try:
            result = cmds.fileDialog2(
                fileMode=0,
                caption="Save Asset Doctor JSON Report",
                fileFilter="JSON (*.json)",
            ) or []
            if result:
                path = result[0]
                if not path.lower().endswith(".json"):
                    path += ".json"
                cmds.textFieldButtonGrp(
                    self.controls["doctor_report_path"],
                    edit=True,
                    text=path,
                )
        except Exception as exc:
            self.set_status("Report path failed: {0}".format(exc))

    def on_asset_doctor_export(self) -> None:
        try:
            reports = MAYA_LAST_REPORTS or self._run_asset_doctor()
            if not reports:
                raise RuntimeError("No mesh assets to report")

            path = self.get_asset_doctor_report_path()
            if not path:
                raise RuntimeError("Choose a report path first")
            if not path.lower().endswith(".json"):
                path += ".json"

            folder = os.path.dirname(path)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)

            write_report_json(
                path,
                reports,
                suite_version="2.4.0",
                metadata={
                    "host": "Maya",
                    "profile": self.get_asset_doctor_profile_id(),
                },
            )
            self.set_status("Asset report written to {0}".format(path))
        except Exception as exc:
            self.set_status("Report export failed: {0}".format(exc))
            cmds.warning(str(exc))

    def _asset_root(self) -> str:
        return cmds.textFieldGrp(self.controls["asset_root"], query=True, text=True).strip()

    def _asset_analysis_settings(self):
        enforce_collision = cmds.checkBox(
            self.controls["asset_collision_budget_enabled"], query=True, value=True,
        )
        return {
            "min_lod_reduction_percent": float(cmds.floatFieldGrp(
                self.controls["asset_lod_min_reduction"], query=True, value1=True,
            )),
            "require_contiguous_lods": bool(cmds.checkBox(
                self.controls["asset_lod_contiguous"], query=True, value=True,
            )),
            "collision_triangle_budget": (
                int(cmds.intFieldGrp(self.controls["asset_collision_budget"], query=True, value1=True))
                if enforce_collision else None
            ),
        }

    def _doctor_options(self) -> Dict[str, object]:
        return {
            "profile_id": self.get_asset_doctor_profile_id(),
            "budget_mode": self.get_count_mode(),
            "budget": self.get_budget(),
            "margin_percent": self.get_margin(),
            "count_material_splits": self.get_count_material_splits(),
            "enforce_budget": self.get_asset_doctor_enforce_budget(),
            "td_texture_size": self.get_td_texture_size(),
            "td_target_px_per_m": maya_density_from_display(self.get_td_target(), self.get_td_unit()),
            "td_tolerance_percent": self.get_td_tolerance(),
            "enforce_td_target": self.get_asset_doctor_enforce_td(),
        }

    def refresh_asset_set_output(self, analysis=None) -> None:
        analysis = analysis or MAYA_LAST_ASSET_SET
        if not analysis or "asset_output" not in self.controls:
            return
        metrics = analysis.get("metrics", {})
        lines = [
            "{0} · {1}".format(analysis.get("root_name", "Asset"), analysis.get("status", "CLEAN")),
            "{0} members · {1} LOD levels · {2} collisions · {3} sockets".format(
                metrics.get("member_count", 0),
                metrics.get("lod_count", 0),
                metrics.get("collision_count", 0),
                metrics.get("socket_count", 0),
            ),
            "",
            "LOD progression",
        ]
        for lod in metrics.get("lod_metrics", []):
            reduction = lod.get("reduction_from_previous_percent")
            suffix = "" if reduction is None else " · -{0:.1f}%".format(float(reduction))
            lines.append("  LOD{0}: {1:,} tris · {2} member(s){3}".format(
                int(lod.get("level", 0)), int(lod.get("triangles", 0)), int(lod.get("member_count", 0)), suffix
            ))
        lines.append("")
        lines.append("Collision: {0:,} tris".format(int(metrics.get("collision_triangles", 0))))
        issues = analysis.get("issues", [])
        if issues:
            lines.append("")
            lines.append("Issues")
            for issue in issues:
                lines.append("  {0} | {1}: {2}".format(issue.severity, issue.category, issue.title))
                lines.append("    " + issue.message)
        lines.append("")
        lines.append("Members")
        for member in analysis.get("members", []):
            role = str(member.get("role", ""))
            lod_level = int(member.get("lod_level", 0))
            if role in {ROLE_RENDER, ROLE_LOD}:
                role = "{0}{1}".format(role, lod_level)
            enabled = "" if member.get("export_enabled", True) else " [disabled]"
            lines.append("  {0} · {1}{2}".format(member.get("name", ""), role, enabled))
        cmds.scrollField(self.controls["asset_output"], edit=True, text="\n".join(lines))

    def on_preview_export(self, asset_set: bool = False) -> None:
        try:
            export_folder = cmds.textFieldButtonGrp(self.controls["export_path"], query=True, text=True)
            profile_id = self.get_export_profile_id()
            write_sidecar = bool(cmds.checkBox(self.controls["write_sidecar"], query=True, value=True))
            if asset_set:
                root = self._asset_root()
                if not root:
                    selected = maya_selected_asset_nodes()
                    if selected:
                        root = maya_asset_set_root_for_node(selected[-1])
                if not root:
                    raise RuntimeError("No active Asset Set")
                members = maya_get_asset_set_members(root, include_disabled=False)
                base_name = root
                names = [_short_name(node) for node in members]
            else:
                selected = maya_selected_asset_nodes()
                if not selected:
                    raise RuntimeError("No selected object")
                active = selected[-1]
                base_name = _short_name(active)
                names = [_short_name(node) for node in selected]
            plan = build_export_plan(export_folder, base_name, profile_id, names, write_sidecar)
            message = format_export_plan(plan)
            cmds.text(self.controls["export_preview"], edit=True, label=message)
            if plan.get("has_conflict") and not cmds.checkBox(self.controls["overwrite_existing"], query=True, value=True):
                self.set_status("Target exists; enable overwrite to replace it.")
            else:
                self.set_status(message)
        except Exception as exc:
            self.set_status("Export preview failed: {0}".format(exc))
            cmds.warning(str(exc))

    def _modular_settings(self):
        return {
            "grid_cm": float(cmds.floatFieldGrp(self.controls["mod_grid"], query=True, value1=True)),
            "tolerance_cm": float(cmds.floatFieldGrp(self.controls["mod_tolerance"], query=True, value1=True)),
            "check_position": bool(cmds.checkBox(self.controls["mod_check_position"], query=True, value=True)),
        }

    def on_modular_analyze(self) -> None:
        try:
            nodes = [node for node in maya_selected_asset_nodes() if get_mesh_shapes(node)]
            if not nodes:
                raise RuntimeError("Select one or more mesh transforms")
            settings = self._modular_settings()
            bad_dimensions = []
            bad_positions = []
            for node in nodes:
                result = maya_modular_analysis(node, **settings)
                if result and not result.get("dimensions_on_grid", True):
                    bad_dimensions.append(_short_name(node))
                if result and settings["check_position"] and not result.get("position_on_grid", True):
                    bad_positions.append(_short_name(node))
            message = "{0} checked · {1} size issue(s) · {2} placement issue(s)".format(
                len(nodes), len(bad_dimensions), len(bad_positions)
            )
            cmds.text(self.controls["mod_result"], edit=True, label=message)
            self.set_status(message)
        except Exception as exc:
            self.set_status("Modular check failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_modular_snap(self) -> None:
        try:
            nodes = maya_selected_asset_nodes()
            if not nodes:
                raise RuntimeError("Select transforms to snap")
            factor = maya_cm_per_unit()
            step = float(cmds.floatFieldGrp(self.controls["mod_grid"], query=True, value1=True))
            axes = (
                bool(cmds.checkBox(self.controls["mod_snap_x"], query=True, value=True)),
                bool(cmds.checkBox(self.controls["mod_snap_y"], query=True, value=True)),
                bool(cmds.checkBox(self.controls["mod_snap_z"], query=True, value=True)),
            )
            mode = str(cmds.optionMenuGrp(self.controls["mod_snap_mode"], query=True, value=True)).upper()
            changed = 0
            for node in nodes:
                translation = cmds.xform(node, query=True, worldSpace=True, translation=True)
                cm = tuple(float(v) * factor for v in translation)
                snapped = snap_vector(cm, step, axes, mode)
                target = tuple(v / factor for v in snapped)
                if any(abs(target[i] - float(translation[i])) > 1e-10 for i in range(3)):
                    cmds.xform(node, worldSpace=True, translation=target)
                    changed += 1
            self.set_status("Snapped {0} object(s).".format(changed))
        except Exception as exc:
            self.set_status("Grid snap failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_modular_align(self) -> None:
        try:
            nodes = maya_selected_asset_nodes()
            if len(nodes) < 2:
                raise RuntimeError("Select objects to move, then the active/reference object last")
            active = nodes[-1]
            movers = nodes[:-1]
            anchor_name = cmds.optionMenuGrp(self.controls["mod_align_anchor"], query=True, value=True)
            minimum, maximum = maya_world_bounds([active])
            if minimum is None:
                raise RuntimeError("Reference object has no bounds")
            target = bounds_anchor(minimum, maximum, anchor_name)
            for node in movers:
                lo, hi = maya_world_bounds([node])
                if lo is None:
                    continue
                current = bounds_anchor(lo, hi, anchor_name)
                delta = tuple(target[i] - current[i] for i in range(3))
                translation = cmds.xform(node, query=True, worldSpace=True, translation=True)
                cmds.xform(
                    node,
                    worldSpace=True,
                    translation=tuple(float(translation[i]) + delta[i] for i in range(3)),
                )
            self.set_status("Aligned {0} object(s) to {1}.".format(len(movers), anchor_name))
        except Exception as exc:
            self.set_status("Bounds alignment failed: {0}".format(exc))
            cmds.warning(str(exc))


    def on_modular_match_bounds(self) -> None:
        try:
            nodes = maya_selected_asset_nodes()
            if len(nodes) < 2:
                raise RuntimeError("Select objects to resize, then the active/reference object last")
            active = nodes[-1]
            movers = nodes[:-1]

            def axis_aligned(node: str) -> bool:
                rotation = cmds.xform(node, query=True, worldSpace=True, rotation=True)
                return all(abs(float(value)) <= 1e-4 for value in rotation)

            if not axis_aligned(active):
                raise RuntimeError("Bounds matching requires an axis-aligned active reference")
            target_min, target_max = maya_world_bounds([active])
            if target_min is None:
                raise RuntimeError("Reference object has no bounds")
            target_dims = tuple(float(target_max[i] - target_min[i]) for i in range(3))
            axes = (
                bool(cmds.checkBox(self.controls["mod_match_x"], query=True, value=True)),
                bool(cmds.checkBox(self.controls["mod_match_y"], query=True, value=True)),
                bool(cmds.checkBox(self.controls["mod_match_z"], query=True, value=True)),
            )

            matched = 0
            skipped = 0
            for node in movers:
                if not get_mesh_shapes(node) or not axis_aligned(node):
                    skipped += 1
                    continue
                minimum, maximum = maya_world_bounds([node])
                if minimum is None:
                    skipped += 1
                    continue
                source_dims = tuple(float(maximum[i] - minimum[i]) for i in range(3))
                factors = bounds_scale_factors(source_dims, target_dims, axes)
                current = (
                    float(cmds.getAttr(node + ".scaleX")),
                    float(cmds.getAttr(node + ".scaleY")),
                    float(cmds.getAttr(node + ".scaleZ")),
                )
                cmds.setAttr(node + ".scaleX", current[0] * factors[0])
                cmds.setAttr(node + ".scaleY", current[1] * factors[1])
                cmds.setAttr(node + ".scaleZ", current[2] * factors[2])
                matched += 1

            self.set_status("Matched bounds for {0} object(s); skipped {1} rotated/invalid.".format(matched, skipped))
        except Exception as exc:
            self.set_status("Bounds matching failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_create_attachment(self) -> None:
        try:
            selected = maya_selected_asset_nodes()
            active = selected[-1] if selected else None
            root = self._asset_root()
            if not root and active:
                root = maya_asset_set_root_for_node(active)
            if not root:
                raise RuntimeError("Choose an Asset Set root or select an asset member")

            role = cmds.optionMenuGrp(self.controls["attachment_role"], query=True, value=True)
            label = cmds.textFieldGrp(self.controls["attachment_label"], query=True, text=True)
            mode = cmds.optionMenuGrp(self.controls["attachment_location"], query=True, value=True)
            location = maya_attachment_location(selected, mode)
            name = make_attachment_name(root, label, role)
            locator = cmds.spaceLocator(name=name)[0]
            cmds.xform(locator, worldSpace=True, translation=location)

            size_cm = max(0.01, float(cmds.floatFieldGrp(self.controls["attachment_size"], query=True, value1=True)))
            size = size_cm / maya_cm_per_unit()
            shapes = cmds.listRelatives(locator, shapes=True, fullPath=True) or []
            for shape in shapes:
                for axis in "XYZ":
                    attr = shape + ".localScale" + axis
                    if cmds.objExists(attr):
                        cmds.setAttr(attr, size)

            if active and cmds.checkBox(self.controls["attachment_parent"], query=True, value=True):
                cmds.parent(locator, active, absolute=True)
                locator = _long_name(locator)

            maya_set_asset_membership(locator, root, role=role, lod_level=0, collision_type="", export_enabled=True)
            cmds.textFieldGrp(self.controls["asset_root"], edit=True, text=root)
            cmds.select(locator, replace=True)
            analysis = maya_build_asset_set_analysis(root, **self._asset_analysis_settings())
            self.refresh_asset_set_output(analysis)
            self.set_status("Created {0}.".format(_short_name(locator)))
        except Exception as exc:
            self.set_status("Attachment creation failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_normalize_transforms(self) -> None:
        try:
            nodes = [node for node in maya_selected_asset_nodes() if cmds.nodeType(node) == "transform"]
            if not nodes:
                raise RuntimeError("Select transform nodes")
            apply_scale = bool(cmds.checkBox(self.controls["normalize_scale"], query=True, value=True))
            apply_rotation = bool(cmds.checkBox(self.controls["normalize_rotation"], query=True, value=True))
            allow_negative = bool(cmds.checkBox(self.controls["normalize_negative"], query=True, value=True))
            applied = 0
            skipped = 0
            for node in nodes:
                scale = cmds.xform(node, query=True, relative=True, scale=True)
                if apply_scale and any(float(value) < 0.0 for value in scale) and not allow_negative:
                    skipped += 1
                    continue
                cmds.makeIdentity(
                    node,
                    apply=True,
                    translate=False,
                    rotate=apply_rotation,
                    scale=apply_scale,
                    normal=0,
                )
                applied += 1
            self.set_status("Normalized {0} object(s); skipped {1}.".format(applied, skipped))
        except Exception as exc:
            self.set_status("Transform normalization failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_asset_set_create(self) -> None:
        try:
            nodes = maya_selected_asset_nodes()
            if not nodes:
                raise RuntimeError("Select at least one transform/joint")
            root = self._asset_root() or canonical_root_from_members(
                [_short_name(node) for node in nodes],
                _short_name(nodes[0]),
            )
            for node in nodes:
                inferred = infer_asset_member(_short_name(node), root)
                role = inferred.role
                if cmds.nodeType(node) == "joint":
                    role = ROLE_SKELETON
                elif not get_mesh_shapes(node) and role == ROLE_RENDER:
                    role = ROLE_SOCKET
                maya_set_asset_membership(
                    node, root, role=role, lod_level=inferred.lod_level,
                    collision_type=inferred.collision_type, export_enabled=True,
                )
            cmds.textFieldGrp(self.controls["asset_root"], edit=True, text=root)
            analysis = maya_build_asset_set_analysis(root, **self._asset_analysis_settings())
            self.refresh_asset_set_output(analysis)
            self.set_status("Asset Set {0}: {1} member(s).".format(root, analysis["metrics"]["member_count"]))
        except Exception as exc:
            self.set_status("Asset Set creation failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_asset_set_select(self) -> None:
        try:
            root = self._asset_root()
            if not root:
                selected = maya_selected_asset_nodes()
                if selected:
                    root = str(_maya_get_attr(selected[0], MAYA_ASSET_ROOT_ATTR, "") or "")
            if not root:
                raise RuntimeError("Choose an Asset Set root first")
            nodes = maya_get_asset_set_members(root, include_disabled=True)
            cmds.select(nodes, replace=True) if nodes else cmds.select(clear=True)
            self.set_status("Selected {0} Asset Set member(s).".format(len(nodes)))
        except Exception as exc:
            self.set_status("Asset Set selection failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_asset_set_assign_role(self) -> None:
        try:
            nodes = maya_selected_asset_nodes()
            if not nodes:
                raise RuntimeError("Select one or more Asset Set members")
            root = self._asset_root()
            if not root:
                root = str(_maya_get_attr(nodes[0], MAYA_ASSET_ROOT_ATTR, "") or "")
            if not root:
                root = canonical_root_from_members([_short_name(node) for node in nodes], _short_name(nodes[0]))
            role = cmds.optionMenuGrp(self.controls["asset_role"], query=True, value=True)
            lod_level = int(cmds.intFieldGrp(self.controls["asset_lod"], query=True, value1=True))
            collision_type = cmds.optionMenuGrp(self.controls["asset_collision_type"], query=True, value=True) if role == ROLE_COLLISION else ""
            export_enabled = bool(cmds.checkBox(self.controls["asset_export"], query=True, value=True))
            for node in nodes:
                maya_set_asset_membership(node, root, role, lod_level, collision_type, export_enabled)
            cmds.textFieldGrp(self.controls["asset_root"], edit=True, text=root)
            analysis = maya_build_asset_set_analysis(root, **self._asset_analysis_settings())
            self.refresh_asset_set_output(analysis)
            self.set_status("Assigned {0} member(s) as {1}.".format(len(nodes), role))
        except Exception as exc:
            self.set_status("Asset role assignment failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_asset_set_analyze(self) -> None:
        try:
            root = self._asset_root()
            if not root:
                selected = maya_selected_asset_nodes()
                root = str(_maya_get_attr(selected[0], MAYA_ASSET_ROOT_ATTR, "") or "") if selected else ""
            if not root:
                raise RuntimeError("Create or select an Asset Set first")
            cmds.textFieldGrp(self.controls["asset_root"], edit=True, text=root)
            analysis = maya_build_asset_set_analysis(root, **self._asset_analysis_settings())
            self.refresh_asset_set_output(analysis)
            self.set_status("Asset Set {0}: {1}.".format(root, analysis.get("status", "CLEAN")))
        except Exception as exc:
            self.set_status("Asset Set analysis failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_asset_set_create_box_collision(self) -> None:
        try:
            root = self._asset_root()
            if not root:
                raise RuntimeError("Create or activate an Asset Set first")
            source = [
                node for node in maya_selected_asset_nodes()
                if get_mesh_shapes(node) and maya_asset_set_root_for_node(node) == root
            ]
            if not source:
                source = [
                    node for node in maya_get_asset_set_members(root)
                    if get_mesh_shapes(node)
                    and str(_maya_get_attr(node, MAYA_ASSET_ROLE_ATTR, "") or "") in {ROLE_RENDER, ROLE_LOD}
                    and int(_maya_get_attr(node, MAYA_ASSET_LOD_ATTR, 0) or 0) == 0
                ]
            if not source:
                raise RuntimeError("Asset Set has no render mesh to bound")
            bbox = cmds.exactWorldBoundingBox(source)
            dx, dy, dz = bbox[3] - bbox[0], bbox[4] - bbox[1], bbox[5] - bbox[2]
            center = ((bbox[0] + bbox[3]) * 0.5, (bbox[1] + bbox[4]) * 0.5, (bbox[2] + bbox[5]) * 0.5)
            existing = [
                _short_name(node) for node in maya_get_asset_set_members(root)
                if _short_name(node).startswith("UBX_{0}_".format(root))
            ]
            index = 0
            while "UBX_{0}_{1:02d}".format(root, index) in existing:
                index += 1
            name = "UBX_{0}_{1:02d}".format(root, index)
            cube = cmds.polyCube(name=name, width=max(dx, 1e-6), height=max(dy, 1e-6), depth=max(dz, 1e-6), constructionHistory=False)[0]
            cmds.xform(cube, worldSpace=True, translation=center)
            maya_set_asset_membership(cube, root, ROLE_COLLISION, 0, "BOX", True)
            cmds.select(cube, replace=True)
            analysis = maya_build_asset_set_analysis(root, **self._asset_analysis_settings())
            self.refresh_asset_set_output(analysis)
            self.set_status("Created {0}.".format(name))
        except Exception as exc:
            self.set_status("Collision creation failed: {0}".format(exc))
            cmds.warning(str(exc))


    def on_asset_set_create_sphere_collision(self) -> None:
        try:
            root = self._asset_root()
            if not root:
                raise RuntimeError("Create or activate an Asset Set first")
            source = [
                node for node in maya_selected_asset_nodes()
                if get_mesh_shapes(node) and maya_asset_set_root_for_node(node) == root
            ]
            if not source:
                source = [
                    node for node in maya_get_asset_set_members(root)
                    if get_mesh_shapes(node)
                    and str(_maya_get_attr(node, MAYA_ASSET_ROLE_ATTR, "") or "") in {ROLE_RENDER, ROLE_LOD}
                    and int(_maya_get_attr(node, MAYA_ASSET_LOD_ATTR, 0) or 0) == 0
                ]
            if not source:
                raise RuntimeError("Asset Set has no render mesh to bound")

            bbox = cmds.exactWorldBoundingBox(source)
            center = ((bbox[0] + bbox[3]) * 0.5, (bbox[1] + bbox[4]) * 0.5, (bbox[2] + bbox[5]) * 0.5)
            dx, dy, dz = bbox[3] - bbox[0], bbox[4] - bbox[1], bbox[5] - bbox[2]
            # Half the AABB diagonal encloses every corner. It is not the tightest sphere, but
            # Collision generation favors predictable bounds over a tighter approximation.
            radius = max((dx * dx + dy * dy + dz * dz) ** 0.5 * 0.5, 1e-6)
            existing = [
                _short_name(node) for node in maya_get_asset_set_members(root)
                if _short_name(node).startswith("USP_{0}_".format(root))
            ]
            index = 0
            while "USP_{0}_{1:02d}".format(root, index) in existing:
                index += 1
            name = "USP_{0}_{1:02d}".format(root, index)
            sphere = cmds.polySphere(name=name, radius=radius, subdivisionsX=16, subdivisionsY=8, constructionHistory=False)[0]
            cmds.xform(sphere, worldSpace=True, translation=center)
            maya_set_asset_membership(sphere, root, ROLE_COLLISION, 0, "SPHERE", True)
            cmds.select(sphere, replace=True)
            analysis = maya_build_asset_set_analysis(root, **self._asset_analysis_settings())
            self.refresh_asset_set_output(analysis)
            self.set_status("Created {0}.".format(name))
        except Exception as exc:
            self.set_status("Collision creation failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_asset_set_generate_lod(self) -> None:
        try:
            root = self._asset_root()
            level = int(cmds.intFieldGrp(self.controls["asset_lod_generate_level"], query=True, value1=True))
            ratio = float(cmds.floatFieldGrp(self.controls["asset_lod_generate_ratio"], query=True, value1=True))
            ratio = max(0.01, min(1.0, ratio))
            source = [
                node for node in maya_selected_asset_nodes()
                if get_mesh_shapes(node)
                and maya_asset_set_root_for_node(node) == root
                and str(_maya_get_attr(node, MAYA_ASSET_ROLE_ATTR, ROLE_RENDER) or ROLE_RENDER) in {ROLE_RENDER, ROLE_LOD}
            ]
            if not root or not source:
                raise RuntimeError("Select render members from an Asset Set")
            created = []
            for node in source:
                base = _short_name(node)
                match = re.match(r"^(.*)_LOD\d+$", base, re.IGNORECASE)
                if match:
                    base = match.group(1)
                dup = cmds.duplicate(node, name="{0}_LOD{1}".format(base, level), returnRootsOnly=True)[0]
                try:
                    cmds.polyReduce(dup, percentage=(1.0 - ratio) * 100.0, constructionHistory=True)
                except Exception:
                    cmds.warning("Could not add polyReduce history to {0}; duplicated mesh is still assigned to the LOD.".format(dup))
                maya_set_asset_membership(dup, root, ROLE_LOD, level, "", True)
                created.append(dup)
            cmds.select(created, replace=True)
            analysis = maya_build_asset_set_analysis(root, **self._asset_analysis_settings())
            self.refresh_asset_set_output(analysis)
            self.set_status("Generated LOD{0} for {1} member(s) at {2:.0f}% keep ratio.".format(level, len(created), ratio * 100.0))
        except Exception as exc:
            self.set_status("LOD generation failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_asset_set_export(self) -> None:
        try:
            root = self._asset_root()
            if not root:
                raise RuntimeError("Choose an Asset Set root first")
            analysis = maya_build_asset_set_analysis(root, **self._asset_analysis_settings())
            nodes = [
                node for node in analysis.get("nodes", [])
                if bool(_maya_get_attr(node, MAYA_ASSET_EXPORT_ATTR, True))
            ]
            if not nodes:
                raise RuntimeError("Asset Set has no export-enabled members")

            validate = bool(cmds.checkBox(self.controls["validate_export"], query=True, value=True))
            block = bool(cmds.checkBox(self.controls["block_export"], query=True, value=True))
            write_sidecar = bool(cmds.checkBox(self.controls["write_sidecar"], query=True, value=True))
            set_errors = [issue for issue in analysis.get("issues", []) if issue.severity == "ERROR"]
            doctor_options = self._doctor_options()
            report_nodes = [
                node for node in nodes
                if get_mesh_shapes(node)
                and str(_maya_get_attr(node, MAYA_ASSET_ROLE_ATTR, "") or "") in {ROLE_RENDER, ROLE_LOD}
            ]
            reports = [maya_asset_doctor_analyze_object(node, **doctor_options) for node in report_nodes] if (validate or write_sidecar) else []
            report_errors = [issue for report in reports for issue in report.issues if issue.severity == "ERROR"]
            if validate and block and (set_errors or report_errors):
                raise RuntimeError("Export blocked: {0} set error(s), {1} asset error(s).".format(len(set_errors), len(report_errors)))

            export_folder = cmds.textFieldButtonGrp(self.controls["export_path"], query=True, text=True)
            plan = build_export_plan(
                export_folder, root, self.get_export_profile_id(),
                [_short_name(node) for node in nodes], write_sidecar,
            )
            if plan.get("has_conflict") and not cmds.checkBox(self.controls["overwrite_existing"], query=True, value=True):
                raise RuntimeError("Export or sidecar target already exists; enable overwrite or change the target")
            original_selection = cmds.ls(selection=True, long=True) or []
            moved = {}
            try:
                if cmds.checkBox(self.controls["asset_move_origin"], query=True, value=True):
                    primary = maya_asset_set_primary(nodes)
                    if primary:
                        pivot = cmds.xform(primary, query=True, worldSpace=True, rotatePivot=True)
                        node_set = set(_long_name(node) for node in nodes)
                        top_nodes = []
                        for node in nodes:
                            parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
                            if not parents or _long_name(parents[0]) not in node_set:
                                top_nodes.append(node)
                        for node in top_nodes:
                            translation = cmds.xform(node, query=True, worldSpace=True, translation=True)
                            moved[node] = translation
                            cmds.xform(
                                node, worldSpace=True,
                                translation=(translation[0] - pivot[0], translation[1] - pivot[1], translation[2] - pivot[2]),
                            )

                cmds.select(nodes, replace=True)
                filepath = export_selected_file(
                    export_folder,
                    root,
                    validate_before_export=False,
                    block_on_validation_errors=False,
                    include_uv_checks=False,
                    asset_doctor_options=doctor_options,
                    export_profile_id=self.get_export_profile_id(),
                )
                if write_sidecar:
                    write_export_sidecar(
                        filepath,
                        reports,
                        source_host="Maya",
                        profile_id=self.get_asset_doctor_profile_id(),
                        suite_version="2.4.0",
                        metadata={"export_mode": "asset_set"},
                        asset_set=maya_asset_set_sidecar_data(analysis),
                        export_profile_id=self.get_export_profile_id(),
                    )
            finally:
                for node, translation in moved.items():
                    if cmds.objExists(node):
                        cmds.xform(node, worldSpace=True, translation=translation)
                if original_selection:
                    cmds.select(original_selection, replace=True)
                else:
                    cmds.select(clear=True)

            self.refresh_asset_set_output(analysis)
            self.set_status("Exported Asset Set {0} ({1} member(s)).".format(root, len(nodes)))
            self.save_settings()
        except Exception as exc:
            self.set_status("Asset Set export failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_rename_selected(self) -> None:
        try:
            prefix = (
                cmds.textFieldGrp(
                    self.controls["rs_prefix"],
                    query=True,
                    text=True,
                ).strip()
                or "SM"
            )

            item_name = (
                cmds.textFieldGrp(
                    self.controls["rs_name"],
                    query=True,
                    text=True,
                ).strip()
                or "Prop"
            )

            start_index = cmds.intFieldGrp(
                self.controls["rs_start"],
                query=True,
                value1=True,
            )

            rename_shapes = cmds.checkBox(
                self.controls["rs_shapes"],
                query=True,
                value=True,
            )

            renamed = rename_selected(
                prefix,
                item_name,
                start_index=start_index,
                rename_shapes=rename_shapes,
            )

            self.set_status(
                "Renamed {0} object(s).".format(
                    len(renamed)
                )
            )

            self.refresh_budget_marker()

        except Exception as exc:
            self.set_status(
                "Rename failed: {0}".format(exc)
            )
            cmds.warning(str(exc))

    def refresh_budget_marker(self) -> None:
        try:
            meshes = get_selected_mesh_transforms()

            if not meshes:
                cmds.text(
                    self.controls["budget_marker"],
                    edit=True,
                    label="Poly budget: No mesh selected",
                )
                return

            mode = self.get_count_mode()

            count = get_selection_metric(
                mode,
                count_material_splits=self.get_count_material_splits(),
            )

            classification = classify_budget(
                count,
                self.get_budget(),
                self.get_margin(),
                self.get_lp_prefix(),
                self.get_hp_prefix(),
            )

            auto_prefix = cmds.checkBox(
                self.controls["auto_prefix"],
                query=True,
                value=True,
            )

            if auto_prefix:
                label = "{0} · {1:,}/{2:,} {3} · Export prefix {4}_".format(
                    classification["label"],
                    count,
                    classification["limit"],
                    mode.lower(),
                    classification["prefix"],
                )
            else:
                label = "{0} · {1:,}/{2:,} {3}".format(
                    classification["label"],
                    count,
                    classification["limit"],
                    mode.lower(),
                )

            cmds.text(
                self.controls["budget_marker"],
                edit=True,
                label=label,
            )

        except Exception as exc:
            cmds.text(
                self.controls["budget_marker"],
                edit=True,
                label="Budget marker error: {0}".format(exc),
            )

    def on_move_pivot(self) -> None:
        try:
            x_axis = cmds.optionMenuGrp(
                self.controls["x_axis"],
                query=True,
                value=True,
            )

            y_axis = cmds.optionMenuGrp(
                self.controls["y_axis"],
                query=True,
                value=True,
            )

            z_axis = cmds.optionMenuGrp(
                self.controls["z_axis"],
                query=True,
                value=True,
            )

            individual = cmds.checkBox(
                self.controls["individual"],
                query=True,
                value=True,
            )

            moved = set_selected_pivots(
                x_axis,
                y_axis,
                z_axis,
                individual=individual,
            )

            self.set_status(
                "Moved pivot on {0} object(s).".format(moved)
            )

        except Exception as exc:
            self.set_status(
                "Move pivot failed: {0}".format(exc)
            )
            cmds.warning(str(exc))

    def on_pivot_to_selected_vertex(self) -> None:
        try:
            moved = move_pivot_to_selected_vertices()

            self.set_status(
                "Moved pivot to selected vertex position on {0} object(s)".format(moved)
            )
        except Exception as exc:
            self.set_status(
                "Move pivot to vertex failed: {0}".format(exc)
            )
            cmds.warning(str(exc))

    def on_validate(self) -> None:
        try:
            select_first = cmds.checkBox(
                self.controls["select_issue"],
                query=True,
                value=True,
            )

            include_uv = cmds.checkBox(
                self.controls["uv_checks"],
                query=True,
                value=True,
            )

            problems, message = validate_selection(
                select_first_issue=select_first,
                include_uv_checks=include_uv,
            )

            self.set_validation_message(message)

            if problems:
                self.set_status(
                    "Validation found issues in {0} object(s).".format(
                        len(problems)
                    )
                )
                cmds.warning(message)
            else:
                self.set_status(message)

        except Exception as exc:
            self.set_validation_message(
                "Validation failed: {0}".format(exc)
            )
            self.set_status(
                "Validation failed: {0}".format(exc)
            )
            cmds.warning(str(exc))

    def on_combine(self) -> None:
        try:
            allowed = cmds.checkBox(
                self.controls["allow_combine"],
                query=True,
                value=True,
            )

            if not allowed:
                raise RuntimeError(
                    "Enable 'Allow destructive combine' first."
                )

            combined = combine_selected_meshes()

            self.set_status(
                "Combined selected meshes into {0}.".format(
                    _short_name(combined)
                    if combined
                    else "new mesh"
                )
            )

            self.refresh_budget_marker()

        except Exception as exc:
            self.set_status(
                "Combine failed: {0}".format(exc)
            )
            cmds.warning(str(exc))

    def on_browse_export_path(self) -> None:
        folder = cmds.fileDialog2(
            fileMode=3,
            caption="Choose Export Folder",
        )

        if folder:
            cmds.textFieldButtonGrp(
                self.controls["export_path"],
                edit=True,
                text=folder[0],
            )

    def on_export(self) -> None:
        try:
            meshes = get_selected_mesh_transforms()

            if not meshes:
                raise RuntimeError("No mesh transforms selected.")

            mode = self.get_count_mode()
            count_splits = self.get_count_material_splits()

            auto_prefix = cmds.checkBox(
                self.controls["auto_prefix"], query=True, value=True,
            )
            batch_mode = cmds.checkBox(
                self.controls["batch_mode"], query=True, value=True,
            )
            move_origin = cmds.checkBox(
                self.controls["move_origin"], query=True, value=True,
            )
            write_sidecar = cmds.checkBox(
                self.controls["write_sidecar"], query=True, value=True,
            )
            overwrite_existing = cmds.checkBox(
                self.controls["overwrite_existing"], query=True, value=True,
            )
            include_uv = cmds.checkBox(
                self.controls["uv_checks"], query=True, value=True,
            )
            validate = cmds.checkBox(
                self.controls["validate_export"], query=True, value=True,
            )
            block = cmds.checkBox(
                self.controls["block_export"], query=True, value=True,
            )

            export_folder = cmds.textFieldButtonGrp(
                self.controls["export_path"], query=True, text=True,
            )

            doctor_options = {
                "profile_id": self.get_asset_doctor_profile_id(),
                "budget_mode": mode,
                "budget": self.get_budget(),
                "margin_percent": self.get_margin(),
                "count_material_splits": count_splits,
                "enforce_budget": self.get_asset_doctor_enforce_budget(),
                "td_texture_size": self.get_td_texture_size(),
                "td_target_px_per_m": maya_density_from_display(self.get_td_target(), self.get_td_unit()),
                "td_tolerance_percent": self.get_td_tolerance(),
                "enforce_td_target": self.get_asset_doctor_enforce_td(),
            }

            if batch_mode:
                exported = []
                if not overwrite_existing:
                    conflicts = []
                    for obj in meshes:
                        count = get_mesh_metric(obj, mode, count_material_splits=count_splits)
                        name = self._export_name_for(obj, count, auto_prefix)
                        plan = build_export_plan(export_folder, name, self.get_export_profile_id(), [_short_name(obj)], write_sidecar)
                        if plan.get("has_conflict"):
                            conflicts.append(plan["filepath"])
                    if conflicts:
                        raise RuntimeError("Batch export blocked: {0} target file(s) already exist".format(len(conflicts)))

                for obj in meshes:
                    count = get_mesh_metric(
                        obj, mode, count_material_splits=count_splits,
                    )

                    filepath = export_object_file(
                        obj,
                        export_folder,
                        self._export_name_for(obj, count, auto_prefix),
                        move_to_origin=move_origin,
                        validate_before_export=validate,
                        block_on_validation_errors=block,
                        include_uv_checks=include_uv,
                        asset_doctor_options=doctor_options,
                        export_profile_id=self.get_export_profile_id(),
                    )

                    if write_sidecar:
                        report = maya_asset_doctor_analyze_object(obj, **doctor_options)
                        write_export_sidecar(
                            filepath,
                            [report],
                            source_host="Maya",
                            profile_id=self.get_asset_doctor_profile_id(),
                            suite_version="2.4.0",
                            export_profile_id=self.get_export_profile_id(),
                        )

                    exported.append(filepath)

                self.set_status(
                    "Batch exported {0} file(s) to {1}".format(
                        len(exported), export_folder,
                    )
                )

            else:
                active = get_active_mesh_transform() or meshes[0]

                count = get_selection_metric(
                    mode, count_material_splits=count_splits,
                )

                export_name = self._export_name_for(active, count, auto_prefix)
                plan = build_export_plan(
                    export_folder, export_name, self.get_export_profile_id(),
                    [_short_name(obj) for obj in meshes], write_sidecar,
                )
                if plan.get("has_conflict") and not overwrite_existing:
                    raise RuntimeError("Export or sidecar target already exists; enable overwrite or change the target")

                filepath = export_selected_file(
                    export_folder,
                    export_name,
                    validate_before_export=validate,
                    block_on_validation_errors=block,
                    include_uv_checks=include_uv,
                    asset_doctor_options=doctor_options,
                    export_profile_id=self.get_export_profile_id(),
                )

                if write_sidecar:
                    reports = [
                        maya_asset_doctor_analyze_object(obj, **doctor_options)
                        for obj in meshes
                    ]
                    write_export_sidecar(
                        filepath,
                        reports,
                        source_host="Maya",
                        profile_id=self.get_asset_doctor_profile_id(),
                        suite_version="2.4.0",
                        export_profile_id=self.get_export_profile_id(),
                    )

                self.set_status(
                    "Exported {0} with {1:,} {2}.".format(
                        os.path.basename(filepath), count, mode.lower(),
                    )
                )

            self.save_settings()

        except Exception as exc:
            self.set_status(
                "Export failed: {0}".format(exc)
            )
            cmds.warning(str(exc))

    def _export_name_for(self, obj: str, count: int, auto_prefix: bool) -> str:
        base_name = _short_name(obj)

        if not auto_prefix:
            return _strip_namespace(base_name)

        export_name, _classification = export_name_with_prefix(
            base_name,
            count,
            self.get_budget(),
            self.get_margin(),
            self.get_lp_prefix(),
            self.get_hp_prefix(),
        )

        return export_name

    def on_check_texel_density(self) -> None:
        try:
            texture_size = cmds.intFieldGrp(
                self.controls["td_size"], query=True, value1=True,
            )

            unit = self.get_td_unit()
            density = get_selection_texel_density(texture_size, unit=unit)

            if density is None:
                message = "TD: no UV or surface area found on selection"
            else:
                message = "TD: {0:.3f} {1} at {2}px textures".format(
                    density, unit, texture_size,
                )

            cmds.text(
                self.controls["td_result"],
                edit=True,
                label=message,
            )

            self.set_status(message)

        except Exception as exc:
            self.set_status(
                "Texel density check failed: {0}".format(exc)
            )
            cmds.warning(str(exc))

    def on_set_texel_density(self) -> None:
        try:
            texture_size = cmds.intFieldGrp(
                self.controls["td_size"], query=True, value1=True,
            )
            target = cmds.floatFieldGrp(
                self.controls["td_target"], query=True, value1=True,
            )

            adjusted = set_selection_texel_density(
                target, texture_size, unit=self.get_td_unit()
            )

            self.set_status(
                "Adjusted texel density on {0} object(s).".format(adjusted)
            )

            self.on_check_texel_density()

        except Exception as exc:
            self.set_status(
                "Set texel density failed: {0}".format(exc)
            )
            cmds.warning(str(exc))

    def on_td_heatmap(self) -> None:
        try:
            meshes = get_selected_mesh_transforms()
            if not meshes:
                raise RuntimeError("Select one or more mesh assets first")
            target_px_per_m = maya_density_from_display(
                self.get_td_target(), self.get_td_unit()
            )
            colored = sum(
                maya_apply_td_heatmap(
                    obj, self.get_td_texture_size(), target_px_per_m
                )
                for obj in meshes
            )
            self.set_status("Texel-density heatmap applied to {0} face(s).".format(colored))
        except Exception as exc:
            self.set_status("TD heatmap failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_clear_td_heatmap(self) -> None:
        try:
            cleared = sum(
                maya_clear_td_heatmap(obj) for obj in get_selected_mesh_transforms()
            )
            self.set_status("Cleared TD heatmap from {0} mesh shape(s).".format(cleared))
        except Exception as exc:
            self.set_status("Clear heatmap failed: {0}".format(exc))
            cmds.warning(str(exc))

    def on_create_ik(self) -> None:
        try:
            multiplier = cmds.floatFieldGrp(
                self.controls["pole_mult"], query=True, value1=True,
            )

            handle, locator = create_ik_with_pole_vector(
                pole_distance=multiplier,
            )

            self.set_status(
                "Created {0} with pole vector {1}.".format(
                    _short_name(handle), _short_name(locator),
                )
            )

        except Exception as exc:
            self.set_status(
                "Create IK failed: {0}".format(exc)
            )
            cmds.warning(str(exc))

    def on_create_muscle(self) -> None:
        try:
            axis = cmds.optionMenuGrp(
                self.controls["muscle_axis"], query=True, value=True,
            )
            max_angle = cmds.floatFieldGrp(
                self.controls["muscle_angle"], query=True, value1=True,
            )
            bulge = cmds.floatFieldGrp(
                self.controls["muscle_bulge"], query=True, value1=True,
            )

            helper = create_muscle_helper(
                rotate_axis=axis,
                max_angle=max_angle,
                bulge_scale=bulge,
            )

            self.set_status(
                "Created {0}. Add it to the skinCluster and paint "
                "weights to see the bulge.".format(_short_name(helper))
            )

        except Exception as exc:
            self.set_status(
                "Create muscle helper failed: {0}".format(exc)
            )
            cmds.warning(str(exc))


_UI_INSTANCE: Optional[JAMTAToolsUI] = None


def _restore_docked_workspace() -> None:
    """Workspace-control restore hook; Maya calls this from the saved workspace state."""
    global _UI_INSTANCE
    if _UI_INSTANCE is None:
        _UI_INSTANCE = JAMTAToolsUI()
    if cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True) and not _UI_INSTANCE.controls:
        _UI_INSTANCE._build_content(WORKSPACE_CONTROL_NAME, WORKSPACE_CONTROL_NAME)


def show(docked: bool = True) -> JAMTAToolsUI:
    """Open JAM TA Tools. Docked workspace mode is the production default."""
    global _UI_INSTANCE
    _UI_INSTANCE = JAMTAToolsUI()
    if docked:
        _UI_INSTANCE.show_docked()
    else:
        _UI_INSTANCE.show()
    return _UI_INSTANCE


if __name__ == "__main__":
    show()
