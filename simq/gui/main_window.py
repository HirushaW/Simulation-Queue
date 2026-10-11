"""The main window: a sidebar on the left, the selected page on the right."""

from __future__ import annotations

from PySide6.QtCore import Qt
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
from ..monitor.gpu import GpuMonitor
from ..monitor.system import host_info
from .dashboard import DashboardPage
from .sampler import Monitor
from .software_page import SoftwarePage
from .widgets import label


class PlaceholderPage(QWidget):
    """A page whose feature arrives in a later build phase."""

    def __init__(self, title: str, text: str):
        super().__init__()
        self.setObjectName("Page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 28)
        lay.addWidget(label(title, "PageTitle"))
        lay.addWidget(label(text, "PageSubtitle", wrap=True))
        lay.addStretch(1)


class MainWindow(QMainWindow):
    # (name, available now?, what it will do)
    PAGES = (
        ("Dashboard", True, ""),
        ("Queue", False, "Add, check, run and track simulations. Coming in the next build."),
        ("Software", True, ""),
        ("History", False, "Finished jobs and measured speeds. Coming in a later build."),
        ("Settings", False, "Cores, email and health-watch limits. Coming in a later build."),
    )

    def __init__(self, demo: bool = False):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · Simulation Queue")
        self.resize(1320, 900)
        self.setMinimumSize(980, 640)

        self.gpu = GpuMonitor(demo=demo)
        host = host_info()

        self.dashboard = DashboardPage(host, self.gpu.info)
        self.software = SoftwarePage()
        pages = {
            "Dashboard": self.dashboard,
            "Software": self.software,
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
        for name, ready, text in self.PAGES:
            page = pages[name] if ready else PlaceholderPage(name, text)
            index = self.stack.addWidget(page)
            button = QPushButton(name if ready else f"{name}   ·  soon")
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setEnabled(ready)
            if not ready:
                button.setToolTip(text)
            button.clicked.connect(lambda _=False, i=index: self.stack.setCurrentIndex(i))
            self.nav.addButton(button, index)
            lay.addWidget(button)
        self.nav.button(0).setChecked(True)

        lay.addStretch(1)
        lay.addWidget(label(f"Version {__version__}", "Version"))
        return bar

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt API)
        self.monitor.stop()
        self.software.shutdown()
        self.gpu.close()
        super().closeEvent(event)
