"""The Dashboard page: live CPU, memory, GPU and disk use."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import fmt
from ..monitor.gpu import GpuInfo, GpuSample, GpuSystemInfo
from ..monitor.system import DiskUsage, HostInfo
from . import theme
from .sampler import Snapshot
from .widgets import (
    Card,
    Chip,
    CoreGrid,
    HistoryPlot,
    LegendDot,
    MeterBar,
    RingGauge,
    Stat,
    label,
)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


class HostCard(Card):
    def __init__(self, host: HostInfo, gpus: GpuSystemInfo):
        super().__init__()
        row = QHBoxLayout()
        row.setSpacing(16)
        names = QVBoxLayout()
        names.setSpacing(2)
        names.addWidget(label(host.hostname, "HostName"))
        self._details = label("", "Muted")
        names.addWidget(self._details)
        row.addLayout(names, 1)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        cpu = host.cpu
        sockets = f" × {cpu.sockets}" if cpu.sockets > 1 else ""
        texts = [
            cpu.model + sockets,
            f"{_plural(cpu.physical_cores, 'core')} · {_plural(cpu.logical_cpus, 'thread')}",
            f"{fmt.size(host.ram_total)} RAM",
            _plural(len(gpus.gpus), "GPU") if gpus.gpus else "No GPU",
        ]
        for text in texts:
            chips.addWidget(Chip(text))
        row.addLayout(chips)
        self.body.addLayout(row)
        self._host = host
        self.refresh()

    def refresh(self) -> None:
        uptime = fmt.duration(time.time() - self._host.boot_time)
        self._details.setText(f"{self._host.os_name}  ·  Linux {self._host.kernel}  ·  up {uptime}")


class CpuCard(Card):
    def __init__(self, host: HostInfo):
        super().__init__("CPU", host.cpu.model)
        top = QHBoxLayout()
        top.setSpacing(22)
        self.gauge = RingGauge(theme.CPU, "load")
        top.addWidget(self.gauge)
        stats = QGridLayout()
        stats.setHorizontalSpacing(26)
        stats.setVerticalSpacing(12)
        self.clock = Stat("Clock")
        self.load = Stat("Load average")
        self.temp = Stat("Temperature")
        self.busy = Stat("Busy threads")
        stats.addWidget(self.clock, 0, 0)
        stats.addWidget(self.temp, 0, 1)
        stats.addWidget(self.load, 1, 0)
        stats.addWidget(self.busy, 1, 1)
        top.addLayout(stats, 1)
        self.body.addLayout(top)
        self.plot = HistoryPlot([("cpu", theme.CPU)])
        self.body.addWidget(self.plot)
        self._threads = host.cpu.logical_cpus

    def update_from(self, snap: Snapshot) -> None:
        s = snap.system
        self.gauge.set_value(s.cpu_percent)
        self.clock.set(fmt.ghz(s.freq_mhz))
        self.load.set(" · ".join(f"{v:.1f}" for v in s.load_avg))
        if s.cpu_temp_c is None:
            self.temp.set("–")
        else:
            self.temp.set(f"{s.cpu_temp_c:.0f} °C", theme.temperature_color(s.cpu_temp_c))
        busy = sum(1 for v in s.per_cpu if v >= 50)
        self.busy.set(f"{busy} / {self._threads}")
        self.plot.push({"cpu": s.cpu_percent})


class MemoryCard(Card):
    def __init__(self, host: HostInfo):
        super().__init__("Memory", f"{fmt.size(host.ram_total)} installed")
        top = QHBoxLayout()
        top.setSpacing(22)
        self.gauge = RingGauge(theme.MEMORY, "used")
        top.addWidget(self.gauge)
        right = QVBoxLayout()
        right.setSpacing(12)
        stats = QHBoxLayout()
        stats.setSpacing(26)
        self.used = Stat("Used")
        self.available = Stat("Available")
        stats.addWidget(self.used)
        stats.addWidget(self.available)
        stats.addStretch(1)
        right.addLayout(stats)
        swap_row = QHBoxLayout()
        swap_row.addWidget(label("SWAP", "StatLabel"))
        swap_row.addStretch(1)
        self.swap_text = label("", "Faint")
        swap_row.addWidget(self.swap_text)
        right.addLayout(swap_row)
        self.swap_bar = MeterBar(theme.MEMORY, 6)
        right.addWidget(self.swap_bar)
        right.addStretch(1)
        top.addLayout(right, 1)
        self.body.addLayout(top)
        self.plot = HistoryPlot([("ram", theme.MEMORY)])
        self.body.addWidget(self.plot)

    def update_from(self, snap: Snapshot) -> None:
        s = snap.system
        self.gauge.set_value(
            s.ram_percent, color=theme.level_color(s.ram_percent, 85, 95, theme.MEMORY)
        )
        self.used.set(fmt.size(s.ram_used))
        self.available.set(fmt.size(s.ram_available))
        if s.swap_total:
            self.swap_text.setText(f"{fmt.size(s.swap_used)} of {fmt.size(s.swap_total)}")
            self.swap_bar.set_value(s.swap_percent)
        else:
            self.swap_text.setText("none")
            self.swap_bar.set_value(0)
        self.plot.push({"ram": s.ram_percent})


class CoresCard(Card):
    def __init__(self, host: HostInfo):
        super().__init__(
            "CPU threads",
            f"{_plural(host.cpu.logical_cpus, 'logical CPU')}, one bar each · "
            "hover a bar for its number",
        )
        self.grid = CoreGrid(host.cpu.logical_cpus)
        self.body.addWidget(self.grid)

    def update_from(self, snap: Snapshot) -> None:
        self.grid.set_values(snap.system.per_cpu)


class GpuCard(Card):
    def __init__(self, gpu: GpuInfo):
        super().__init__(f"GPU {gpu.index}", gpu.name)
        legend = QHBoxLayout()
        legend.setSpacing(12)
        legend.addWidget(LegendDot(theme.GPU, "load"))
        legend.addWidget(LegendDot(theme.VRAM, "memory"))
        self.header.addLayout(legend)

        top = QHBoxLayout()
        top.setSpacing(22)
        self.gauge = RingGauge(theme.GPU, "load")
        top.addWidget(self.gauge)

        right = QVBoxLayout()
        right.setSpacing(8)
        self.vram_text = label("", "Faint")
        self.vram_bar = MeterBar(theme.VRAM)
        self.power_text = label("", "Faint")
        self.power_bar = MeterBar(theme.POWER)
        for caption, text, bar in (
            ("MEMORY", self.vram_text, self.vram_bar),
            ("POWER", self.power_text, self.power_bar),
        ):
            row = QHBoxLayout()
            row.addWidget(label(caption, "StatLabel"))
            row.addStretch(1)
            row.addWidget(text)
            right.addLayout(row)
            right.addWidget(bar)
        stats = QHBoxLayout()
        stats.setSpacing(26)
        self.temp = Stat("Temperature")
        self.fan = Stat("Fan")
        self.procs = Stat("Processes")
        stats.addWidget(self.temp)
        stats.addWidget(self.fan)
        stats.addWidget(self.procs)
        stats.addStretch(1)
        right.addSpacing(4)
        right.addLayout(stats)
        top.addLayout(right, 1)
        self.body.addLayout(top)

        self.plot = HistoryPlot([("vram", theme.VRAM), ("util", theme.GPU)])
        self.body.addWidget(self.plot)

    def update_from(self, sample: GpuSample) -> None:
        self.gauge.set_value(sample.util_percent)
        mem_pct = sample.mem_percent
        if sample.mem_used is not None:
            self.vram_text.setText(f"{fmt.size(sample.mem_used)} of {fmt.size(sample.mem_total)}")
        self.vram_bar.set_value(mem_pct, theme.level_color(mem_pct, 85, 95, theme.VRAM))
        if sample.power_w is not None:
            limit = f" of {sample.power_limit_w:.0f} W" if sample.power_limit_w else ""
            self.power_text.setText(f"{sample.power_w:.0f} W{limit}")
            if sample.power_limit_w:
                self.power_bar.set_value(100 * sample.power_w / sample.power_limit_w)
        else:
            self.power_text.setText("–")
        t = sample.temperature_c
        self.temp.set("–" if t is None else f"{t:.0f} °C", theme.temperature_color(t))
        self.fan.set("–" if sample.fan_percent is None else f"{sample.fan_percent:.0f}%")
        self.procs.set("–" if sample.process_count is None else str(sample.process_count))
        self.plot.push({"util": sample.util_percent, "vram": mem_pct})


class NoGpuCard(Card):
    def __init__(self, info: GpuSystemInfo):
        super().__init__("GPU", "No NVIDIA GPU found")
        text = info.error or "No NVIDIA GPU was detected on this machine."
        self.body.addWidget(
            label(
                text + "  CPU-only jobs work normally; GPU jobs need the NVIDIA driver.",
                "Muted",
                wrap=True,
            )
        )


class DiskRow(QWidget):
    def __init__(self, disk: DiskUsage):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(6)
        top = QHBoxLayout()
        self.mount = label(disk.mount, "StatValue")
        self.device = label(f"{disk.device} · {disk.fstype}", "Faint")
        top.addWidget(self.mount)
        top.addSpacing(8)
        top.addWidget(self.device)
        top.addStretch(1)
        self.io = label("", "Faint")
        top.addWidget(self.io)
        top.addSpacing(18)
        self.space = label("", "Muted")
        top.addWidget(self.space)
        lay.addLayout(top)
        self.bar = MeterBar(theme.DISK)
        lay.addWidget(self.bar)

    def update_from(self, disk: DiskUsage) -> None:
        if not disk.responding:
            self.space.setText("not responding")
            self.space.setStyleSheet(f"color: {theme.WARN};")
            self.io.setText("")
            return
        self.space.setStyleSheet("")
        self.space.setText(
            f"{fmt.size(disk.free)} free of {fmt.size(disk.total)} · {disk.percent:.0f}% used"
        )
        if disk.read_bps is None:
            self.io.setText("")
        else:
            self.io.setText(f"read {fmt.rate(disk.read_bps)}   write {fmt.rate(disk.write_bps)}")
        self.bar.set_value(disk.percent, theme.level_color(disk.percent, 85, 95, theme.DISK))


class DisksCard(Card):
    def __init__(self):
        super().__init__("Disks", "Free space and read/write speed per mounted disk")
        self._rows: dict[str, DiskRow] = {}
        self._list = QVBoxLayout()
        self._list.setSpacing(14)
        self.body.addLayout(self._list)
        self._empty = label("No disks found.", "Muted")
        self._list.addWidget(self._empty)

    def update_from(self, snap: Snapshot) -> None:
        disks = snap.system.disks
        mounts = [d.mount for d in disks]
        if list(self._rows) != mounts:  # a disk was mounted or unmounted
            for row in self._rows.values():
                row.deleteLater()
            self._rows = {}
            for d in disks:
                row = DiskRow(d)
                self._rows[d.mount] = row
                self._list.addWidget(row)
        self._empty.setVisible(not disks)
        for d in disks:
            self._rows[d.mount].update_from(d)


def _section(title: str) -> QWidget:
    w = label(title.upper(), "StatLabel")
    w.setContentsMargins(4, 6, 0, 0)
    return w


class DashboardPage(QScrollArea):
    def __init__(self, host: HostInfo, gpus: GpuSystemInfo):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        page = QWidget()
        page.setObjectName("Page")
        self.setWidget(page)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 28)
        lay.setSpacing(14)

        title_row = QHBoxLayout()
        title_row.addWidget(label("Dashboard", "PageTitle"))
        title_row.addStretch(1)
        self._updated = label("waiting for first sample…", "Faint")
        title_row.addWidget(self._updated, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(title_row)

        self.host_card = HostCard(host, gpus)
        lay.addWidget(self.host_card)

        lay.addWidget(_section("Processor & memory"))
        top = QHBoxLayout()
        top.setSpacing(14)
        self.cpu_card = CpuCard(host)
        self.mem_card = MemoryCard(host)
        top.addWidget(self.cpu_card, 1)
        top.addWidget(self.mem_card, 1)
        lay.addLayout(top)
        self.cores_card = CoresCard(host)
        lay.addWidget(self.cores_card)

        lay.addWidget(_section("Graphics"))
        self.gpu_cards: dict[int, GpuCard] = {}
        gpu_grid = QGridLayout()
        gpu_grid.setSpacing(14)
        if gpus.gpus:
            driver = []
            if gpus.driver_version:
                driver.append(f"driver {gpus.driver_version}")
            if gpus.cuda_version:
                driver.append(f"CUDA {gpus.cuda_version}")
            for i, info in enumerate(gpus.gpus):
                card = GpuCard(info)
                if driver:
                    card.set_subtitle(f"{info.name}  ·  {'  ·  '.join(driver)}")
                self.gpu_cards[info.index] = card
                if len(gpus.gpus) == 1:
                    gpu_grid.addWidget(card, 0, 0, 1, 2)  # a lone GPU gets the full width
                else:
                    gpu_grid.addWidget(card, i // 2, i % 2)
            gpu_grid.setColumnStretch(0, 1)
            gpu_grid.setColumnStretch(1, 1)
        else:
            gpu_grid.addWidget(NoGpuCard(gpus), 0, 0)
        lay.addLayout(gpu_grid)

        lay.addWidget(_section("Storage"))
        self.disks_card = DisksCard()
        lay.addWidget(self.disks_card)
        lay.addStretch(1)

    def update_from(self, snap: Snapshot) -> None:
        self.cpu_card.update_from(snap)
        self.mem_card.update_from(snap)
        self.cores_card.update_from(snap)
        for sample in snap.gpus:
            card = self.gpu_cards.get(sample.index)
            if card is not None:
                card.update_from(sample)
        self.disks_card.update_from(snap)
        self.host_card.refresh()
        self._updated.setText("live · updated every second")
