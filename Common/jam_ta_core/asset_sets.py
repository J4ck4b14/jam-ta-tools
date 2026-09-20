"""Host-independent Asset Set naming, structure and LOD/collision validation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import ValidationIssue

ROLE_RENDER = "RENDER"
ROLE_LOD = "LOD"
ROLE_COLLISION = "COLLISION"
ROLE_SOCKET = "SOCKET"
ROLE_SKELETON = "SKELETON"
ROLE_HELPER = "HELPER"

COLLISION_PREFIXES = {
    "UCX": "CONVEX",
    "UBX": "BOX",
    "USP": "SPHERE",
    "UCP": "CAPSULE",
}

_LOD_RE = re.compile(r"^(?P<root>.+?)_LOD(?P<level>\d+)$", re.IGNORECASE)
_COLLISION_RE = re.compile(
    r"^(?P<prefix>UCX|UBX|USP|UCP)_(?P<root>.+?)(?:_(?P<index>\d+))?$",
    re.IGNORECASE,
)
_SOCKET_RE = re.compile(r"^(?:SOCKET|SOCK)_(?P<root>.+?)(?:_(?P<label>[^_]+))?$", re.IGNORECASE)


@dataclass
class AssetMemberSpec:
    name: str
    root_name: str
    role: str = ROLE_RENDER
    lod_level: int = 0
    collision_type: str = ""
    export_enabled: bool = True
    triangles: int = 0
    is_closed: Optional[bool] = None
    is_convex: Optional[bool] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def strip_namespace(name: str) -> str:
    """Strip Maya namespaces and hierarchy while leaving Blender names untouched."""
    leaf = str(name).split("|")[-1]
    return leaf.split(":")[-1]


def infer_asset_member(name: str, fallback_root: str = "") -> AssetMemberSpec:
    """Infer an Asset Set role from common game-art naming conventions.

    This is intentionally only a bootstrap mechanism. DCC integrations persist
    explicit membership after an Asset Set is created, so later renames do not
    silently redefine the asset.
    """

    clean = strip_namespace(name)

    collision = _COLLISION_RE.match(clean)
    if collision:
        prefix = collision.group("prefix").upper()
        return AssetMemberSpec(
            name=clean,
            root_name=collision.group("root"),
            role=ROLE_COLLISION,
            collision_type=COLLISION_PREFIXES[prefix],
        )

    lod = _LOD_RE.match(clean)
    if lod:
        level = int(lod.group("level"))
        return AssetMemberSpec(
            name=clean,
            root_name=lod.group("root"),
            role=ROLE_RENDER if level == 0 else ROLE_LOD,
            lod_level=level,
        )

    socket = _SOCKET_RE.match(clean)
    if socket:
        return AssetMemberSpec(
            name=clean,
            root_name=socket.group("root") or fallback_root or clean,
            role=ROLE_SOCKET,
        )

    return AssetMemberSpec(
        name=clean,
        root_name=fallback_root or clean,
        role=ROLE_RENDER,
        lod_level=0,
    )


def canonical_root_from_members(names: Sequence[str], active_name: str = "") -> str:
    """Choose a stable root from a selection using active/render members first."""
    if active_name:
        inferred = infer_asset_member(active_name)
        if inferred.role not in {ROLE_COLLISION, ROLE_SOCKET}:
            return inferred.root_name

    inferred_members = [infer_asset_member(name) for name in names]
    render = [member for member in inferred_members if member.role == ROLE_RENDER]
    if render:
        return render[0].root_name
    lods = [member for member in inferred_members if member.role == ROLE_LOD]
    if lods:
        return lods[0].root_name
    collisions = [member for member in inferred_members if member.role == ROLE_COLLISION]
    if collisions:
        return collisions[0].root_name
    return strip_namespace(active_name or (names[0] if names else "Asset"))


def normalize_member_for_root(member: AssetMemberSpec, root_name: str) -> AssetMemberSpec:
    """Attach helper/socket members to an explicitly chosen root without changing role."""
    member.root_name = root_name
    return member


def analyze_asset_set(
    root_name: str,
    members: Iterable[AssetMemberSpec | Dict[str, Any]],
    min_lod_reduction_percent: float = 20.0,
    require_contiguous_lods: bool = True,
    collision_triangle_budget: Optional[int] = None,
) -> Dict[str, Any]:
    """Validate set structure and return metrics/issues without host assumptions."""

    normalized: List[AssetMemberSpec] = []
    for member in members:
        if isinstance(member, AssetMemberSpec):
            normalized.append(member)
        else:
            normalized.append(AssetMemberSpec(**dict(member)))

    issues: List[ValidationIssue] = []
    render_members = [m for m in normalized if m.role in {ROLE_RENDER, ROLE_LOD} and m.export_enabled]
    collision_members = [m for m in normalized if m.role == ROLE_COLLISION and m.export_enabled]
    sockets = [m for m in normalized if m.role == ROLE_SOCKET and m.export_enabled]
    helpers = [m for m in normalized if m.role == ROLE_HELPER and m.export_enabled]

    lod_by_level: Dict[int, List[AssetMemberSpec]] = {}
    for member in render_members:
        lod_by_level.setdefault(max(0, int(member.lod_level)), []).append(member)

    if not lod_by_level.get(0):
        issues.append(ValidationIssue(
            "asset_set.missing_lod0",
            "Missing base render mesh",
            "Asset Set has no export-enabled LOD0/base render member.",
            "Asset Set",
            "ERROR",
            object_name=root_name,
        ))

    levels = sorted(lod_by_level)
    if require_contiguous_lods and levels:
        expected = list(range(0, max(levels) + 1))
        missing = [level for level in expected if level not in lod_by_level]
        if missing:
            issues.append(ValidationIssue(
                "asset_set.lod_gaps",
                "LOD chain has gaps",
                "Missing LOD level(s): {0}.".format(", ".join("LOD{0}".format(level) for level in missing)),
                "LOD",
                "WARNING",
                object_name=root_name,
                metadata={"missing_levels": missing},
            ))

    lod_metrics: List[Dict[str, Any]] = []
    previous_triangles: Optional[int] = None
    for level in levels:
        level_members = lod_by_level[level]
        triangles = sum(max(0, int(member.triangles)) for member in level_members)
        reduction = None
        if previous_triangles is not None and previous_triangles > 0:
            reduction = max(0.0, (1.0 - (float(triangles) / previous_triangles)) * 100.0)
            if triangles >= previous_triangles:
                issues.append(ValidationIssue(
                    "asset_set.lod_not_reduced",
                    "LOD triangle count does not decrease",
                    "LOD{0} totals {1:,} tris across {2} member(s); previous level has {3:,}.".format(
                        level, triangles, len(level_members), previous_triangles
                    ),
                    "LOD",
                    "ERROR",
                    object_name=root_name,
                    metadata={"lod_level": level, "triangles": triangles, "previous_triangles": previous_triangles},
                ))
            elif reduction < float(min_lod_reduction_percent):
                issues.append(ValidationIssue(
                    "asset_set.lod_weak_reduction",
                    "LOD reduction is small",
                    "LOD{0} reduces aggregate triangles by {1:.1f}%; configured minimum is {2:.1f}%.".format(
                        level, reduction, float(min_lod_reduction_percent)
                    ),
                    "LOD",
                    "WARNING",
                    object_name=root_name,
                    metadata={"lod_level": level, "reduction_percent": reduction},
                ))
        lod_metrics.append({
            "level": level,
            "member_count": len(level_members),
            "members": [member.name for member in level_members],
            "triangles": triangles,
            "reduction_from_previous_percent": reduction,
        })
        previous_triangles = triangles

    collision_triangles = sum(max(0, int(member.triangles)) for member in collision_members)
    if collision_triangle_budget is not None and collision_triangles > int(collision_triangle_budget):
        issues.append(ValidationIssue(
            "asset_set.collision_budget",
            "Collision triangle budget exceeded",
            "Collision members total {0:,} tris; configured limit is {1:,}.".format(
                collision_triangles, int(collision_triangle_budget)
            ),
            "Collision",
            "WARNING",
            object_name=root_name,
            metadata={"triangles": collision_triangles, "limit": int(collision_triangle_budget)},
        ))

    for member in collision_members:
        if member.collision_type == "CONVEX":
            if member.is_closed is False:
                issues.append(ValidationIssue(
                    "asset_set.collision_open",
                    "Custom collision is open",
                    "{0} is marked as convex collision but is not watertight.".format(member.name),
                    "Collision",
                    "ERROR",
                    object_name=member.name,
                ))
            if member.is_convex is False:
                issues.append(ValidationIssue(
                    "asset_set.collision_nonconvex",
                    "Custom collision is not convex",
                    "{0} is marked as convex collision but contains concavity.".format(member.name),
                    "Collision",
                    "ERROR",
                    object_name=member.name,
                ))

    export_members = [m for m in normalized if m.export_enabled]
    metrics = {
        "member_count": len(normalized),
        "export_member_count": len(export_members),
        "render_member_count": len(render_members),
        "lod_count": len(levels),
        "lod_levels": levels,
        "lod_metrics": lod_metrics,
        "collision_count": len(collision_members),
        "collision_triangles": collision_triangles,
        "socket_count": len(sockets),
        "helper_count": len(helpers),
        "total_render_triangles": sum(max(0, int(m.triangles)) for m in render_members),
    }

    return {
        "root_name": root_name,
        "members": [member.to_dict() for member in normalized],
        "metrics": metrics,
        "issues": issues,
        "status": "ERROR" if any(i.severity == "ERROR" for i in issues) else (
            "WARNING" if any(i.severity == "WARNING" for i in issues) else "CLEAN"
        ),
    }
