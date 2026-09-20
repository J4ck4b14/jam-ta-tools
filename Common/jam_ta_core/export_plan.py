"""Preflight helpers for deterministic exports.

A plan is plain serializable data. UI code can show it, CI can serialize it and
an exporter can use the same target path without rebuilding the filename rules.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, Optional

from .export_profiles import export_extension, export_format, get_export_profile


def build_export_plan(
    export_folder: str,
    base_name: str,
    export_profile_id: str,
    member_names: Optional[Iterable[str]] = None,
    write_sidecar: bool = True,
) -> Dict[str, object]:
    folder = os.path.abspath(os.path.expanduser(str(export_folder or ".")))
    safe_name = os.path.basename(str(base_name or "Asset").strip()) or "Asset"
    extension = export_extension(export_profile_id)
    filepath = os.path.normpath(os.path.join(folder, safe_name + extension))
    sidecar = os.path.splitext(filepath)[0] + ".jammeta.json" if write_sidecar else ""
    members = [str(name) for name in (member_names or [])]
    profile = get_export_profile(export_profile_id)

    export_exists = os.path.exists(filepath)
    sidecar_exists = bool(sidecar and os.path.exists(sidecar))

    return {
        "base_name": safe_name,
        "folder": folder,
        "filepath": filepath,
        "filename": os.path.basename(filepath),
        "extension": extension,
        "format": export_format(export_profile_id),
        "profile_id": str(profile.get("id", export_profile_id)),
        "profile_label": str(profile.get("label", export_profile_id)),
        "member_count": len(members),
        "members": members,
        "write_sidecar": bool(write_sidecar),
        "sidecar_path": sidecar,
        "export_exists": export_exists,
        "sidecar_exists": sidecar_exists,
        "has_conflict": bool(export_exists or sidecar_exists),
    }


def format_export_plan(plan: Dict[str, object]) -> str:
    replacement = []
    if plan.get("export_exists"):
        replacement.append("export exists")
    if plan.get("sidecar_exists"):
        replacement.append("sidecar exists")
    suffix = " · " + ", ".join(replacement) if replacement else ""
    members = int(plan.get("member_count", 0) or 0)
    member_label = " · {0} member(s)".format(members) if members else ""
    return "{0} · {1}{2}{3}".format(
        plan.get("format", ""),
        plan.get("filepath", ""),
        member_label,
        suffix,
    )
