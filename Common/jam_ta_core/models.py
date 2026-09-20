"""Data model shared by Blender, Maya, command-line tooling and tests."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional

SEVERITY_ORDER = {
    "INFO": 0,
    "WARNING": 1,
    "ERROR": 2,
}


@dataclass
class ValidationIssue:
    rule_id: str
    title: str
    message: str
    category: str
    severity: str = "WARNING"
    object_name: str = ""
    component_type: str = ""
    components: List[Any] = field(default_factory=list)
    safe_fix_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        severity = str(self.severity).upper()
        if severity not in SEVERITY_ORDER:
            raise ValueError("Unknown issue severity: {0}".format(self.severity))
        self.severity = severity

    @property
    def is_blocking(self) -> bool:
        return self.severity == "ERROR"

    @property
    def is_safe_fixable(self) -> bool:
        return bool(self.safe_fix_id)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AssetReport:
    object_name: str
    host: str
    profile_id: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    issues: List[ValidationIssue] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_issue(self, issue: ValidationIssue) -> None:
        if not issue.object_name:
            issue.object_name = self.object_name
        self.issues.append(issue)

    def extend(self, issues: Iterable[ValidationIssue]) -> None:
        for issue in issues:
            self.add_issue(issue)

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "WARNING")

    @property
    def info_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "INFO")

    @property
    def safe_fix_count(self) -> int:
        return sum(1 for issue in self.issues if issue.is_safe_fixable)

    @property
    def status(self) -> str:
        if self.error_count:
            return "ERROR"
        if self.warning_count:
            return "WARNING"
        return "CLEAN"

    def sorted_issues(self) -> List[ValidationIssue]:
        return sorted(
            self.issues,
            key=lambda issue: (
                -SEVERITY_ORDER.get(issue.severity, 0),
                issue.category,
                issue.rule_id,
            ),
        )

    def summary(self) -> str:
        if not self.issues:
            return "Clean"
        return "{0} error(s), {1} warning(s), {2} info".format(
            self.error_count,
            self.warning_count,
            self.info_count,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "object_name": self.object_name,
            "host": self.host,
            "profile_id": self.profile_id,
            "status": self.status,
            "summary": self.summary(),
            "metrics": self.metrics,
            "issues": [issue.to_dict() for issue in self.sorted_issues()],
            "metadata": self.metadata,
        }
