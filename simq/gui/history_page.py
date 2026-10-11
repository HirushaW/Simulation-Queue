"""The History page: finished jobs, the speed memory, and recent activity."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import fmt, present
from ..db import FINISHED_STATES, Database
from . import theme
from .widgets import label


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(34)
    table.setShowGrid(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.horizontalHeader().setHighlightSections(False)
    return table


def _item(text: str, color: str | None = None, data: object = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if color:
        item.setForeground(QColor(color))
    if data is not None:
        item.setData(Qt.ItemDataRole.UserRole, data)
    return item


class HistoryPage(QWidget):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.setObjectName("Page")
        self.db = db
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(12)
        lay.addWidget(label("History", "PageTitle"))
        lay.addWidget(
            label(
                "Finished jobs, the speeds SimQ has measured, and everything the queue has done.",
                "PageSubtitle",
            )
        )
        tabs = QTabWidget()
        lay.addWidget(tabs, 1)

        self.finished = _table(
            ["#", "Job", "Type", "Result", "Started", "Took", "CPUs", "GPU", "Speed", "Folder"]
        )
        header = self.finished.horizontalHeader()
        for col in range(9):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        box = QWidget()
        b = QVBoxLayout(box)
        b.setContentsMargins(12, 12, 12, 12)
        b.addWidget(self.finished)
        tabs.addTab(box, "Finished jobs")

        self.speeds = _table(
            ["Date", "Job", "Program", "Mode", "CPUs", "Atoms", "s/step", "ns/day", "Measured by"]
        )
        sh = self.speeds.horizontalHeader()
        for col in range(9):
            sh.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        sh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        box2 = QWidget()
        b2 = QVBoxLayout(box2)
        b2.setContentsMargins(12, 12, 12, 12)
        b2.addWidget(
            label(
                "Every test run and finished job adds its speed here. New jobs of a "
                "similar size get an estimate from these numbers straight away.",
                "Small",
                wrap=True,
            )
        )
        b2.addWidget(self.speeds, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.delete_speed = QPushButton("Forget selected")
        self.delete_speed.setObjectName("Danger")
        row.addWidget(self.delete_speed)
        b2.addLayout(row)
        tabs.addTab(box2, "Speed memory")

        self.activity = QPlainTextEdit()
        self.activity.setObjectName("Log")
        self.activity.setReadOnly(True)
        box3 = QWidget()
        b3 = QVBoxLayout(box3)
        b3.setContentsMargins(12, 12, 12, 12)
        b3.addWidget(self.activity)
        tabs.addTab(box3, "Activity")

        self.delete_speed.clicked.connect(self._forget)
        self._signature: tuple | None = None

    def refresh(self) -> None:
        jobs = [j for j in self.db.jobs() if j.state in FINISHED_STATES]
        speeds = self.db.speeds()
        events = self.db.events(limit=400)
        signature = (
            tuple((j.id, j.state, j.ended) for j in jobs),
            tuple(r["id"] for r in speeds),
            events[-1]["id"] if events else 0,
        )
        if signature == self._signature:
            return
        self._signature = signature

        jobs.sort(key=lambda j: j.ended or 0, reverse=True)
        self.finished.setRowCount(len(jobs))
        colors = {"done": theme.OK, "failed": theme.BAD, "killed": theme.MUTED}
        for r, j in enumerate(jobs):
            speed = j.meta.get("speed") or {}
            ts = (j.meta.get("analysis") or {}).get("timestep_fs") or 2.0
            ns_day = (f"{86400 / speed['s_per_step'] * ts / 1e6:.3g} ns/day"
                      if speed.get("s_per_step") else "")  # fmt: skip
            took = fmt.duration(j.ended - j.started) if j.started and j.ended else ""
            values = [str(j.id), j.name, present.KIND_NAMES.get(j.kind, j.kind), j.state,
                      present.clock(j.started), took, str(j.cores), present.gpu_text(j), ns_day,
                      j.folder]  # fmt: skip
            for c, v in enumerate(values):
                item = _item(v, colors.get(j.state) if c == 3 else None)
                if c == 3 and j.meta.get("failure"):
                    item.setToolTip(j.meta["failure"])
                self.finished.setItem(r, c, item)

        self.speeds.setRowCount(len(speeds))
        for r, s in enumerate(speeds):
            ts = s["timestep_fs"] or 2.0
            values = [
                time.strftime("%Y-%m-%d %H:%M", time.localtime(s["time"])),
                s["job_name"] or "",
                s["program"].upper(),
                s["mode"].upper(),
                str(s["cores"]),
                f"{s['atoms']:,}" if s["atoms"] else "?",
                f"{s['s_per_step']:.4g}",
                f"{86400 / s['s_per_step'] * ts / 1e6:.3g}",
                {"test": "test run", "run": "finished job", "corescan": "core-count test"}.get(
                    s["source"], s["source"]
                ),
            ]
            for c, v in enumerate(values):
                self.speeds.setItem(r, c, _item(v, data=s["id"] if c == 0 else None))

        names = {j.id: j.name for j in self.db.jobs()}
        lines = []
        for e in events:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["time"]))
            who = f"#{e['job_id']} {names.get(e['job_id'], '(removed)')}: " if e["job_id"] else ""
            lines.append(f"{stamp}  {e['level'].upper():5}  {who}{e['text']}")
        self.activity.setPlainText("\n".join(lines))
        self.activity.verticalScrollBar().setValue(self.activity.verticalScrollBar().maximum())

    def _forget(self) -> None:
        rows = {i.row() for i in self.speeds.selectedIndexes()}
        if not rows:
            return
        answer = QMessageBox.question(
            self, "Forget speeds", f"Forget {len(rows)} measured speed(s)?"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for row in rows:
            speed_id = self.speeds.item(row, 0).data(Qt.ItemDataRole.UserRole)
            self.db.delete_speed(int(speed_id))
        self._signature = None
        self.refresh()
