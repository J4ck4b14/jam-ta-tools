"""PySide6 Asset Doctor dashboard for Maya 2025+.

This widget is the production-facing dashboard for inspection, issue drill-down,
conservative fixes and report export. The full tool shelf exposes specialised utilities.
"""

from __future__ import annotations

import os
from typing import List, Optional

try:
    from PySide6 import QtCore, QtWidgets
    from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
    import maya.cmds as cmds
except Exception:  # lets source packages lint/compile outside Maya
    QtCore = None
    QtWidgets = None
    MayaQWidgetDockableMixin = object
    cmds = None

import ta_tools
from jam_ta_core import (
    AssetReport,
    load_profiles_json,
    load_rule_module,
    profile_items,
    write_report_json,
)

_DASHBOARD = None


if QtWidgets is not None:
    class _DashboardBase(MayaQWidgetDockableMixin, QtWidgets.QWidget):
        pass
else:
    class _DashboardBase(object):
        pass


class JAMTAToolsDashboard(_DashboardBase):
    WINDOW_TITLE = "JAM TA Tools — Asset Doctor"
    OBJECT_NAME = "JAMTAToolsDashboard"

    def __init__(self, parent=None):
        if QtWidgets is None:
            raise RuntimeError("PySide6 is not available. This dashboard requires Maya 2025+.")
        super().__init__(parent=parent)
        self.setObjectName(self.OBJECT_NAME)
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setMinimumWidth(520)
        self.resize(720, 760)
        self.reports: List[AssetReport] = []
        self._build_ui()
        self._refresh_profiles()

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(7)

        header = QtWidgets.QHBoxLayout()
        self.profile = QtWidgets.QComboBox()
        self.profile.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        header.addWidget(QtWidgets.QLabel("Profile"))
        header.addWidget(self.profile, 1)

        self.analyze_button = QtWidgets.QPushButton("Analyze Selection")
        self.analyze_button.clicked.connect(self.analyze_selection)
        header.addWidget(self.analyze_button)
        root.addLayout(header)

        extension_row = QtWidgets.QHBoxLayout()
        load_profiles = QtWidgets.QPushButton("Project Profiles…")
        load_profiles.clicked.connect(self.load_project_profiles)
        extension_row.addWidget(load_profiles)
        load_rules = QtWidgets.QPushButton("Project Rules…")
        load_rules.clicked.connect(self.load_project_rules)
        extension_row.addWidget(load_rules)
        extension_row.addStretch(1)
        root.addLayout(extension_row)

        # Keep the dense table scannable; detailed information is shown below it.
        self.table = QtWidgets.QTreeWidget()
        self.table.setHeaderLabels([
            "Status", "Asset", "Tris", "Render verts", "UV util", "Textures", "Missing",
        ])
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.currentItemChanged.connect(self._show_current_report)
        root.addWidget(self.table, 3)

        self.detail = QtWidgets.QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        root.addWidget(self.detail, 2)

        actions = QtWidgets.QHBoxLayout()
        select_issue = QtWidgets.QPushButton("Select First Issue")
        select_issue.clicked.connect(self.select_first_issue)
        actions.addWidget(select_issue)
        fix_safe = QtWidgets.QPushButton("Fix Safe")
        fix_safe.clicked.connect(self.fix_safe)
        actions.addWidget(fix_safe)
        export_report = QtWidgets.QPushButton("Export JSON…")
        export_report.clicked.connect(self.export_report)
        actions.addWidget(export_report)
        full_tools = QtWidgets.QPushButton("Full Tools")
        full_tools.clicked.connect(lambda: ta_tools.show(docked=True))
        actions.addWidget(full_tools)
        root.addLayout(actions)

        self.status = QtWidgets.QLabel("Select one or more mesh assets, then analyze.")
        self.status.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.status)

    def _refresh_profiles(self, preferred: str = "") -> None:
        previous = preferred or self.profile.currentData() or "GENERIC_GAME"
        self.profile.blockSignals(True)
        self.profile.clear()
        for profile_id, label, description in profile_items():
            self.profile.addItem(label, profile_id)
            index = self.profile.count() - 1
            self.profile.setItemData(index, description, QtCore.Qt.ItemDataRole.ToolTipRole)
        match = self.profile.findData(previous)
        if match >= 0:
            self.profile.setCurrentIndex(match)
        self.profile.blockSignals(False)

    def _selected_meshes(self):
        return ta_tools.get_selected_mesh_transforms()

    def analyze_selection(self) -> None:
        profile_id = str(self.profile.currentData() or "GENERIC_GAME")
        meshes = self._selected_meshes()
        if not meshes:
            self.reports = []
            self._populate_table()
            self.status.setText("No mesh transforms selected.")
            return

        self.reports = [
            ta_tools.maya_asset_doctor_analyze_object(obj, profile_id=profile_id)
            for obj in meshes
        ]
        self._populate_table()
        errors = sum(report.error_count for report in self.reports)
        warnings = sum(report.warning_count for report in self.reports)
        self.status.setText(
            "{0} asset(s) · {1} error(s) · {2} warning(s)".format(
                len(self.reports), errors, warnings
            )
        )

    def _populate_table(self) -> None:
        self.table.clear()
        for index, report in enumerate(self.reports):
            metrics = report.metrics
            item = QtWidgets.QTreeWidgetItem([
                report.status,
                report.object_name,
                "{0:,}".format(int(metrics.get("triangles", 0))),
                "{0:,}".format(int(metrics.get("render_vertices", 0))),
                "{0:.1f}%".format(float(metrics.get("uv_utilization_percent", 0.0))),
                str(int(metrics.get("texture_count", 0))),
                str(int(metrics.get("missing_texture_count", 0))),
            ])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, index)
            self.table.addTopLevelItem(item)
        for column in range(self.table.columnCount()):
            self.table.resizeColumnToContents(column)
        if self.table.topLevelItemCount():
            self.table.setCurrentItem(self.table.topLevelItem(0))
        else:
            self.detail.clear()

    def _current_report(self) -> Optional[AssetReport]:
        item = self.table.currentItem()
        if item is None:
            return None
        index = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(index, int) and 0 <= index < len(self.reports):
            return self.reports[index]
        return None

    def _show_current_report(self, *_args) -> None:
        report = self._current_report()
        if report is None:
            self.detail.clear()
            return
        m = report.metrics
        lines = [
            "{0} [{1}] — {2}".format(report.object_name, report.status, report.profile_id),
            "",
            "{0:,} tris | {1:,} model verts | {2:,} render verts".format(
                int(m.get("triangles", 0)), int(m.get("model_vertices", 0)), int(m.get("render_vertices", 0))
            ),
            "UV islands {0} | utilisation ~{1:.1f}% | overlap faces {2} | min padding ~{3:.1f}px".format(
                int(m.get("uv_island_count", 0)), float(m.get("uv_utilization_percent", 0.0)),
                int(m.get("uv_overlap_faces", 0)), float(m.get("uv_min_padding_px", 0.0)),
            ),
            "Materials {0} | textures {1} | missing {2} | UDIM {3}".format(
                int(m.get("material_count", 0)), int(m.get("texture_count", 0)),
                int(m.get("missing_texture_count", 0)), int(m.get("udim_texture_count", 0)),
            ),
            "",
        ]
        for issue in report.sorted_issues():
            fix = " [safe fix]" if issue.is_safe_fixable else ""
            lines.append("{0} | {1} | {2}{3}".format(issue.severity, issue.category, issue.title, fix))
            lines.append("  " + issue.message)
        if not report.issues:
            lines.append("Clean")
        self.detail.setPlainText("\n".join(lines))

    def select_first_issue(self) -> None:
        report = self._current_report()
        if report is None or not report.sorted_issues():
            return
        ta_tools.maya_select_report_issue(report, report.sorted_issues()[0])

    def fix_safe(self) -> None:
        if not self.reports:
            return
        count = ta_tools.maya_apply_safe_fixes(self.reports)
        self.status.setText("Applied {0} conservative safe fix(es); re-analyzing.".format(count))
        self.analyze_selection()

    def export_report(self) -> None:
        if not self.reports:
            return
        path, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export JAM Asset Doctor Report", "jam_asset_report.json", "JSON (*.json)"
        )
        if not path:
            return
        write_report_json(path, self.reports, suite_version="2.4.0", metadata={"host": "Maya", "ui": "PySide6"})
        self.status.setText("Report written to {0}".format(path))

    def load_project_profiles(self) -> None:
        path, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load JAM project profiles", "", "JSON (*.json)"
        )
        if not path:
            return
        loaded = load_profiles_json(path)
        ta_tools._refresh_profile_catalog()
        self._refresh_profiles(preferred=loaded[0] if loaded else "GENERIC_GAME")
        self.status.setText("Loaded {0} project profile(s).".format(len(loaded)))

    def load_project_rules(self) -> None:
        path, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load JAM project rules", "", "Python (*.py)"
        )
        if not path:
            return
        load_rule_module(path)
        self.status.setText("Loaded project rules from {0}".format(os.path.basename(path)))


def show_dashboard():
    global _DASHBOARD
    if QtWidgets is None:
        raise RuntimeError("PySide6 dashboard requires Maya 2025+.")
    try:
        if _DASHBOARD is not None:
            _DASHBOARD.close()
            _DASHBOARD.deleteLater()
    except Exception:
        pass
    _DASHBOARD = JAMTAToolsDashboard()
    _DASHBOARD.show(dockable=True, floating=False, area="right", retain=False)
    return _DASHBOARD
