"""Builds the real window off-screen and feeds it samples."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

try:
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # e.g. libEGL missing on a bare server
    pytest.skip(f"Qt is not usable here: {exc}", allow_module_level=True)

from simq.gui.main_window import MainWindow  # noqa: E402
from simq.gui.sampler import Snapshot  # noqa: E402
from simq.gui.theme import apply_theme  # noqa: E402
from simq.monitor.system import SystemSampler  # noqa: E402


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    return app


def test_window_builds_and_updates(app, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    window = MainWindow(demo=True)
    window.resize(1200, 900)
    window.show()
    sampler = SystemSampler()
    for _ in range(3):
        snap = Snapshot(sampler.sample(), tuple(window.gpu.sample()))
        window.dashboard.update_from(snap)
        app.processEvents()
    assert len(window.dashboard.gpu_cards) == 2
    assert window.dashboard.disks_card._rows
    assert not window.grab().isNull()
    # Pages that are not built yet cannot be opened.
    assert not window.nav.button(1).isEnabled()
    window.nav.button(2).click()
    assert window.stack.currentWidget() is window.software
    window.close()


def test_window_without_gpu(app, monkeypatch):
    from simq.monitor import gpu

    monkeypatch.setattr(gpu, "_auto_backend", lambda: (None, "No NVIDIA GPU detected."))
    window = MainWindow()
    assert not window.gpu.available
    assert window.dashboard.gpu_cards == {}
    window.close()
