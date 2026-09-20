"""Headless Asset Doctor entry point for Blender.

Example:
    blender -b asset.blend --python jam_validate_blend.py -- \
        --profile UNREAL_STATIC --output report.json --junit report.xml
"""

from __future__ import annotations

import argparse
import os
import sys

import bpy

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ta_tools  # noqa: E402
from jam_ta_core import load_profiles_json, load_rule_module, write_junit_xml, write_report_json  # noqa: E402


def _arguments():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    parser = argparse.ArgumentParser(description="Run JAM TA Tools validation in Blender background mode")
    parser.add_argument("--profile", default="GENERIC_GAME")
    parser.add_argument("--profiles", default="", help="Optional project profile JSON")
    parser.add_argument("--rules", action="append", default=[], help="Project Python rule module; may be repeated")
    parser.add_argument("--output", default="jam_asset_report.json")
    parser.add_argument("--junit", default="")
    parser.add_argument("--selected", action="store_true", help="Validate selected meshes instead of every mesh")
    parser.add_argument("--warnings-as-errors", action="store_true")
    return parser.parse_args(argv)


def main() -> int:
    args = _arguments()
    if args.profiles:
        load_profiles_json(os.path.abspath(args.profiles))
    for rule_file in args.rules:
        load_rule_module(rule_file)
    if not hasattr(bpy.types.Scene, "ta_profile_id"):
        ta_tools.register()

    scene = bpy.context.scene
    scene.ta_profile_id = args.profile
    if args.selected:
        objects = [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]
    else:
        objects = [obj for obj in bpy.data.objects if obj.type == "MESH"]

    reports = [ta_tools.ta_asset_doctor_analyze_object(bpy.context, obj) for obj in objects]
    write_report_json(
        os.path.abspath(args.output),
        reports,
        suite_version="2.4.0",
        metadata={"host": "Blender", "profile": args.profile, "headless": True},
    )
    if args.junit:
        write_junit_xml(os.path.abspath(args.junit), reports, suite_name="JAM TA Tools / Blender")

    errors = sum(report.error_count for report in reports)
    warnings = sum(report.warning_count for report in reports)
    print("[JAM TA] validated {0} mesh(es): {1} error(s), {2} warning(s)".format(len(reports), errors, warnings))
    if errors:
        return 2
    if warnings and args.warnings_as_errors:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
