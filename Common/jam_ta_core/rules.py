"""Small validation-rule registry shared by DCC, engine-adjacent and CI tooling.

The point is to let a project add a rule without editing JAM itself. Rules receive the
finished built-in report, the active profile, and a deliberately plain snapshot dict.
They may return one issue, a list of issues, or nothing.
"""

from __future__ import annotations

from collections import OrderedDict
import importlib.util
import os
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from .models import AssetReport, ValidationIssue

RuleCallback = Callable[[AssetReport, Mapping[str, Any], Mapping[str, Any]], Any]

_RULES: "OrderedDict[str, RuleCallback]" = OrderedDict()


def register_rule(rule_id: str, callback: RuleCallback, replace: bool = False) -> None:
    """Register a project validator.

    Keeping the id external to the callback makes collisions obvious; quietly replacing a
    studio rule can otherwise produce difficult-to-diagnose conflicts in production.
    """
    key = str(rule_id).strip()
    if not key:
        raise ValueError("rule_id cannot be empty")
    if not callable(callback):
        raise TypeError("callback must be callable")
    if key in _RULES and not replace:
        raise ValueError("Validation rule already registered: {0}".format(key))
    _RULES[key] = callback


def unregister_rule(rule_id: str) -> bool:
    return _RULES.pop(str(rule_id), None) is not None


def clear_registered_rules() -> None:
    _RULES.clear()


def registered_rule_ids() -> tuple[str, ...]:
    return tuple(_RULES.keys())


def _coerce_issues(rule_id: str, value: Any) -> List[ValidationIssue]:
    if value is None:
        return []
    if isinstance(value, ValidationIssue):
        return [value]
    if isinstance(value, (list, tuple)):
        issues: List[ValidationIssue] = []
        for item in value:
            if not isinstance(item, ValidationIssue):
                raise TypeError(
                    "Rule {0} returned {1}; expected ValidationIssue".format(
                        rule_id, type(item).__name__
                    )
                )
            issues.append(item)
        return issues
    raise TypeError(
        "Rule {0} returned {1}; expected ValidationIssue, sequence, or None".format(
            rule_id, type(value).__name__
        )
    )


def run_registered_rules(
    report: AssetReport,
    profile: Mapping[str, Any],
    snapshot: Optional[Mapping[str, Any]] = None,
) -> List[ValidationIssue]:
    """Run project rules and append their findings to ``report``.

    A broken custom rule becomes one explicit validator error instead of taking Asset Doctor
    down. Pipeline extensions report failures without terminating the analysis pass.
    """
    produced: List[ValidationIssue] = []
    context: Dict[str, Any] = dict(snapshot or {})
    context.setdefault("object_name", report.object_name)
    context.setdefault("host", report.host)
    context.setdefault("metrics", report.metrics)
    context.setdefault("metadata", report.metadata)

    for rule_id, callback in tuple(_RULES.items()):
        try:
            issues = _coerce_issues(rule_id, callback(report, profile, context))
        except Exception as exc:  # project callbacks are a boundary, so keep this broad
            issues = [ValidationIssue(
                rule_id="extension.{0}.exception".format(rule_id),
                title="Custom validation rule failed",
                message="Rule '{0}' raised {1}: {2}".format(rule_id, type(exc).__name__, exc),
                category="Pipeline",
                severity="ERROR",
                metadata={"registered_rule": rule_id},
            )]

        for issue in issues:
            if not issue.rule_id:
                issue.rule_id = rule_id
            report.add_issue(issue)
            produced.append(issue)

    return produced


def load_rule_module(path: str) -> str:
    """Import a project rule file for its ``register_rule`` calls and return its module name."""
    full_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(full_path):
        raise FileNotFoundError(full_path)
    stem = os.path.splitext(os.path.basename(full_path))[0]
    module_name = "jam_ta_project_rules_{0}_{1}".format(stem, abs(hash(full_path)))
    spec = importlib.util.spec_from_file_location(module_name, full_path)
    if spec is None or spec.loader is None:
        raise ImportError("Could not load JAM rule module: {0}".format(full_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module_name
