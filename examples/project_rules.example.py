"""Example JAM project validators.

Load this file from CI with --rules or import it during DCC startup. Rules are ordinary
Python; they receive the completed built-in report, active profile and a plain snapshot.
"""

from jam_ta_core import ValidationIssue, register_rule


def project_material_rule(report, profile, snapshot):
    metrics = snapshot["metrics"]
    if metrics.get("material_slots", 0) <= 4:
        return None
    return ValidationIssue(
        rule_id="project.material_sections",
        title="Project material-section cap",
        message="This example project allows at most four material sections.",
        category="Project",
        severity="WARNING",
    )


register_rule("project.material_sections", project_material_rule)
