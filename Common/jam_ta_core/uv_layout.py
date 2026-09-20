"""Host-independent UV layout diagnostics.

Hosts hand over face vertex ids + UV coordinates. The shared side then builds UV islands,
looks for cross-island overlap, estimates 0-1 utilisation and measures the smallest island
padding. It stays geometry-only on purpose; no Blender/Maya objects cross this boundary.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

Point = Tuple[float, float]
_EPS = 1e-9


def _signed_area(points: Sequence[Point]) -> float:
    area = 0.0
    for i, point in enumerate(points):
        nxt = points[(i + 1) % len(points)]
        area += point[0] * nxt[1] - nxt[0] * point[1]
    return area * 0.5


def _cross(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _point_in_triangle(point: Point, a: Point, b: Point, c: Point, eps: float = _EPS) -> bool:
    c1 = _cross(a, b, point)
    c2 = _cross(b, c, point)
    c3 = _cross(c, a, point)
    return c1 >= -eps and c2 >= -eps and c3 >= -eps


def _triangulate_polygon(points: Sequence[Point]) -> List[Tuple[Point, Point, Point]]:
    """Ear-clip a simple UV polygon; use a fan only if malformed data defeats clipping."""
    if len(points) < 3:
        return []
    clean = [(float(p[0]), float(p[1])) for p in points]
    order = list(range(len(clean)))
    if _signed_area(clean) < 0.0:
        order.reverse()

    triangles: List[Tuple[Point, Point, Point]] = []
    guard = 0
    while len(order) > 3 and guard < len(clean) * len(clean):
        guard += 1
        clipped = False
        for cursor, current in enumerate(order):
            prev = order[cursor - 1]
            nxt = order[(cursor + 1) % len(order)]
            a, b, c = clean[prev], clean[current], clean[nxt]
            if _cross(a, b, c) <= _EPS:
                continue
            if any(
                idx not in {prev, current, nxt} and _point_in_triangle(clean[idx], a, b, c)
                for idx in order
            ):
                continue
            triangles.append((a, b, c))
            del order[cursor]
            clipped = True
            break
        if not clipped:
            break

    if len(order) == 3:
        a, b, c = (clean[index] for index in order)
        if abs(_cross(a, b, c)) > _EPS:
            triangles.append((a, b, c))

    if triangles:
        return triangles

    # Rare broken/self-crossing polygons still deserve a diagnostic rather than no data.
    return [
        (clean[0], clean[i], clean[i + 1])
        for i in range(1, len(clean) - 1)
        if abs(_cross(clean[0], clean[i], clean[i + 1])) > _EPS
    ]


def _same_point(a: Point, b: Point, tolerance: float) -> bool:
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def _clip_polygon(subject: Sequence[Point], clipper: Sequence[Point]) -> List[Point]:
    """Sutherland-Hodgman clipping. ``clipper`` must be counter-clockwise."""
    output = list(subject)
    for i, a in enumerate(clipper):
        b = clipper[(i + 1) % len(clipper)]
        input_points = output
        output = []
        if not input_points:
            break
        start = input_points[-1]
        for end in input_points:
            end_inside = _cross(a, b, end) >= -_EPS
            start_inside = _cross(a, b, start) >= -_EPS
            if end_inside != start_inside:
                dx1, dy1 = end[0] - start[0], end[1] - start[1]
                dx2, dy2 = b[0] - a[0], b[1] - a[1]
                denominator = dx1 * dy2 - dy1 * dx2
                if abs(denominator) > _EPS:
                    t = ((a[0] - start[0]) * dy2 - (a[1] - start[1]) * dx2) / denominator
                    output.append((start[0] + t * dx1, start[1] + t * dy1))
            if end_inside:
                output.append(end)
            start = end
    return output


def _triangle_intersection_area(a: Sequence[Point], b: Sequence[Point]) -> float:
    aa = list(a)
    bb = list(b)
    if _signed_area(aa) < 0.0:
        aa.reverse()
    if _signed_area(bb) < 0.0:
        bb.reverse()
    clipped = _clip_polygon(aa, bb)
    return abs(_signed_area(clipped)) if len(clipped) >= 3 else 0.0


def _aabb(points: Sequence[Point]) -> Tuple[float, float, float, float]:
    us = [p[0] for p in points]
    vs = [p[1] for p in points]
    return min(us), min(vs), max(us), max(vs)


def _aabb_overlap(a, b, eps=_EPS) -> bool:
    return not (a[2] <= b[0] + eps or b[2] <= a[0] + eps or a[3] <= b[1] + eps or b[3] <= a[1] + eps)


def _point_segment_distance(p: Point, a: Point, b: Point) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    length2 = vx * vx + vy * vy
    if length2 <= _EPS:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / length2
    t = max(0.0, min(1.0, t))
    q = (a[0] + t * vx, a[1] + t * vy)
    return math.hypot(p[0] - q[0], p[1] - q[1])


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    o1, o2 = _cross(a, b, c), _cross(a, b, d)
    o3, o4 = _cross(c, d, a), _cross(c, d, b)
    return (o1 * o2 < -_EPS) and (o3 * o4 < -_EPS)


def _segment_distance(a: Point, b: Point, c: Point, d: Point) -> float:
    if _segments_intersect(a, b, c, d):
        return 0.0
    return min(
        _point_segment_distance(a, c, d), _point_segment_distance(b, c, d),
        _point_segment_distance(c, a, b), _point_segment_distance(d, a, b),
    )


def _bbox_distance(a, b) -> float:
    dx = max(0.0, a[0] - b[2], b[0] - a[2])
    dy = max(0.0, a[1] - b[3], b[1] - a[3])
    return math.hypot(dx, dy)


class _UnionFind:
    def __init__(self, count: int):
        self.parent = list(range(count))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def analyze_uv_layout(
    faces: Iterable[Mapping[str, Any]],
    texture_size: int = 2048,
    tolerance: float = 1e-6,
) -> Dict[str, Any]:
    """Analyze one UV channel and return island/overlap/utilisation/padding facts."""
    prepared = []
    for raw in faces:
        vertices = [int(v) for v in raw.get("vertices", [])]
        uvs = [(float(p[0]), float(p[1])) for p in raw.get("uvs", [])]
        if len(vertices) < 3 or len(vertices) != len(uvs):
            continue
        prepared.append({"component": raw.get("component"), "vertices": vertices, "uvs": uvs})

    if not prepared:
        return {
            "face_count": 0, "island_count": 0, "islands": [], "overlap_components": [],
            "overlap_pair_count": 0, "overlap_area": 0.0, "utilization_percent": 0.0,
            "raw_area_percent": 0.0, "min_island_padding_uv": 0.0, "min_island_padding_px": 0.0,
        }

    union = _UnionFind(len(prepared))
    edge_uses: Dict[Tuple[int, int], List[Tuple[int, Dict[int, Point]]]] = {}
    for face_index, face in enumerate(prepared):
        vertices, uvs = face["vertices"], face["uvs"]
        for i, va in enumerate(vertices):
            vb = vertices[(i + 1) % len(vertices)]
            key = tuple(sorted((va, vb)))
            edge_uses.setdefault(key, []).append((
                face_index,
                {va: uvs[i], vb: uvs[(i + 1) % len(vertices)]},
            ))

    for uses in edge_uses.values():
        for i in range(len(uses)):
            face_a, uv_a = uses[i]
            for j in range(i + 1, len(uses)):
                face_b, uv_b = uses[j]
                shared = set(uv_a).intersection(uv_b)
                if len(shared) != 2:
                    continue
                if all(_same_point(uv_a[v], uv_b[v], tolerance) for v in shared):
                    union.union(face_a, face_b)

    roots: Dict[int, List[int]] = {}
    for face_index in range(len(prepared)):
        roots.setdefault(union.find(face_index), []).append(face_index)
    ordered_roots = sorted(roots, key=lambda root: min(roots[root]))
    island_id_for_face = {}
    for island_id, root in enumerate(ordered_roots):
        for face_index in roots[root]:
            island_id_for_face[face_index] = island_id

    islands = []
    boundary_by_island: Dict[int, List[Tuple[Point, Point]]] = {}
    triangles = []
    raw_area = 0.0
    area_in_01 = 0.0
    unit_square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]

    for island_id, root in enumerate(ordered_roots):
        face_indices = roots[root]
        points = [point for idx in face_indices for point in prepared[idx]["uvs"]]
        signed_areas = [_signed_area(prepared[idx]["uvs"]) for idx in face_indices]
        area = sum(abs(value) for value in signed_areas)
        signed_area = sum(signed_areas)
        mirrored = bool(signed_areas) and all(value < -_EPS for value in signed_areas if abs(value) > _EPS)
        mixed_winding = any(value > _EPS for value in signed_areas) and any(value < -_EPS for value in signed_areas)
        raw_area += area

        # Count UV edges in this island; one occurrence means an island boundary.
        uv_edges: Dict[Tuple[Tuple[int, int], Tuple[int, int]], Tuple[Point, Point, int]] = {}
        edge_counts: Dict[Tuple[Tuple[int, int], Tuple[int, int]], int] = {}
        quant = max(tolerance, 1e-8)
        for idx in face_indices:
            face = prepared[idx]
            uvs = face["uvs"]
            for i, start in enumerate(uvs):
                end = uvs[(i + 1) % len(uvs)]
                qa = (round(start[0] / quant), round(start[1] / quant))
                qb = (round(end[0] / quant), round(end[1] / quant))
                key = tuple(sorted((qa, qb)))
                edge_counts[key] = edge_counts.get(key, 0) + 1
                uv_edges[key] = (start, end, idx)
        boundary_by_island[island_id] = [
            (edge[0], edge[1]) for key, edge in uv_edges.items() if edge_counts.get(key, 0) == 1
        ]

        components = [prepared[idx]["component"] for idx in face_indices]
        islands.append({
            "island_id": island_id,
            "components": components,
            "face_count": len(face_indices),
            "uv_area": area,
            "signed_uv_area": signed_area,
            "mirrored": mirrored,
            "mixed_winding": mixed_winding,
            "bounds": _aabb(points),
        })

        for idx in face_indices:
            face = prepared[idx]
            for tri in _triangulate_polygon(face["uvs"]):
                tri_area = abs(_signed_area(tri))
                clipped = _clip_polygon(list(tri), unit_square)
                if len(clipped) >= 3:
                    area_in_01 += abs(_signed_area(clipped))
                triangles.append({
                    "points": tri,
                    "bounds": _aabb(tri),
                    "island": island_id,
                    "component": face["component"],
                    "area": tri_area,
                })

    # Sweep on U: production layouts tend to be sparse, so this avoids N² in the common case.
    sorted_indices = sorted(range(len(triangles)), key=lambda i: triangles[i]["bounds"][0])
    active: List[int] = []
    overlap_components = set()
    overlap_pairs = set()
    overlap_area = 0.0
    overlap_area_01 = 0.0
    for tri_index in sorted_indices:
        tri = triangles[tri_index]
        active = [idx for idx in active if triangles[idx]["bounds"][2] > tri["bounds"][0] + tolerance]
        for other_index in active:
            other = triangles[other_index]
            if tri["island"] == other["island"] or not _aabb_overlap(tri["bounds"], other["bounds"], tolerance):
                continue
            area = _triangle_intersection_area(tri["points"], other["points"])
            if area <= max(_EPS, tolerance * tolerance):
                continue
            overlap_area += area
            overlap_components.add(tri["component"])
            overlap_components.add(other["component"])
            overlap_pairs.add(tuple(sorted((tri["island"], other["island"]))))

            intersection = _clip_polygon(list(tri["points"]), list(other["points"]))
            if len(intersection) >= 3:
                clipped01 = _clip_polygon(intersection, unit_square)
                if len(clipped01) >= 3:
                    overlap_area_01 += abs(_signed_area(clipped01))
        active.append(tri_index)

    min_padding = 0.0 if overlap_pairs else math.inf
    if not overlap_pairs:
        for i in range(len(islands)):
            for j in range(i + 1, len(islands)):
                if _bbox_distance(islands[i]["bounds"], islands[j]["bounds"]) >= min_padding:
                    continue
                for a, b in boundary_by_island.get(i, []):
                    for c, d in boundary_by_island.get(j, []):
                        distance = _segment_distance(a, b, c, d)
                        if distance < min_padding:
                            min_padding = distance
                            if min_padding <= tolerance:
                                break
                    if min_padding <= tolerance:
                        break
    if not math.isfinite(min_padding):
        min_padding = 0.0

    # Pairwise subtraction is intentionally an estimate if three+ islands all overlap.
    # The overlap list is the authoritative diagnostic; utilisation is a packing signal.
    estimated_coverage = max(0.0, min(1.0, area_in_01 - overlap_area_01))
    return {
        "face_count": len(prepared),
        "island_count": len(islands),
        "mirrored_island_count": sum(1 for island in islands if island.get("mirrored")),
        "mixed_winding_island_count": sum(1 for island in islands if island.get("mixed_winding")),
        "islands": islands,
        "overlap_components": sorted((c for c in overlap_components if c is not None), key=str),
        "overlap_pair_count": len(overlap_pairs),
        "overlap_area": overlap_area,
        "utilization_percent": estimated_coverage * 100.0,
        "raw_area_percent": raw_area * 100.0,
        "min_island_padding_uv": min_padding,
        "min_island_padding_px": min_padding * max(1, int(texture_size)),
    }


def summarize_island_texel_density(
    texel_samples: Iterable[Mapping[str, Any]],
    islands: Iterable[Mapping[str, Any]],
    texture_size: int,
) -> List[Dict[str, Any]]:
    """Aggregate existing per-face TD samples by UV island."""
    from .texel_analysis import face_texel_density_px_per_m

    by_component = {sample.get("component"): sample for sample in texel_samples}
    result = []
    for island in islands:
        world_area = 0.0
        uv_area = 0.0
        for component in island.get("components", []):
            sample = by_component.get(component)
            if not sample:
                continue
            world_area += float(sample.get("world_area_m2", 0.0))
            uv_area += float(sample.get("uv_area", 0.0))
        density = face_texel_density_px_per_m(world_area, uv_area, texture_size)
        result.append({
            "island_id": int(island.get("island_id", len(result))),
            "face_count": int(island.get("face_count", 0)),
            "world_area_m2": world_area,
            "uv_area": uv_area,
            "density_px_per_m": density,
            "density_px_per_cm": density / 100.0,
        })
    return result
