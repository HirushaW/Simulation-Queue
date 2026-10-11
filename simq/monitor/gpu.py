"""NVIDIA GPUs: what is installed, and live load, memory, temperature and power.

Three backends, tried in order:

* **NVML** (the library behind ``nvidia-smi``), through the ``nvidia-ml-py``
  package.  Fast, and the normal case.
* **nvidia-smi**, parsed from its CSV output, if NVML cannot be loaded.
* **none**, when the machine has no NVIDIA driver; the dashboard then says so.

GPU numbers follow the PCI bus order, which is also what ``nvidia-smi``
shows.  CUDA numbers GPUs fastest-first unless ``CUDA_DEVICE_ORDER=PCI_BUS_ID``
is set, so the queue sets that variable for every job it launches; "GPU 1"
then means the same card everywhere.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class GpuInfo:
    index: int
    name: str
    uuid: str | None
    memory_total: int
    pci_bus_id: str | None


@dataclass(frozen=True)
class GpuSample:
    index: int
    util_percent: float | None
    mem_used: int | None
    mem_total: int
    temperature_c: float | None
    power_w: float | None
    power_limit_w: float | None
    fan_percent: float | None
    process_count: int | None

    @property
    def mem_percent(self) -> float | None:
        if self.mem_used is None or not self.mem_total:
            return None
        return 100.0 * self.mem_used / self.mem_total


@dataclass(frozen=True)
class GpuSystemInfo:
    backend: str  # "nvml", "nvidia-smi", "demo" or "none"
    driver_version: str | None
    cuda_version: str | None
    gpus: tuple[GpuInfo, ...]
    error: str | None = None


class _Backend(Protocol):
    def system_info(self) -> GpuSystemInfo: ...
    def sample(self) -> list[GpuSample]: ...
    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# NVML
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def cuda_version_text(code: int) -> str:
    """NVML encodes CUDA 12.4 as 12040."""
    return f"{code // 1000}.{(code % 1000) // 10}"


class NvmlBackend:
    def __init__(self, nvml: Any) -> None:
        self._nvml = nvml
        nvml.nvmlInit()
        count = nvml.nvmlDeviceGetCount()
        self._handles = [nvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)]

    def _try(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except self._nvml.NVMLError:
            return None

    def system_info(self) -> GpuSystemInfo:
        nvml = self._nvml
        driver = self._try(nvml.nvmlSystemGetDriverVersion)
        cuda = self._try(nvml.nvmlSystemGetCudaDriverVersion)
        gpus = []
        for i, h in enumerate(self._handles):
            mem = self._try(nvml.nvmlDeviceGetMemoryInfo, h)
            pci = self._try(nvml.nvmlDeviceGetPciInfo, h)
            uuid = self._try(nvml.nvmlDeviceGetUUID, h)
            gpus.append(
                GpuInfo(
                    index=i,
                    name=_text(self._try(nvml.nvmlDeviceGetName, h) or f"GPU {i}"),
                    uuid=_text(uuid) if uuid else None,
                    memory_total=int(mem.total) if mem else 0,
                    pci_bus_id=_text(pci.busId) if pci else None,
                )
            )
        return GpuSystemInfo(
            backend="nvml",
            driver_version=_text(driver) if driver else None,
            cuda_version=cuda_version_text(cuda) if cuda else None,
            gpus=tuple(gpus),
        )

    def sample(self) -> list[GpuSample]:
        nvml = self._nvml
        samples = []
        for i, h in enumerate(self._handles):
            util = self._try(nvml.nvmlDeviceGetUtilizationRates, h)
            mem = self._try(nvml.nvmlDeviceGetMemoryInfo, h)
            temp = self._try(nvml.nvmlDeviceGetTemperature, h, nvml.NVML_TEMPERATURE_GPU)
            power = self._try(nvml.nvmlDeviceGetPowerUsage, h)
            limit = self._try(nvml.nvmlDeviceGetEnforcedPowerLimit, h)
            fan = self._try(nvml.nvmlDeviceGetFanSpeed, h)
            procs = self._try(nvml.nvmlDeviceGetComputeRunningProcesses, h)
            samples.append(
                GpuSample(
                    index=i,
                    util_percent=float(util.gpu) if util is not None else None,
                    mem_used=int(mem.used) if mem is not None else None,
                    mem_total=int(mem.total) if mem is not None else 0,
                    temperature_c=float(temp) if temp is not None else None,
                    power_w=power / 1000.0 if power is not None else None,
                    power_limit_w=limit / 1000.0 if limit else None,
                    fan_percent=float(fan) if fan is not None else None,
                    process_count=len(procs) if procs is not None else None,
                )
            )
        return samples

    def close(self) -> None:
        self._try(self._nvml.nvmlShutdown)


# ---------------------------------------------------------------------------
# nvidia-smi
# ---------------------------------------------------------------------------

SMI_FIELDS = (
    "index",
    "name",
    "uuid",
    "pci.bus_id",
    "driver_version",
    "memory.total",
    "memory.used",
    "utilization.gpu",
    "temperature.gpu",
    "power.draw",
    "power.limit",
    "fan.speed",
)
_MIB = 1024 * 1024


def _smi_number(text: str) -> float | None:
    """nvidia-smi writes ``[N/A]`` or ``[Not Supported]`` for missing values."""
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _smi_index(row: dict[str, str], fallback: int) -> int:
    value = _smi_number(row["index"])
    return int(value) if value is not None else fallback


def parse_smi_csv(text: str) -> list[dict[str, str]]:
    rows = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(SMI_FIELDS):
            continue
        rows.append(dict(zip(SMI_FIELDS, parts, strict=True)))
    return rows


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return result.stdout


class SmiBackend:
    def __init__(self, exe: str, run: Callable[[list[str]], str] = _run) -> None:
        self._exe = exe
        self._run = run
        self._query = [exe, f"--query-gpu={','.join(SMI_FIELDS)}", "--format=csv,noheader,nounits"]
        self._rows = parse_smi_csv(self._run(self._query))

    def system_info(self) -> GpuSystemInfo:
        cuda = None
        try:
            match = re.search(r"CUDA Version:\s*([\d.]+)", self._run([self._exe]))
            cuda = match.group(1) if match else None
        except (OSError, subprocess.SubprocessError):
            pass
        gpus = tuple(
            GpuInfo(
                index=_smi_index(r, i),
                name=r["name"],
                uuid=r["uuid"] or None,
                memory_total=int((_smi_number(r["memory.total"]) or 0) * _MIB),
                pci_bus_id=r["pci.bus_id"] or None,
            )
            for i, r in enumerate(self._rows)
        )
        driver = self._rows[0]["driver_version"] if self._rows else None
        return GpuSystemInfo("nvidia-smi", driver, cuda, gpus)

    def sample(self) -> list[GpuSample]:
        samples = []
        for i, r in enumerate(parse_smi_csv(self._run(self._query))):
            used = _smi_number(r["memory.used"])
            power = _smi_number(r["power.draw"])
            samples.append(
                GpuSample(
                    index=_smi_index(r, i),
                    util_percent=_smi_number(r["utilization.gpu"]),
                    mem_used=int(used * _MIB) if used is not None else None,
                    mem_total=int((_smi_number(r["memory.total"]) or 0) * _MIB),
                    temperature_c=_smi_number(r["temperature.gpu"]),
                    power_w=power,
                    power_limit_w=_smi_number(r["power.limit"]),
                    fan_percent=_smi_number(r["fan.speed"]),
                    process_count=None,
                )
            )
        return samples

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Demo (for previewing the dashboard on a machine without a GPU)
# ---------------------------------------------------------------------------


class DemoBackend:
    _CARDS = (("NVIDIA RTX A5000 (demo)", 24), ("NVIDIA GeForce RTX 4090 (demo)", 24))

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def system_info(self) -> GpuSystemInfo:
        gpus = tuple(
            GpuInfo(i, name, None, gb * 1024**3, f"00000000:{i + 1:02X}:00.0")
            for i, (name, gb) in enumerate(self._CARDS)
        )
        return GpuSystemInfo("demo", "550.54.15", "12.4", gpus)

    def sample(self) -> list[GpuSample]:
        t = time.monotonic() - self._t0
        out = []
        for i, (_, gb) in enumerate(self._CARDS):
            phase = t / 9.0 + i * 2.1
            util = 55 + 40 * math.sin(phase) if i == 0 else 8 + 6 * math.sin(phase * 3)
            util = max(0.0, min(100.0, util))
            total = gb * 1024**3
            out.append(
                GpuSample(
                    index=i,
                    util_percent=util,
                    mem_used=int(total * (0.15 + 0.5 * util / 100)),
                    mem_total=total,
                    temperature_c=38 + 0.4 * util,
                    power_w=60 + 2.8 * util,
                    power_limit_w=350.0,
                    fan_percent=30 + 0.5 * util,
                    process_count=1 if util > 20 else 0,
                )
            )
        return out

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


class GpuMonitor:
    """Describes and samples the machine's NVIDIA GPUs.

    Never raises: if no driver is present, :attr:`info` has no GPUs and an
    ``error`` explaining why, and :meth:`sample` returns an empty list.
    """

    def __init__(self, backend: _Backend | None = None, demo: bool = False) -> None:
        error = None
        if backend is None:
            backend, error = (DemoBackend(), None) if demo else _auto_backend()
        self._backend = backend
        if backend is None:
            self.info = GpuSystemInfo("none", None, None, (), error)
            return
        try:
            self.info = backend.system_info()
        except Exception as exc:  # a broken driver must not take the app down
            self._backend = None
            self.info = GpuSystemInfo("none", None, None, (), f"GPU query failed: {exc}")

    @property
    def available(self) -> bool:
        return bool(self.info.gpus)

    def sample(self) -> list[GpuSample]:
        if self._backend is None:
            return []
        try:
            return self._backend.sample()
        except Exception:
            return []

    def close(self) -> None:
        if self._backend is not None:
            self._backend.close()
            self._backend = None


def _auto_backend() -> tuple[_Backend | None, str | None]:
    reasons = []
    try:
        import pynvml  # provided by the nvidia-ml-py package

        return NvmlBackend(pynvml), None
    except ImportError:
        reasons.append("the nvidia-ml-py package is not installed")
    except Exception as exc:
        reasons.append(f"NVML could not start ({exc})")

    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            return SmiBackend(exe), None
        except (OSError, subprocess.SubprocessError) as exc:
            reasons.append(f"nvidia-smi failed ({exc})")
    else:
        reasons.append("nvidia-smi was not found")
    return None, "No NVIDIA GPU detected: " + "; ".join(reasons) + "."
