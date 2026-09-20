"""Headless Asset Doctor entry point for Maya/mayapy.

Example:
    mayapy jam_validate_maya.py scene.ma --profile UNREAL_STATIC \
        --output report.json --junit report.xml
"""

from __future__ import annotations

import argparse
import os
import sys


def _arguments():
    parser = argparse.ArgumentParser(description="Run JAM TA Tools validation in Maya standalone")
    parser.add_argument("scene")
    parser.add_argument("--profile", default="GENERIC_GAME")
    parser.add_argument("--profiles", default="", help="Optional project profile JSON")
    parser.add_argument("--rules", action="append", default=[], help="Project Python rule module; may be repeated")
    parser.add_argument("--output", default="jam_asset_report.json")
    parser.add_argument("--junit", default="")
    parser.add_argument("--warnings-as-errors", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _arguments()

    import maya.standalone
    maya.standalone.initialize(name="python")
    try:
        import maya.cmds as cmds

        script_dir = os.path.abspath(os.path.dirname(__file__))
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import ta_tools
        from jam_ta_core import load_profiles_json, load_rule_module, write_junit_xml, write_report_json

        if args.profiles:
            load_profiles_json(os.path.abspath(args.profiles))
        for rule_file in args.rules:
            load_rule_module(rule_file)
        cmds.file(os.path.abspath(args.scene), open=True, force=True, prompt=False)
        transforms = []
        for shape in cmds.ls(type="mesh", long=True) or []:
            if cmds.getAttr(shape + ".intermediateObject"):
                continue
            parents = cmds.listRelatives(shape, parent=True, fullPath=True) or []
            if parents and parents[0] not in transforms:
                transforms.append(parents[0])

        # CI validation must not depend on selection state or UI state.
        reports = [
            ta_tools.maya_asset_doctor_analyze_object(obj, profile_id=args.profile)
            for obj in transforms
        ]
        write_report_json(
            os.path.abspath(args.output),
            reports,
            suite_version="2.4.0",
            metadata={"host": "Maya", "profile": args.profile, "headless": True},
        )
        if args.junit:
            write_junit_xml(os.path.abspath(args.junit), reports, suite_name="JAM TA Tools / Maya")

        errors = sum(report.error_count for report in reports)
        warnings = sum(report.warning_count for report in reports)
        print("[JAM TA] validated {0} mesh(es): {1} error(s), {2} warning(s)".format(len(reports), errors, warnings))
        if errors:
            return 2
        if warnings and args.warnings_as_errors:
            return 3
        return 0
    finally:
        maya.standalone.uninitialize()


if __name__ == "__main__":
    raise SystemExit(main())
