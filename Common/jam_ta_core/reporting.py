"""Serialization helpers for Asset Doctor reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Dict, Any

from .models import AssetReport


def build_report_document(
    reports: Iterable[AssetReport],
    suite_version: str = "2.4.0",
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    reports = list(reports)
    return {
        "schema": "jam-ta-tools.asset-report.v1",
        "suite_version": suite_version,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "asset_count": len(reports),
        "error_count": sum(report.error_count for report in reports),
        "warning_count": sum(report.warning_count for report in reports),
        "metadata": metadata or {},
        "assets": [report.to_dict() for report in reports],
    }


def write_report_json(
    path: str,
    reports: Iterable[AssetReport],
    suite_version: str = "2.4.0",
    metadata: Dict[str, Any] | None = None,
) -> str:
    document = build_report_document(reports, suite_version=suite_version, metadata=metadata)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=False)
    return path
