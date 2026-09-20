"""Host-independent core for JAM TA Tools."""

from .models import AssetReport, ValidationIssue, SEVERITY_ORDER
from .profiles import PROFILES, get_profile, profile_items, register_profile, clear_external_profiles, load_profiles_json
from .split_analysis import analyze_render_splits
from .reporting import build_report_document, write_report_json
from .sidecar import build_export_sidecar, sidecar_path_for_export, write_export_sidecar
from .cost_analysis import estimate_mesh_memory, format_bytes
from .texel_analysis import face_texel_density_px_per_m, summarize_texel_density, texel_density_color
from .material_analysis import (
    analyze_material_inventory, estimate_texture_memory, infer_texture_semantic, detect_udim, texture_set_key,
)
from .junit import build_junit_xml, write_junit_xml
from .rules import (
    register_rule, unregister_rule, clear_registered_rules, registered_rule_ids, run_registered_rules, load_rule_module,
)
from .export_profiles import EXPORT_PROFILES, get_export_profile, export_profile_items, export_extension, export_format
from .uv_layout import analyze_uv_layout, summarize_island_texel_density
from .modular import Bounds3, snap_scalar, snap_vector, multiple_delta, analyze_modular_bounds, bounds_anchor, bounds_scale_factors, make_attachment_name
from .export_plan import build_export_plan, format_export_plan
from .asset_sets import (
    AssetMemberSpec, ROLE_RENDER, ROLE_LOD, ROLE_COLLISION, ROLE_SOCKET, ROLE_SKELETON, ROLE_HELPER,
    infer_asset_member, canonical_root_from_members, normalize_member_for_root, analyze_asset_set,
)

__all__ = [
    "AssetReport",
    "ValidationIssue",
    "SEVERITY_ORDER",
    "PROFILES",
    "get_profile",
    "profile_items",
    "register_profile",
    "clear_external_profiles",
    "load_profiles_json",
    "analyze_render_splits",
    "build_report_document",
    "write_report_json",
    "build_export_sidecar",
    "sidecar_path_for_export",
    "write_export_sidecar",
    "estimate_mesh_memory",
    "format_bytes",
    "face_texel_density_px_per_m",
    "summarize_texel_density",
    "texel_density_color",
    "analyze_material_inventory",
    "estimate_texture_memory",
    "infer_texture_semantic",
    "detect_udim",
    "texture_set_key",
    "build_junit_xml",
    "write_junit_xml",
    "register_rule",
    "unregister_rule",
    "clear_registered_rules",
    "registered_rule_ids",
    "run_registered_rules",
    "load_rule_module",
    "EXPORT_PROFILES",
    "get_export_profile",
    "export_profile_items",
    "export_extension",
    "export_format",
    "analyze_uv_layout",
    "summarize_island_texel_density",
    "Bounds3",
    "snap_scalar",
    "snap_vector",
    "multiple_delta",
    "analyze_modular_bounds",
    "bounds_anchor",
    "bounds_scale_factors",
    "make_attachment_name",
    "build_export_plan",
    "format_export_plan",
    "AssetMemberSpec",
    "ROLE_RENDER",
    "ROLE_LOD",
    "ROLE_COLLISION",
    "ROLE_SOCKET",
    "ROLE_SKELETON",
    "ROLE_HELPER",
    "infer_asset_member",
    "canonical_root_from_members",
    "normalize_member_for_root",
    "analyze_asset_set",
]

__version__ = "2.4.0"
