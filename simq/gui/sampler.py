"""Background thread that samples the hardware once a second.

Sampling runs off the GUI thread so a slow call (a busy disk, a hung network
mount, a slow ``nvidia-smi``) can never freeze the window.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from ..monitor.gpu import GpuMonitor, GpuSample
from ..monitor.system import SystemSampler, SystemSnapshot


@dataclass(frozen=True)
class Snapshot:
    system: SystemSnapshot
    gpus: tuple[GpuSample, ...]


class _Worker(QObject):
    sampled = Signal(object)  # Snapshot

    def __init__(self, gpu: GpuMonitor, interval_ms: int) -> None:
        super().__init__()
        self._gpu = gpu
        self._interval_ms = interval_ms
        self._sampler: SystemSampler | None = None
        self._timer: QTimer | None = None

    @Slot()
    def start(self) -> None:
        self._sampler = SystemSampler()
        self._timer = QTimer(self)
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        # The first CPU reading needs a moment after priming; take it shortly.
        QTimer.singleShot(250, self._tick)

    @Slot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        if self._sampler is not None:
            self._sampler.close()
            self._sampler = None

    @Slot()
    def _tick(self) -> None:
        if self._sampler is None:
            return
        self.sampled.emit(Snapshot(self._sampler.sample(), tuple(self._gpu.sample())))


class Monitor(QObject):
    """Owns the sampling thread; connect to :attr:`sampled`."""

    sampled = Signal(object)  # Snapshot
    _stop_requested = Signal()

    def __init__(
        self, gpu: GpuMonitor, interval_ms: int = 1000, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._thread = QThread()
        self._thread.setObjectName("simq-monitor")
        self._worker = _Worker(gpu, interval_ms)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self._worker.sampled.connect(self.sampled)
        # Blocking: stop() returns only after the worker has stopped its timer.
        self._stop_requested.connect(self._worker.stop, Qt.ConnectionType.BlockingQueuedConnection)
        self._thread.finished.connect(self._worker.deleteLater)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if not self._thread.isRunning():
            return
        self._stop_requested.emit()
        self._thread.quit()
        self._thread.wait(3000)
