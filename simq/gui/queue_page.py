"""The Queue page: the job table, the actions, and the selected job's details."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import fmt, present
from ..db import FAILED, PAUSED, QUEUED, RUNNING, Database, Job
from ..detect.software import DetectionReport
from ..engine import client, launch
from ..monitor.gpu import GpuSystemInfo
from . import theme
from .job_dialog import BatchAddDialog, JobDialog
from .widgets import label

COLUMNS = ("#", "Name", "Type", "CPUs", "GPU", "Check", "Status", "Progress", "Time", "When")
(C_ID, C_NAME, C_TYPE, C_CPUS, C_GPU, C_CHECK, C_STATUS, C_PROGRESS, C_TIME,
 C_WHEN) = range(len(COLUMNS))  # fmt: skip


def _status_color(job: Job) -> str:
    if job.state == RUNNING:
        return theme.ACCENT
    if job.state == PAUSED:
        return theme.WARN
    if job.state == "done":
        return theme.OK
    if job.state == FAILED or (job.state == QUEUED and job.check_state == "failed"):
        return theme.BAD
    return theme.MUTED


def _check_color(text: str) -> str:
    if "✘" in text:
        return theme.BAD
    if "!" in text:
        return theme.WARN
    if "✔" in text:
        return theme.OK
    return theme.MUTED


class QueueModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[Job] = []
        self.planned: dict[int, tuple[float | None, float | None]] = {}
        self.queue_running = False

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008
        return 0 if parent.isValid() else len(self.jobs)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802, B008
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def set_data(self, jobs: list[Job], planned, queue_running: bool) -> None:
        same_rows = [j.id for j in jobs] == [j.id for j in self.jobs]
        if not same_rows:
            self.beginResetModel()
        self.jobs, self.planned, self.queue_running = jobs, planned, queue_running
        if same_rows:
            if jobs:
                self.dataChanged.emit(self.index(0, 0), self.index(len(jobs) - 1, len(COLUMNS) - 1))
        else:
            self.endResetModel()

    def text(self, job: Job, col: int, now: float) -> str:
        if col == C_ID:
            return str(job.id)
        if col == C_NAME:
            return job.name
        if col == C_TYPE:
            return present.KIND_NAMES.get(job.kind, job.kind)
        if col == C_CPUS:
            return str(job.cores)
        if col == C_GPU:
            return present.gpu_text(job)
        if col == C_CHECK:
            return present.check_text(job)
        if col == C_STATUS:
            return present.status_text(job, now, self.queue_running)
        if col == C_PROGRESS:
            return present.progress_text(job)
        if col == C_TIME:
            return present.time_left(job)
        if col == C_WHEN:
            return present.when_text(job, self.planned.get(job.id), now)
        return ""

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        job = self.jobs[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self.text(job, col, time.time())
        if role == Qt.ItemDataRole.UserRole:
            return job
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == C_STATUS:
                return QColor(_status_color(job))
            if col == C_CHECK:
                return QColor(_check_color(present.check_text(job)))
            if col in (C_ID, C_TYPE, C_TIME, C_WHEN):
                return QColor(theme.MUTED)
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (C_ID, C_CPUS, C_CHECK):
            return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == C_STATUS and job.meta.get("failure"):
                return job.meta["failure"]
            if col == C_TIME and job.est_source:
                return f"Estimate from: {job.est_source}"
            if col == C_NAME:
                return job.folder
        return None


class ProgressDelegate(QStyledItemDelegate):
    """Draws the Progress column as a thin bar with the percentage."""

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        job: Job = index.data(Qt.ItemDataRole.UserRole)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor("#23304d"))
        if job is None or job.progress is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(8, 0, -8, 0)
        bar = QRectF(r.left(), r.center().y() + 7, r.width(), 4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.TRACK))
        painter.drawRoundedRect(bar, 2, 2)
        color = theme.OK if job.state == "done" else theme.WARN if job.state == PAUSED \
            else theme.ACCENT  # fmt: skip
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * job.progress, 4), 2, 2)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(
            QRectF(r.left(), r.top(), r.width(), r.height() / 2 + 3),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom),
            present.progress_text(job),
        )
        painter.restore()


# ---------------------------------------------------------------------------
# Details of the selected job
# ---------------------------------------------------------------------------


_SOURCES = {"live": "the running job's speed", "test run": "its test run"}


class JobDetails(QTabWidget):
    use_cores = Signal(int, int)  # job id, CPU count (from the core-count test)

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.job: Job | None = None

        self.overview_box = QWidget()
        self.overview = QVBoxLayout(self.overview_box)
        self.overview.setContentsMargins(16, 14, 16, 14)
        self.overview.setSpacing(10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.overview_box)
        self.addTab(scroll, "Overview")

        self.check_box = QWidget()
        self.check = QVBoxLayout(self.check_box)
        self.check.setContentsMargins(16, 14, 16, 14)
        check_scroll = QScrollArea()
        check_scroll.setWidgetResizable(True)
        check_scroll.setFrameShape(QFrame.Shape.NoFrame)
        check_scroll.setWidget(self.check_box)
        self.addTab(check_scroll, "Check")

        output = QWidget()
        out_lay = QVBoxLayout(output)
        out_lay.setContentsMargins(12, 10, 12, 12)
        row = QHBoxLayout()
        self.output_source = QComboBox()
        self.output_source.addItems(["Simulation log", "Command output"])
        row.addWidget(self.output_source)
        self.output_path = label("", "Small")
        row.addWidget(self.output_path, 1)
        out_lay.addLayout(row)
        self.output = QPlainTextEdit()
        self.output.setObjectName("Log")
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        out_lay.addWidget(self.output, 1)
        self.addTab(output, "Output")

        self.events = QPlainTextEdit()
        self.events.setObjectName("Log")
        self.events.setReadOnly(True)
        events_box = QWidget()
        ev_lay = QVBoxLayout(events_box)
        ev_lay.setContentsMargins(12, 12, 12, 12)
        ev_lay.addWidget(self.events)
        self.addTab(events_box, "Events")

        self.output_source.currentIndexChanged.connect(lambda _: self.refresh_output())
        self.currentChanged.connect(lambda _: self.refresh_live())
        self._last_key: tuple | None = None

    @staticmethod
    def _clear(layout: QVBoxLayout | QGridLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                JobDetails._clear(item.layout())  # type: ignore[arg-type]

    def show_job(self, job: Job | None) -> None:
        key = None if job is None else (job.id, job.state, job.check_state, job.test_state,
                                        job.est_seconds, job.status_text, job.cores, job.gpu,
                                        str(job.meta.get("corescan")), str(job.meta.get("test")),
                                        job.command, job.retries)  # fmt: skip
        self.job = job
        if key != self._last_key:
            self._last_key = key
            self._fill_overview(job)
            self._fill_check(job)
        self.refresh_live()

    def refresh_live(self) -> None:
        if self.currentIndex() == 2:
            self.refresh_output()
        elif self.currentIndex() == 3:
            self.refresh_events()

    # -- overview -------------------------------------------------------------

    def _fill_overview(self, job: Job | None) -> None:
        self._clear(self.overview)
        if job is None:
            self.overview.addWidget(label("Select a job to see its details.", "Muted"))
            self.overview.addStretch(1)
            return
        head = QHBoxLayout()
        head.addWidget(label(job.name, "SectionTitle"))
        head.addStretch(1)
        open_folder = QPushButton("Open folder")
        open_folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(job.folder))
        )
        head.addWidget(open_folder)
        self.overview.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        analysis = job.meta.get("analysis") or {}
        speed = job.meta.get("speed") or {}
        rows: list[tuple[str, str]] = [
            ("Folder", job.folder),
            ("Command", job.command),
            (
                "Resources",
                f"{job.cores} CPU{'s' if job.cores != 1 else ''}, "
                + ("no GPU" if job.gpu == "none" else present.gpu_text(job)),
            ),
            (
                "Retries",
                f"{job.retries} after a crash"
                + (f" · attempt {job.attempt}" if job.attempt else ""),
            ),
        ]
        if job.start_at:
            rows.append(("Start", f"not before {present.clock(job.start_at)}"))
        if job.after_job:
            rows.append(("Start", f"after job #{job.after_job}"))
        if analysis.get("total_steps"):
            md = f" · {analysis['ns']:g} ns of MD" if analysis.get("ns") else ""
            rows.append(("Steps", f"{analysis['total_steps']:,}{md}"))
        if analysis.get("atoms"):
            rows.append(("Atoms", f"{analysis['atoms']:,}"))
        if analysis.get("traj_bytes"):
            rows.append(("Trajectory", f"about {fmt.size(analysis['traj_bytes'])}"))
        if speed.get("s_per_step"):
            ts = analysis.get("timestep_fs") or 2.0
            ns_day = 86400 / speed["s_per_step"] * ts / 1e6
            rows.append(
                (
                    "Speed",
                    f"{speed['s_per_step']:.4g} s/step · {ns_day:.3g} ns/day "
                    f"({speed.get('source', '')})",
                )
            )
        if job.state in (RUNNING, PAUSED) and job.remaining_s is not None:
            ends = present.clock(time.time() + job.remaining_s)
            rows.append(("Time left", f"{fmt.duration(job.remaining_s)} · ends about {ends}"))
        if job.est_seconds:
            rows.append(
                (
                    "Estimate",
                    f"{fmt.duration(job.est_seconds)} in total"
                    + (
                        f" · from {_SOURCES.get(job.est_source, job.est_source)}"
                        if job.est_source
                        else ""
                    ),
                )
            )
        elif analysis.get("program") in ("namd", "amber"):
            rows.append(
                ("Estimate", "unknown yet: run a Test run, or it is measured once the job starts")
            )
        if job.meta.get("failure") and job.state in (FAILED, QUEUED):
            rows.append(("Last failure", job.meta["failure"]))
        test = job.meta.get("test") or {}
        if test:
            text = "passed" if test.get("state") == "ok" else "failed"
            if test.get("speed"):
                text += f" · {test['speed']}"
            if test.get("reason"):
                text += f" · {test['reason']}"
            rows.append(("Test run", f"{text} ({present.clock(test.get('time'))})"))
        for r, (key, value) in enumerate(rows):
            k = label(key, "StatLabel")
            v = label(value, wrap=True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if key == "Last failure":
                v.setStyleSheet(f"color: {theme.BAD};")
            grid.addWidget(k, r, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(v, r, 1)
        grid.setColumnStretch(1, 1)
        self.overview.addLayout(grid)

        stages = analysis.get("stages") or []
        if len(stages) > 1:
            self.overview.addWidget(label("STAGES", "StatLabel"))
            sgrid = QGridLayout()
            sgrid.setHorizontalSpacing(18)
            sgrid.setVerticalSpacing(3)
            sps = speed.get("s_per_step")
            for r, s in enumerate(stages):
                sgrid.addWidget(label(f"{s['number']}. {s['title']}"), r, 0)
                sgrid.addWidget(label(f"{s['steps']:,} steps", "Muted"), r, 1)
                eta = fmt.duration(s["steps"] * sps) if sps else ""
                sgrid.addWidget(label(eta, "Muted"), r, 2)
            sgrid.setColumnStretch(3, 1)
            self.overview.addLayout(sgrid)

        scan = job.meta.get("corescan") or {}
        results = {int(k): v for k, v in (scan.get("results") or {}).items() if v}
        if results:
            self.overview.addWidget(label("CORE-COUNT TEST", "StatLabel"))
            best = min(results.values())
            enough = min((c for c, s in results.items() if best / s >= 0.9), default=None)
            sgrid = QGridLayout()
            sgrid.setHorizontalSpacing(18)
            for r, (cores, sps) in enumerate(sorted(results.items())):
                ts = analysis.get("timestep_fs") or 2.0
                ns_day = 86400 / sps * ts / 1e6
                sgrid.addWidget(label(f"{cores} CPUs"), r, 0)
                sgrid.addWidget(label(f"{ns_day:.3g} ns/day", "Muted"), r, 1)
                sgrid.addWidget(label(f"{100 * best / sps:.0f}% of the fastest", "Muted"), r, 2)
            sgrid.setColumnStretch(3, 1)
            self.overview.addLayout(sgrid)
            if enough is not None and enough != job.cores:
                row = QHBoxLayout()
                row.addWidget(label(f"{enough} CPUs give at least 90% of the best speed.", "Small"))
                use = QPushButton(f"Use {enough} CPUs")
                use.clicked.connect(lambda: self.use_cores.emit(job.id, enough))
                row.addWidget(use)
                row.addStretch(1)
                self.overview.addLayout(row)
        self.overview.addStretch(1)

    def _fill_check(self, job: Job | None) -> None:
        self._clear(self.check)
        check = (job.meta.get("check") if job else None) or {}
        if not check:
            self.check.addWidget(label("Not checked yet. Press Check.", "Muted"))
            self.check.addStretch(1)
            return
        when = present.clock(check.get("time"))
        self.check.addWidget(label(f"Quick check at {when}", "StatLabel"))
        icons = {"ok": ("✔", theme.OK), "warn": ("!", theme.WARN), "error": ("✘", theme.BAD)}
        for level, text in check.get("findings", []):
            icon, color = icons.get(level, ("•", theme.MUTED))
            row = QHBoxLayout()
            mark = label(icon)
            mark.setFixedWidth(18)
            mark.setStyleSheet(f"color: {color}; font-weight: 700;")
            row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
            body = label(text, wrap=True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(body, 1)
            self.check.addLayout(row)
        self.check.addStretch(1)

    # -- live tabs -------------------------------------------------------------

    def _simulation_log(self, job: Job) -> Path | None:
        from ..engine.progress import read_progress

        analysis = job.meta.get("analysis") or {}
        run_dir = job.meta.get("run_dir")
        since = 0.0 if job.state not in (RUNNING, PAUSED) else (job.started or 0) - 5
        prog = read_progress(
            job, analysis, Path(job.folder), Path(run_dir) if run_dir else None, since
        )
        if prog.log and Path(prog.log).is_file():
            return Path(prog.log)
        return None

    def refresh_output(self) -> None:
        job = self.job
        if job is None:
            self.output.setPlainText("")
            self.output_path.setText("")
            return
        path: Path | None = None
        if self.output_source.currentIndex() == 0:
            path = self._simulation_log(job)
        if path is None and job.meta.get("run_dir"):
            path = Path(job.meta["run_dir"]) / "output.log"
        if path is None or not path.exists():
            self.output_path.setText("")
            self.output.setPlainText("Nothing yet: the job has not run.")
            return
        self.output_path.setText(str(path))
        text = launch.tail(path, 300)
        if text != self.output.toPlainText():
            bar = self.output.verticalScrollBar()
            at_end = bar.value() >= bar.maximum() - 4
            self.output.setPlainText(text)
            if at_end:
                bar.setValue(bar.maximum())

    def refresh_events(self) -> None:
        if self.job is None:
            self.events.setPlainText("")
            return
        lines = []
        for e in self.db.events(self.job.id, limit=300):
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["time"]))
            lines.append(f"{stamp}  {e['level'].upper():5}  {e['text']}")
        text = "\n".join(lines)
        if text != self.events.toPlainText():
            self.events.setPlainText(text)
            self.events.verticalScrollBar().setValue(self.events.verticalScrollBar().maximum())


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

ACTIONS = (
    ("check", "Check"),
    ("test", "Test run"),
    ("cancel_test", "Stop test"),
    ("corescan", "Core test"),
    ("run_now", "Run now"),
    ("pause", "Pause"),
    ("resume", "Resume"),
    ("kill", "Kill"),
    ("retry", "Run again"),
    ("hold", "Hold"),
    ("release", "Release"),
    ("edit", "Edit…"),
    ("duplicate", "Duplicate"),
    ("remove", "Remove"),
)


class QueuePage(QWidget):
    def __init__(self, db: Database, gpus: GpuSystemInfo, report_source) -> None:
        super().__init__()
        self.setObjectName("Page")
        self.db = db
        self.gpus = gpus
        self.report_source = report_source  # callable returning the latest DetectionReport
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(12)

        title_row = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("Queue", "PageTitle"))
        self.summary = label("", "PageSubtitle")
        titles.addWidget(self.summary)
        title_row.addLayout(titles, 1)
        self.add_button = QPushButton("+ Add job")
        self.add_button.setObjectName("Primary")
        self.batch_button = QPushButton("+ Add folders…")
        self.verify = QComboBox()
        self.verify.addItem("Verify all first", "all_first")
        self.verify.addItem("Verify as you go", "as_you_go")
        self.verify.setToolTip(
            "Verify all first: every job is checked as soon as it is added.\n"
            "Verify as you go: each job is checked when its turn comes.\n"
            "Either way, a job is checked again right before it starts."
        )
        self.start_button = QPushButton("▶  Start queue")
        self.start_button.setObjectName("Primary")
        for w in (self.verify, self.batch_button, self.add_button, self.start_button):
            title_row.addWidget(w, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(title_row)

        self.table = QTableView()
        self.model = QueueModel()
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(C_PROGRESS, ProgressDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        widths = {C_ID: 44, C_TYPE: 86, C_CPUS: 56, C_GPU: 70, C_CHECK: 82, C_PROGRESS: 110,
                  C_TIME: 104, C_WHEN: 150}  # fmt: skip
        for col, width in widths.items():
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(col, width)
        header.setSectionResizeMode(C_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(C_STATUS, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(lambda _: self._do("edit"))

        self.empty = label(
            "The queue is empty. Press  + Add job  and choose a simulation folder.", "Muted"
        )
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.buttons: dict[str, QPushButton] = {}
        for key, text in ACTIONS:
            button = QPushButton(text)
            button.setObjectName("Danger" if key in ("kill", "remove") else "Toolbar")
            button.clicked.connect(lambda _=False, k=key: self._do(k))
            self.buttons[key] = button
            actions.addWidget(button)
        actions.addStretch(1)
        up = QPushButton("↑")
        down = QPushButton("↓")
        for b, d in ((up, -1), (down, 1)):
            b.setObjectName("Toolbar")
            b.setFixedWidth(36)
            b.setToolTip("Move the job up or down the queue")
            b.clicked.connect(lambda _=False, d=d: self._move(d))
            actions.addWidget(b)
        self.up, self.down = up, down

        top = QWidget()
        top_lay = QVBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(8)
        top_lay.addWidget(self.table, 1)
        top_lay.addWidget(self.empty)
        top_lay.addLayout(actions)

        self.details = JobDetails(db)
        self.details.use_cores.connect(self._use_cores)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setChildrenCollapsible(False)
        lay.addWidget(split, 1)

        self.add_button.clicked.connect(self._add)
        self.batch_button.clicked.connect(self._batch)
        self.start_button.clicked.connect(self._toggle_queue)
        self.verify.currentIndexChanged.connect(self._verify_changed)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._selection_changed())
        self._loading = False
        self.refresh()

    # -- data ---------------------------------------------------------------------

    def selected(self) -> Job | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        return self.model.jobs[row] if row < len(self.model.jobs) else None

    def refresh(self) -> None:
        selected = self.selected()
        jobs = self.db.jobs()
        settings = self.db.settings()
        running = bool(settings.get("queue_running"))
        total = client.queue_cpus(self.db)
        gpus = [g.index for g in self.gpus.gpus]
        now = time.time()
        planned = present.queue_plan(self.db, jobs, total, gpus, now)
        self.model.set_data(jobs, planned, running)
        if selected is not None:
            for row, job in enumerate(jobs):
                if job.id == selected.id:
                    if self.table.currentIndex().row() != row:
                        self.table.selectRow(row)
                    break
        self.table.setVisible(bool(jobs))
        self.empty.setVisible(not jobs)

        self._loading = True
        self.verify.setCurrentIndex(max(0, self.verify.findData(settings.get("verify_mode"))))
        self._loading = False
        self.start_button.setText("Pause queue" if running else "▶  Start queue")
        self.start_button.setObjectName("Toolbar" if running else "Primary")
        self.start_button.style().unpolish(self.start_button)
        self.start_button.style().polish(self.start_button)

        busy_cpus = sum(r.cores for r in self.db.runs(RUNNING))
        busy_gpus = {r.gpu_index for r in self.db.runs(RUNNING) if r.gpu_index is not None}
        parts = [f"{max(0, total - busy_cpus)} of {total} CPUs free"]
        for g in gpus:
            parts.append(f"GPU {g} {'busy' if g in busy_gpus else 'free'}")
        # "Everything done by …" only when every unfinished job has a predicted end.
        open_jobs = [j for j in jobs if j.state in (QUEUED, RUNNING, PAUSED)]
        ends = [planned.get(j.id, (None, None))[1] for j in open_jobs]
        if open_jobs and all(ends):
            parts.append(f"everything done by {present.clock(max(ends), now)}")
        parts.append("queue running" if running else "queue paused")
        self.summary.setText("  ·  ".join(parts))
        self._selection_changed()

    def _selection_changed(self) -> None:
        job = self.selected()
        for key, button in self.buttons.items():
            button.setEnabled(job is not None and client.can(job, key))
            if key in ("cancel_test", "resume", "release"):
                button.setVisible(job is not None and client.can(job, key))
            if key in ("test", "pause", "hold"):
                partner = {"test": "cancel_test", "pause": "resume", "hold": "release"}[key]
                button.setVisible(not (job is not None and client.can(job, partner)))
        self.up.setEnabled(job is not None)
        self.down.setEnabled(job is not None)
        self.details.show_job(job)

    # -- actions ----------------------------------------------------------------------

    def _report(self) -> DetectionReport | None:
        return self.report_source()

    def _add(self) -> None:
        if JobDialog(self.db, self._report(), self.gpus, parent=self).exec():
            self.refresh()
            self.table.selectRow(len(self.model.jobs) - 1)

    def _batch(self) -> None:
        dialog = BatchAddDialog(self.db, self._report(), self)
        if dialog.exec():
            self.refresh()

    def _toggle_queue(self) -> None:
        running = bool(self.db.setting("queue_running"))
        client.set_queue_running(self.db, not running)
        self.refresh()

    def _verify_changed(self) -> None:
        if not self._loading:
            self.db.set_setting("verify_mode", self.verify.currentData())

    def _move(self, direction: int) -> None:
        job = self.selected()
        if job is not None:
            client.move_job(self.db, job.id, direction)
            self.refresh()
            for row, j in enumerate(self.model.jobs):
                if j.id == job.id:
                    self.table.selectRow(row)

    def _use_cores(self, job_id: int, cores: int) -> None:
        try:
            client.update_job(self.db, job_id, cores=cores)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot change", str(exc))
        self.db.request("check", job_id)
        self.refresh()

    def _do(self, action: str) -> None:
        job = self.selected()
        if job is None or not client.can(job, action):
            return
        if action == "edit":
            if JobDialog(self.db, self._report(), self.gpus, job=job, parent=self).exec():
                self.refresh()
            return
        if action == "duplicate":
            new_id = client.duplicate_job(self.db, job.id)
            source = self.db.job(new_id)
            dialog = JobDialog(self.db, self._report(), self.gpus, job=source, parent=self)
            dialog.setWindowTitle("Duplicate: choose the new folder")
            if not dialog.exec():
                client.remove_job(self.db, new_id)
            self.refresh()
            return
        if action == "remove":
            answer = QMessageBox.question(
                self,
                "Remove job",
                f"Remove “{job.name}” from the queue?\n\nIts folder and files are not touched.",
            )
            if answer == QMessageBox.StandardButton.Yes:
                try:
                    client.remove_job(self.db, job.id)
                except ValueError as exc:
                    QMessageBox.warning(self, "Cannot remove", str(exc))
            self.refresh()
            return
        if action == "kill":
            answer = QMessageBox.question(
                self,
                "Kill job",
                f"Stop “{job.name}” now?\n\nIts restart files stay, so "
                "Run again continues a UDB job where it stopped.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if action == "hold":
            client.hold(self.db, job.id)
        elif action == "release":
            client.release(self.db, job.id)
        elif action == "corescan":
            counts = _scan_counts(client.queue_cpus(self.db), job.cores)
            answer = QMessageBox.question(
                self,
                "Core-count test",
                "Run a short test at " + ", ".join(map(str, counts)) + " CPUs to see how speed "
                "changes with the CPU count?\n\nEach test is a few minutes long and uses the "
                "CPUs while it runs.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.db.request("corescan", job.id, counts)
        else:
            self.db.request(action, job.id)
        self.refresh()


def _scan_counts(total: int, current: int) -> list[int]:
    options = {c for c in (2, 4, 8, 16, 24, 32, 48, 64, 96, 128) if c <= total}
    options.update({total, min(current, total)})
    return sorted(options)[-4:]
