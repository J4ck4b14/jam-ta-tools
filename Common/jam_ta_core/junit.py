"""JUnit serialization for CI systems that already understand test reports."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Iterable

from .models import AssetReport


def build_junit_xml(reports: Iterable[AssetReport], suite_name: str = "JAM TA Tools") -> str:
    reports = list(reports)
    failures = sum(report.error_count for report in reports)
    tests = max(1, sum(max(1, len(report.issues)) for report in reports))
    suite = ET.Element("testsuite", {
        "name": suite_name,
        "tests": str(tests),
        "failures": str(failures),
        "errors": "0",
    })

    if not reports:
        ET.SubElement(suite, "testcase", {"name": "No assets", "classname": "jam.validation"})

    for report in reports:
        if not report.issues:
            ET.SubElement(suite, "testcase", {
                "name": report.object_name,
                "classname": "jam.validation.clean",
            })
            continue

        for issue in report.sorted_issues():
            case = ET.SubElement(suite, "testcase", {
                "name": "{0}: {1}".format(report.object_name, issue.rule_id),
                "classname": "jam.validation.{0}".format(issue.category.lower().replace(" ", "_")),
            })
            if issue.severity == "ERROR":
                failure = ET.SubElement(case, "failure", {
                    "message": issue.title,
                    "type": issue.rule_id,
                })
                failure.text = issue.message
            elif issue.severity == "WARNING":
                output = ET.SubElement(case, "system-out")
                output.text = "WARNING: {0}: {1}".format(issue.title, issue.message)
            else:
                output = ET.SubElement(case, "system-out")
                output.text = "INFO: {0}: {1}".format(issue.title, issue.message)

    return ET.tostring(suite, encoding="unicode")


def write_junit_xml(path: str, reports: Iterable[AssetReport], suite_name: str = "JAM TA Tools") -> str:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(build_junit_xml(reports, suite_name=suite_name))
    return path
