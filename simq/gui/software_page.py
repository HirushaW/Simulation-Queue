"""The Software page: simulation programs found on this machine."""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QListWidget,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..detect.software import (
    GROUP_ORDER,
    DetectionReport,
    Installation,
    detect_software,
    last_report,
)
from . import theme
from .widgets import Chip, label

COLUMNS = ("Program", "Version", "Build", "GPU", "Location", "Found via")
COL_PROGRAM, COL_VERSION, COL_BUILD, COL_GPU, COL_PATH, COL_VIA = range(len(COLUMNS))

# The engines summarised as chips above the list.
HEADLINE_GROUPS = ("NAMD", "AMBER", "GROMACS", "LAMMPS")
# Within a group, which program best represents it in the summary.
_PREFERRED = ("namd3", "namd2", "pmemd.cuda", "pmemd", "sander", "gmx", "gmx_mpi", "lmp")


class _DetectWorker(QObject):
    finished = Signal(object)  # DetectionReport, or the exception that stopped it

    def __init__(self, config: Config) -> None:
        super().__init__()
        self._config = config

    @Slot()
    def run(self) -> None:
        try:
            result: object = detect_software(self._config.search_dirs, self._config.programs)
        except Exception as exc:  # report it in the window instead of losing it
            result = exc
        self.finished.emit(result)


class SearchFoldersDialog(QDialog):
    """Lets the user add or remove extra folders for the software scan."""

    def __init__(self, folders: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Search folders")
        self.setMinimumSize(520, 340)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addWidget(
            label(
                "SimQ already looks in PATH, /opt, /usr/local, ~/.local/bin and the folders "
                "inside your home folder. Add a folder here if a program is installed "
                "somewhere else; SimQ also searches the folders inside it.",
                "Muted",
                wrap=True,
            )
        )
        self.list = QListWidget()
        self.list.addItems(folders)
        lay.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        add = QPushButton("Add folder…")
        remove = QPushButton("Remove")
        done = QPushButton("Done")
        done.setObjectName("Primary")
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        buttons.addWidget(done)
        lay.addLayout(buttons)
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        done.clicked.connect(self.accept)
        remove.setEnabled(False)
        self.list.itemSelectionChanged.connect(
            lambda: remove.setEnabled(bool(self.list.selectedItems()))
        )

    def folders(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def _add(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder to search")
        if folder and folder not in self.folders():
            self.list.addItem(folder)

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))


class SoftwarePage(QWidget):
    report_ready = Signal(object)  # DetectionReport

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Page")
        self._config = Config.load()
        self._thread: QThread | None = None
        self._worker: _DetectWorker | None = None
        self._report: DetectionReport | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 28)
        lay.setSpacing(14)

        title_row = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("Software", "PageTitle"))
        titles.addWidget(
            label(
                "Simulation programs found on this machine. Jobs are "
                "started with these paths and environments.",
                "PageSubtitle",
            )
        )
        title_row.addLayout(titles, 1)
        self.scan_button = QPushButton("Rescan")
        self.scan_button.setObjectName("Primary")
        self.add_button = QPushButton("Add program…")
        self.folders_button = QPushButton("Search folders…")
        for b in (self.folders_button, self.add_button, self.scan_button):
            title_row.addWidget(b, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(title_row)

        self.chips_row = QHBoxLayout()
        self.chips_row.setSpacing(8)
        lay.addLayout(self.chips_row)

        self.amber_line = label("", "Muted", wrap=True)
        lay.addWidget(self.amber_line)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        header = self.tree.header()
        header.setStretchLastSection(False)
        for col in range(len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_PATH, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.tree, 1)

        self.status = label("", "Faint")
        lay.addWidget(self.status)

        self.scan_button.clicked.connect(self.rescan)
        self.add_button.clicked.connect(self._add_program)
        self.folders_button.clicked.connect(self._edit_folders)
        self.tree.customContextMenuRequested.connect(self._context_menu)

        cached = last_report()
        if cached is not None:
            self._show(cached, stale=True)

    # -- scanning ---------------------------------------------------------

    def rescan(self) -> None:
        if self._thread is not None:
            return
        self.scan_button.setEnabled(False)
        self.scan_button.setText("Scanning…")
        self.status.setText("Looking for simulation software…")
        self._thread = QThread()
        self._worker = _DetectWorker(self._config)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    @Slot(object)
    def _on_finished(self, result: object) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread.deleteLater()
        if self._worker is not None:
            self._worker.deleteLater()
        self._thread = self._worker = None
        self.scan_button.setEnabled(True)
        self.scan_button.setText("Rescan")
        if isinstance(result, DetectionReport):
            self._show(result)
            self.report_ready.emit(result)
        else:
            self.status.setText(f"The scan failed: {result}")
            self.status.setStyleSheet(f"color: {theme.BAD};")

    def shutdown(self) -> None:
        """Called when the window closes: let a running scan finish cleanly."""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(30000)

    # -- display ------------------------------------------------------------

    def _show(self, report: DetectionReport, stale: bool = False) -> None:
        self._report = report
        groups = report.by_group()
        self._fill_chips(groups)
        self._fill_tree(groups)

        if report.amber_home:
            script = "amber.sh found" if report.amber_sh else "no amber.sh next to it"
            self.amber_line.setText(f"AMBERHOME  {report.amber_home}   ·   {script}")
            self.amber_line.show()
        else:
            self.amber_line.hide()

        found = sum(1 for i in report.installations if not i.missing)
        self.status.setStyleSheet("")
        if stale:
            self.status.setText(f"{found} programs from the last scan · rescanning…")
        else:
            self.status.setText(
                f"{found} programs found · {report.folders_searched} folders searched "
                f"in {report.seconds:.1f} s"
            )

    def _fill_chips(self, groups: dict[str, list[Installation]]) -> None:
        while self.chips_row.count():
            item = self.chips_row.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for group in HEADLINE_GROUPS:
            installs = [i for i in groups.get(group, []) if not i.missing]
            if not installs:
                chip = Chip(f"{group}  ·  not found")
                chip.setStyleSheet(f"color: {theme.FAINT};")
            else:
                best = _representative(installs)
                parts = [group]
                if best.version:
                    parts.append(best.version)
                if any(i.gpu for i in installs):
                    parts.append("GPU")
                chip = Chip("✔  " + "  ·  ".join(parts))
                chip.setStyleSheet(f"color: {theme.OK}; border-color: #1f5e47;")
            self.chips_row.addWidget(chip)
        self.chips_row.addStretch(1)

    def _fill_tree(self, groups: dict[str, list[Installation]]) -> None:
        self.tree.clear()
        bold = QFont(self.tree.font())
        bold.setWeight(QFont.Weight.DemiBold)
        for group in GROUP_ORDER:
            installs = groups.get(group, [])
            if group == "Added by you" and not installs:
                continue
            found = sum(1 for i in installs if not i.missing)
            head = QTreeWidgetItem(self.tree)
            head.setFirstColumnSpanned(True)
            head.setFlags(Qt.ItemFlag.ItemIsEnabled)
            missing = len(installs) - found
            if found:
                status = f"{found} found"
            elif missing:
                status = f"{missing} missing"
            else:
                status = "not found"
            head.setText(0, f"{group}   ·   {status}")
            head.setFont(0, bold)
            head.setForeground(0, QColor(theme.TEXT if found else theme.FAINT))
            for inst in installs:
                self._add_row(head, inst)
            head.setExpanded(True)

    def _add_row(self, parent: QTreeWidgetItem, inst: Installation) -> None:
        item = QTreeWidgetItem(parent)
        item.setData(0, Qt.ItemDataRole.UserRole, inst)
        mark = "✘" if inst.missing else "✔"
        item.setText(COL_PROGRAM, f"   {mark}  {inst.name}")
        item.setForeground(COL_PROGRAM, QColor(theme.BAD if inst.missing else theme.TEXT))
        if inst.missing:
            item.setText(COL_VERSION, "file not found")
            item.setForeground(COL_VERSION, QColor(theme.BAD))
        else:
            item.setText(COL_VERSION, inst.version or "–")
        item.setText(COL_BUILD, inst.build or "")
        if inst.gpu is True:
            item.setText(COL_GPU, "GPU")
            item.setForeground(COL_GPU, QColor(theme.OK))
        elif inst.gpu is False:
            item.setText(COL_GPU, "CPU")
            item.setForeground(COL_GPU, QColor(theme.MUTED))
        item.setText(COL_PATH, inst.path)
        tip = inst.path if inst.path == inst.realpath else f"{inst.path}\n→ {inst.realpath}"
        item.setToolTip(COL_PATH, tip)
        item.setText(COL_VIA, inst.found_via)
        item.setForeground(COL_VIA, QColor(theme.MUTED))

    # -- actions ------------------------------------------------------------

    def _context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        inst = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(inst, Installation):
            return
        menu = QMenu(self)
        copy = menu.addAction("Copy path")
        folder = menu.addAction("Open containing folder")
        remove = menu.addAction("Remove from list") if inst.found_via == "added by you" else None
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is copy:
            QGuiApplication.clipboard().setText(inst.path)
        elif chosen is folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(inst.path)))
        elif remove is not None and chosen is remove:
            self._config.programs = [
                p for p in self._config.programs if os.path.expanduser(p) != inst.path
            ]
            self._config.save()
            self.rescan()

    def _add_program(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a program")
        if not path:
            return
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            QMessageBox.warning(
                self,
                "Not a program",
                f"{path}\n\nThis file is not executable, so it cannot be run as a program.",
            )
            return
        if path not in self._config.programs:
            self._config.programs.append(path)
            self._config.save()
        self.rescan()

    def _edit_folders(self) -> None:
        dialog = SearchFoldersDialog(self._config.search_dirs, self)
        dialog.exec()
        folders = dialog.folders()
        if folders != self._config.search_dirs:
            self._config.search_dirs = folders
            self._config.save()
            self.rescan()


def _representative(installs: list[Installation]) -> Installation:
    def rank(inst: Installation) -> tuple[int, int]:
        pref = _PREFERRED.index(inst.name) if inst.name in _PREFERRED else len(_PREFERRED)
        return (0 if inst.version else 1, pref)

    return min(installs, key=rank)
