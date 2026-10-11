"""Start the SimQ window."""

from __future__ import annotations

import os
import sys


def run(demo: bool = False) -> int:
    # pyqtgraph must bind to PySide6 even if PyQt is also installed.
    os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")
    from PySide6.QtWidgets import QApplication

    from .. import APP_NAME
    from .main_window import MainWindow
    from .theme import apply_theme

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName("simq")
    apply_theme(app)

    window = MainWindow(demo=demo)
    window.show()
    return app.exec()
