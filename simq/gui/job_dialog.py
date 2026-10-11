"""Add or edit a job; and add many folders at once."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..analyze.folder import KIND_LABELS, KINDS, inspect_folder
from ..db import Database, Job
from ..detect.software import DetectionReport
from ..engine import client
from ..monitor.gpu import GpuSystemInfo
from .widgets import label

FILE_LABELS = {
    "udb": "Launcher",
    "namd": "Configuration",
    "amber": "mdin file",
    "command": "Script",
}


class JobDialog(QDialog):
    """Add a new job (``job`` is None) or edit an existing one."""

    def __init__(
        self,
        db: Database,
        report: DetectionReport | None,
        gpus: GpuSystemInfo,
        job: Job | None = None,
        folder: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.db = db
        self.report = report
        self.job = job
        self.budget = client.queue_cpus(db)
        self._command_edited = False
        self._filling = False
        self.setWindowTitle("Edit job" if job else "Add a job")
        self.setMinimumWidth(720)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)
        lay.addWidget(label("Edit job" if job else "Add a job", "PageTitle"))

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        folder_row = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("The folder you would run the simulation in")
        browse = QPushButton("Browse…")
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        form.addRow("Folder", folder_row)
        self.detected = label("", "Small", wrap=True)
        form.addRow("", self.detected)

        self.name = QLineEdit()
        form.addRow("Name", self.name)

        self.kind = QComboBox()
        for kind in KINDS:
            self.kind.addItem(KIND_LABELS[kind], kind)
        form.addRow("Type", self.kind)

        self.choice = QComboBox()
        self.choice_label = label("Launcher")
        form.addRow(self.choice_label, self.choice)

        self.command = QLineEdit()
        form.addRow("Command", self.command)
        form.addRow(
            "",
            label(
                "{cores} and {gpu} are filled in when the job starts. The "
                "command runs inside the folder, as in a terminal.",
                "Small",
                wrap=True,
            ),
        )

        res_row = QHBoxLayout()
        self.cores = QSpinBox()
        self.cores.setRange(1, max(1, self.budget))
        self.cores.setMinimumWidth(90)
        res_row.addWidget(self.cores)
        res_row.addWidget(label(f"of {self.budget} CPUs", "Muted"))
        res_row.addSpacing(24)
        res_row.addWidget(label("GPU"))
        self.gpu = QComboBox()
        self.gpu.addItem("No GPU", "none")
        if gpus.gpus:
            self.gpu.addItem("Any free GPU", "auto")
            for g in gpus.gpus:
                self.gpu.addItem(f"GPU {g.index} · {g.name}", str(g.index))
        res_row.addWidget(self.gpu, 1)
        form.addRow("CPUs", res_row)
        self.reason = label("", "Small", wrap=True)
        form.addRow("", self.reason)

        start_box = QVBoxLayout()
        start_box.setSpacing(6)
        self.start_group = QButtonGroup(self)
        self.start_queue = QRadioButton("When the queue reaches it")
        self.start_time = QRadioButton("Not before")
        self.start_after = QRadioButton("After job")
        for b in (self.start_queue, self.start_time, self.start_after):
            self.start_group.addButton(b)
        self.when = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600))
        self.when.setCalendarPopup(True)
        self.when.setDisplayFormat("ddd d MMM yyyy  HH:mm")
        self.after = QComboBox()
        for other in db.jobs():
            if job is None or other.id != job.id:
                self.after.addItem(f"#{other.id} {other.name}", other.id)
        row_time = QHBoxLayout()
        row_time.addWidget(self.start_time)
        row_time.addWidget(self.when, 1)
        row_after = QHBoxLayout()
        row_after.addWidget(self.start_after)
        row_after.addWidget(self.after, 1)
        start_box.addWidget(self.start_queue)
        start_box.addLayout(row_time)
        start_box.addLayout(row_after)
        self.start_queue.setChecked(True)
        if self.after.count() == 0:
            self.start_after.setEnabled(False)
        form.addRow("Start", start_box)

        more = QHBoxLayout()
        self.retries = QSpinBox()
        self.retries.setRange(0, 10)
        self.retries.setMinimumWidth(90)
        more.addWidget(self.retries)
        more.addWidget(label("automatic retries after a crash", "Muted"))
        more.addSpacing(24)
        self.hours = QDoubleSpinBox()
        self.hours.setRange(0, 10000)
        self.hours.setDecimals(1)
        self.hours.setSpecialValueText("unknown")
        self.hours.setMinimumWidth(110)
        self.hours_label = label("Your estimate (hours)")
        more.addWidget(self.hours_label)
        more.addWidget(self.hours)
        more.addStretch(1)
        form.addRow("Retries", more)
        lay.addLayout(form)

        self.check_now = QCheckBox("Run the quick check as soon as it is added")
        self.check_now.setChecked(True)
        lay.addWidget(self.check_now)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        self.ok = QPushButton("Save" if job else "Add to queue")
        self.ok.setObjectName("Primary")
        buttons.addWidget(cancel)
        buttons.addWidget(self.ok)
        lay.addLayout(buttons)

        browse.clicked.connect(self._browse)
        self.folder.editingFinished.connect(self._folder_changed)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.choice.currentIndexChanged.connect(self._choice_changed)
        self.gpu.currentIndexChanged.connect(self._gpu_changed)
        self.command.textEdited.connect(self._mark_edited)
        cancel.clicked.connect(self.reject)
        self.ok.clicked.connect(self._accept)

        if job is not None:
            self._load(job)
        elif folder:
            self.folder.setText(folder)
            self._folder_changed()
        else:
            self.retries.setValue(client.default_retries(db, "command"))
        self._update_visibility()

    # -- filling in -------------------------------------------------------------

    def _browse(self) -> None:
        start = self.folder.text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Choose the simulation folder", start)
        if folder:
            self.folder.setText(folder)
            self._folder_changed()

    def _mark_edited(self) -> None:
        self._command_edited = True

    def _folder_changed(self) -> None:
        folder = self.folder.text().strip()
        if not folder or not Path(folder).is_dir():
            self.detected.setText("This folder does not exist." if folder else "")
            return
        guess = inspect_folder(folder)
        self.detected.setText("Detected: " + guess.summary)
        if not self.name.text() or self.job is None:
            self.name.setText(Path(folder).name)
        self._command_edited = False
        self._set_kind(guess.kind)

    def _set_kind(self, kind: str) -> None:
        self._filling = True
        self.kind.setCurrentIndex(max(0, self.kind.findData(kind)))
        self._filling = False
        self._kind_changed()

    def _kind_changed(self) -> None:
        if self._filling:
            return
        folder = self.folder.text().strip()
        kind = self.kind.currentData()
        self.choice_label.setText(FILE_LABELS.get(kind, "File"))
        self._filling = True
        self.choice.clear()
        if folder and Path(folder).is_dir():
            for name in inspect_folder(folder).choices.get(kind, []):
                self.choice.addItem(name)
        self._filling = False
        if self.job is None:
            self.retries.setValue(client.default_retries(self.db, kind))
        self._choice_changed()

    def _choice_changed(self) -> None:
        if self._filling:
            return
        self._update_visibility()
        folder = self.folder.text().strip()
        if not folder or not Path(folder).is_dir():
            return
        kind = self.kind.currentData()
        choice = self.choice.currentText() or None
        values = client.suggest(folder, self.report, self.budget, kind, choice)
        if not self._command_edited:
            self.command.setText(values["command"])
        self.cores.setValue(values["cores"])
        index = self.gpu.findData(values["gpu"])
        if index < 0 and values["gpu"] != "none":
            index = self.gpu.findData("auto")
        self._filling = True
        self.gpu.setCurrentIndex(max(0, index))
        self._filling = False
        self.reason.setText(values["reason"])

    def _gpu_changed(self) -> None:
        if self._filling or self._command_edited:
            return
        folder = self.folder.text().strip()
        if folder and Path(folder).is_dir():
            from ..analyze.folder import default_command

            self.command.setText(
                default_command(
                    self.kind.currentData(),
                    folder,
                    self.choice.currentText() or None,
                    self.gpu.currentData() != "none",
                    self.report,
                )
            )

    def _update_visibility(self) -> None:
        kind = self.kind.currentData()
        self.choice.setVisible(self.choice.count() > 0)
        self.choice_label.setVisible(self.choice.count() > 0)
        generic = kind == "command"
        self.hours.setVisible(generic)
        self.hours_label.setVisible(generic)

    def _load(self, job: Job) -> None:
        self._filling = True
        self.folder.setText(job.folder)
        self.name.setText(job.name)
        self.kind.setCurrentIndex(max(0, self.kind.findData(job.kind)))
        for name in inspect_folder(job.folder).choices.get(job.kind, []):
            self.choice.addItem(name)
        if job.choice:
            if self.choice.findText(job.choice) < 0:
                self.choice.addItem(job.choice)
            self.choice.setCurrentText(job.choice)
        self.command.setText(job.command)
        self.cores.setValue(job.cores)
        self.gpu.setCurrentIndex(max(0, self.gpu.findData(job.gpu)))
        self.retries.setValue(job.retries)
        self.hours.setValue(job.manual_hours or 0)
        if job.start_at:
            self.start_time.setChecked(True)
            self.when.setDateTime(QDateTime.fromSecsSinceEpoch(int(job.start_at)))
        elif job.after_job and self.after.findData(job.after_job) >= 0:
            self.start_after.setChecked(True)
            self.after.setCurrentIndex(self.after.findData(job.after_job))
        self.detected.setText("Detected: " + inspect_folder(job.folder).summary)
        self._filling = False
        self._command_edited = True  # keep the saved command as it is
        self.check_now.setText("Run the quick check after saving")

    # -- result -----------------------------------------------------------------

    def values(self) -> dict[str, Any]:
        start_at = after = None
        if self.start_time.isChecked():
            start_at = float(self.when.dateTime().toSecsSinceEpoch())
        elif self.start_after.isChecked():
            after = self.after.currentData()
        return {
            "name": self.name.text().strip() or Path(self.folder.text()).name,
            "kind": self.kind.currentData(),
            "choice": self.choice.currentText() or None,
            "command": self.command.text().strip(),
            "cores": self.cores.value(),
            "gpu": self.gpu.currentData(),
            "retries": self.retries.value(),
            "start_at": start_at,
            "after_job": after,
            "manual_hours": self.hours.value() or None,
        }

    def _accept(self) -> None:
        folder = self.folder.text().strip()
        if not folder or not Path(folder).is_dir():
            QMessageBox.warning(self, "No folder", "Choose an existing folder for the job.")
            return
        values = self.values()
        if not values["command"]:
            QMessageBox.warning(
                self, "No command", "Type the command that runs the simulation in this folder."
            )
            return
        if values["start_at"] and values["start_at"] < time.time() - 60:
            QMessageBox.warning(self, "Start time", "The start time is in the past.")
            return
        try:
            if self.job is None:
                job_id = client.add_job(self.db, folder, self.report, **values)
            else:
                job_id = self.job.id
                values["folder"] = str(Path(folder).resolve())
                client.update_job(self.db, job_id, **values)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Cannot save", str(exc))
            return
        if self.check_now.isChecked():
            self.db.request("check", job_id)
        self.accept()


class BatchAddDialog(QDialog):
    """Pick a parent folder; add every simulation folder found inside it."""

    def __init__(self, db: Database, report: DetectionReport | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.db = db
        self.report = report
        self.added = 0
        self.setWindowTitle("Add folders")
        self.setMinimumSize(760, 480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(label("Add many folders", "PageTitle"))
        lay.addWidget(
            label(
                "Choose a folder that holds several simulations. Every folder in "
                "it (two levels deep) that SimQ recognises is listed; tick the ones "
                "to add. Each gets the usual defaults, which you can edit later.",
                "Muted",
                wrap=True,
            )
        )
        row = QHBoxLayout()
        self.root = QLineEdit()
        browse = QPushButton("Browse…")
        row.addWidget(self.root, 1)
        row.addWidget(browse)
        lay.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Folder", "Type", "What SimQ found"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.tree, 1)
        buttons = QHBoxLayout()
        self.status = label("", "Small")
        buttons.addWidget(self.status, 1)
        cancel = QPushButton("Cancel")
        self.ok = QPushButton("Add")
        self.ok.setObjectName("Primary")
        self.ok.setEnabled(False)
        buttons.addWidget(cancel)
        buttons.addWidget(self.ok)
        lay.addLayout(buttons)
        browse.clicked.connect(self._browse)
        self.root.editingFinished.connect(self._scan)
        cancel.clicked.connect(self.reject)
        self.ok.clicked.connect(self._add)
        self.tree.itemChanged.connect(self._count)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Folder holding the simulations", self.root.text() or str(Path.home())
        )
        if folder:
            self.root.setText(folder)
            self._scan()

    def _scan(self) -> None:
        self.tree.clear()
        root = Path(self.root.text().strip())
        if not root.is_dir():
            self.status.setText("Not a folder.")
            return
        candidates = [root] + sorted(p for p in root.iterdir() if p.is_dir())
        candidates += sorted(q for p in root.iterdir() if p.is_dir()
                             for q in p.iterdir() if q.is_dir())  # fmt: skip
        known = {str(Path(j.folder)) for j in self.db.jobs()}
        for folder in candidates:
            if folder.name.startswith(".") or folder.name == "logs":
                continue
            guess = inspect_folder(folder)
            if guess.kind == "command" and not guess.choices.get("command"):
                continue
            item = QTreeWidgetItem(self.tree)
            item.setText(0, str(folder.relative_to(root)) if folder != root else ".")
            item.setText(1, KIND_LABELS[guess.kind])
            already = str(folder.resolve()) in known
            item.setText(2, ("already in the queue · " if already else "") + guess.summary)
            item.setData(0, Qt.ItemDataRole.UserRole, str(folder))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked if already or guess.kind == "command"
                               else Qt.CheckState.Checked)  # fmt: skip
        for col in (0, 1):
            self.tree.resizeColumnToContents(col)
        self._count()

    def _checked(self) -> list[str]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.checkState(0) == Qt.CheckState.Checked:
                out.append(item.data(0, Qt.ItemDataRole.UserRole))
        return out

    def _count(self) -> None:
        n = len(self._checked())
        found = self.tree.topLevelItemCount()
        self.status.setText(f"{found} simulation folders found · {n} selected")
        self.ok.setText(f"Add {n} job{'s' if n != 1 else ''}")
        self.ok.setEnabled(n > 0)

    def _add(self) -> None:
        problems = []
        for folder in self._checked():
            try:
                job_id = client.add_job(self.db, folder, self.report)
                self.db.request("check", job_id)
                self.added += 1
            except (ValueError, OSError) as exc:
                problems.append(f"{folder}: {exc}")
        if problems:
            QMessageBox.warning(self, "Some folders were not added", "\n".join(problems))
        self.accept()
