"""The main window: a sidebar on the left, the selected page on the right."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..db import PAUSED, RUNNING, Database
from ..detect.software import DetectionReport, last_report
from ..engine import client
from ..monitor.gpu import GpuMonitor
from ..monitor.system import host_info
from . import theme
from .dashboard import DashboardPage
from .history_page import HistoryPage
from .queue_page import QueuePage
from .sampler import Monitor
from .settings_page import SettingsPage
from .software_page import SoftwarePage
from .widgets import label

PAGES = ("Dashboard", "Queue", "Software", "History", "Settings")


class MainWindow(QMainWindow):
    def __init__(self, demo: bool = False, start_service: bool = True):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · Simulation Queue")
        self.resize(1360, 920)
        self.setMinimumSize(1060, 680)

        self.db = Database()
        if start_service and not client.service_status(self.db)["running"]:
            client.start_service(wait=0, db=self.db)

        self.gpu = GpuMonitor(demo=demo)
        self.report: DetectionReport | None = last_report()
        host = host_info()

        self.dashboard = DashboardPage(host, self.gpu.info)
        self.queue = QueuePage(self.db, self.gpu.info, lambda: self.report)
        self.software = SoftwarePage()
        self.history = HistoryPage(self.db)
        self.settings_page = SettingsPage(self.db)
        self.software.report_ready.connect(self._software_found)
        pages = {
            "Dashboard": self.dashboard,
            "Queue": self.queue,
            "Software": self.software,
            "History": self.history,
            "Settings": self.settings_page,
        }

        self.stack = QStackedWidget()
        root = QWidget()
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._sidebar(pages))
        row.addWidget(self.stack, 1)
        self.setCentralWidget(root)

        self.monitor = Monitor(self.gpu)
        self.monitor.sampled.connect(self.dashboard.update_from)
        self.monitor.start()
        self.software.rescan()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.refresh()

    def _sidebar(self, pages: dict[str, QWidget]) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(208)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(14, 22, 14, 16)
        lay.setSpacing(4)
        lay.addWidget(label(APP_NAME, "AppName"))
        lay.addWidget(label("Simulation Queue", "AppTagline"))
        lay.addSpacing(22)

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.nav_buttons: dict[str, QPushButton] = {}
        for name in PAGES:
            index = self.stack.addWidget(pages[name])
            button = QPushButton(name)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, i=index: self._show_page(i))
            self.nav.addButton(button, index)
            self.nav_buttons[name] = button
            lay.addWidget(button)
        self.nav.button(0).setChecked(True)

        lay.addStretch(1)
        self.service_label = label("", "Status")
        self.service_label.setWordWrap(True)
        lay.addWidget(self.service_label)
        self.start_service_button = QPushButton("Start service")
        self.start_service_button.clicked.connect(lambda: client.start_service(wait=0, db=self.db))
        lay.addWidget(self.start_service_button)
        lay.addSpacing(6)
        lay.addWidget(label(f"Version {__version__}", "Version"))
        return bar

    def _show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.refresh()

    def _software_found(self, report: DetectionReport) -> None:
        self.report = report
        self.db.request("rescan_software")  # the service reads the new results

    def refresh(self) -> None:
        status = client.service_status(self.db)
        if status["running"]:
            self.service_label.setText("● Queue service running")
            self.service_label.setStyleSheet(f"color: {theme.OK};")
        else:
            self.service_label.setText("● Queue service stopped — jobs will not start")
            self.service_label.setStyleSheet(f"color: {theme.BAD};")
        self.start_service_button.setVisible(not status["running"])

        jobs = self.db.jobs()
        active = [j for j in jobs if j.state in (RUNNING, PAUSED)]
        queued = sum(1 for j in jobs if j.state == "queued")
        text = "Queue"
        if active:
            text += f"   ·  {len(active)} running"
        elif queued:
            text += f"   ·  {queued} waiting"
        self.nav_buttons["Queue"].setText(text)

        page = self.stack.currentWidget()
        if page is self.dashboard:
            runs = {r.job_id: r for r in self.db.runs(RUNNING) if r.purpose == "job"}
            self.dashboard.jobs_card.update_jobs([(j, runs[j.id]) for j in active if j.id in runs])
        elif page is self.queue:
            self.queue.refresh()
        elif page is self.history:
            self.history.refresh()
        elif page is self.settings_page:
            self.settings_page.refresh()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt API)
        # Only the window closes: the queue service and its jobs keep running.
        self.timer.stop()
        self.monitor.stop()
        self.software.shutdown()
        self.gpu.close()
        super().closeEvent(event)
