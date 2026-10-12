"""Host hardware: a static description, and live samples of CPU, memory and disks."""

from __future__ import annotations

import os
import platform
import socket
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

import psutil

# ---------------------------------------------------------------------------
# Static description
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CpuInfo:
    model: str
    sockets: int
    physical_cores: int
    logical_cpus: int
    max_mhz: float | None


@dataclass(frozen=True)
class HostInfo:
    hostname: str
    os_name: str
    kernel: str
    cpu: CpuInfo
    ram_total: int
    swap_total: int
    boot_time: float


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _cpu_model_and_sockets() -> tuple[str, int]:
    model = None
    sockets: set[str] = set()
    for line in (_read_text("/proc/cpuinfo") or "").splitlines():
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "model name" and model is None:
            model = " ".join(value.split())
        elif key == "physical id":
            sockets.add(value.strip())
    return model or platform.processor() or "Unknown CPU", max(1, len(sockets))


def _max_mhz() -> float | None:
    try:
        freq = psutil.cpu_freq()
    except (OSError, NotImplementedError):
        freq = None
    if freq is not None and freq.max and freq.max > 0:
        return float(freq.max)
    text = _read_text("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    if text and text.strip().isdigit():
        return int(text.strip()) / 1000.0
    return None


def _os_name() -> str:
    for line in (_read_text("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return platform.system() or "Linux"


def host_info() -> HostInfo:
    model, sockets = _cpu_model_and_sockets()
    logical = psutil.cpu_count(logical=True) or 1
    physical = psutil.cpu_count(logical=False) or logical
    return HostInfo(
        hostname=socket.gethostname(),
        os_name=_os_name(),
        kernel=platform.release(),
        cpu=CpuInfo(
            model=model,
            sockets=sockets,
            physical_cores=physical,
            logical_cpus=logical,
            max_mhz=_max_mhz(),
        ),
        ram_total=psutil.virtual_memory().total,
        swap_total=psutil.swap_memory().total,
        boot_time=psutil.boot_time(),
    )


# ---------------------------------------------------------------------------
# Which mounts count as "disks"
# ---------------------------------------------------------------------------

# Virtual and read-only image filesystems that hold no user data.
_VIRTUAL_FS = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fusectl",
        "hugetlbfs",
        "iso9660",
        "mqueue",
        "nsfs",
        "overlay",
        "proc",
        "pstore",
        "ramfs",
        "rpc_pipefs",
        "securityfs",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
        "udf",
        "fuse.portal",
        "fuse.gvfsd-fuse",
    }
)
_SKIP_MOUNT_PREFIXES = (
    "/boot",
    "/snap",
    "/var/snap",
    "/var/lib/docker",
    "/run",
    "/proc",
    "/sys",
    "/dev",
)


class Partition(NamedTuple):
    device: str
    mountpoint: str
    fstype: str


def select_disks(partitions: Iterable[Partition]) -> list[Partition]:
    """Keep the mounts a user stores data on; drop virtual and duplicate ones.

    Network filesystems (NFS, CIFS, sshfs) are kept: simulation data often
    lives on them.  The root mount is always kept, even when it is an overlay
    (as inside a container).
    """
    chosen: list[Partition] = []
    seen_devices: set[str] = set()
    for part in partitions:
        mount = part.mountpoint
        if mount != "/":
            if part.fstype.lower() in _VIRTUAL_FS:
                continue
            if any(mount == p or mount.startswith(p + "/") for p in _SKIP_MOUNT_PREFIXES):
                continue
        # Bind mounts repeat a device; the first (shortest) mount wins.
        if part.device in seen_devices:
            continue
        seen_devices.add(part.device)
        chosen.append(part)
    chosen.sort(key=lambda p: (p.mountpoint != "/", p.mountpoint))
    return chosen


def _io_key(device: str) -> str:
    """Name of a device in ``/proc/diskstats`` (e.g. ``/dev/mapper/vg-home`` → ``dm-1``)."""
    if device.startswith("/dev/"):
        device = os.path.realpath(device)
    return os.path.basename(device)


# ---------------------------------------------------------------------------
# Live samples
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiskUsage:
    mount: str
    device: str
    fstype: str
    total: int
    used: int
    free: int
    percent: float
    read_bps: float | None
    write_bps: float | None
    responding: bool = True


@dataclass(frozen=True)
class SystemSnapshot:
    timestamp: float
    cpu_percent: float
    per_cpu: tuple[float, ...]
    freq_mhz: float | None
    load_avg: tuple[float, float, float]
    cpu_temp_c: float | None
    ram_total: int
    ram_used: int
    ram_available: int
    ram_percent: float
    swap_total: int
    swap_used: int
    swap_percent: float
    disks: tuple[DiskUsage, ...]


def _cpu_temperature() -> float | None:
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    for name in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
        values = [t.current for t in sensors.get(name, ()) if t.current and t.current > 0]
        if values:
            return max(values)
    return None


_NETWORK_FS_PREFIXES = (
    "nfs",
    "cifs",
    "smb",
    "fuse",
    "9p",
    "ceph",
    "lustre",
    "gpfs",
    "glusterfs",
    "beegfs",
    "afs",
    "davfs",
)


def is_network_fs(fstype: str) -> bool:
    return fstype.lower().startswith(_NETWORK_FS_PREFIXES)


class _Call:
    """Run ``fn(*args)`` in a daemon thread; a hung call never blocks exit."""

    def __init__(self, fn, *args) -> None:
        self.done = threading.Event()
        self.result = None
        threading.Thread(
            target=self._run, args=(fn, args), daemon=True, name="simq-statvfs"
        ).start()

    def _run(self, fn, args) -> None:
        try:
            self.result = fn(*args)
        except OSError:
            self.result = None
        finally:
            self.done.set()


class _DiskUsageProbe:
    """``statvfs`` that cannot hang the sampler.

    A dead network mount blocks ``statvfs`` forever.  Network mounts are
    therefore probed in a background thread with a timeout; one that does not
    answer is reported as not responding.  Local disks answer instantly and
    are queried directly.
    """

    def __init__(self, timeout: float = 0.5) -> None:
        self._timeout = timeout
        self._pending: dict[str, _Call] = {}

    def usage(self, mount: str, fstype: str):
        if not is_network_fs(fstype):
            try:
                return psutil.disk_usage(mount)
            except OSError:
                return None
        call = self._pending.get(mount)
        if call is None:
            call = self._pending[mount] = _Call(psutil.disk_usage, mount)
        if not call.done.wait(self._timeout):
            return None  # still waiting; the same call is checked next time
        del self._pending[mount]
        return call.result


class SystemSampler:
    """Takes a :class:`SystemSnapshot` each time :meth:`sample` is called.

    Rates (disk read/write) are averaged over the time since the previous
    call, so call it at a steady interval (the dashboard uses one second).
    """

    PARTITION_REFRESH_S = 10.0

    def __init__(self) -> None:
        psutil.cpu_percent(percpu=True)  # the first call only primes the counters
        self._probe = _DiskUsageProbe()
        self._partitions: list[Partition] = []
        self._partitions_at = float("-inf")
        self._last_usage: dict[str, DiskUsage] = {}
        self._prev_io: dict[str, tuple[int, int]] = {}
        self._prev_time: float | None = None

    def _current_partitions(self, now: float) -> list[Partition]:
        if now - self._partitions_at >= self.PARTITION_REFRESH_S:
            try:
                raw = psutil.disk_partitions(all=True)
            except OSError:
                raw = []
            self._partitions = select_disks(
                Partition(p.device, p.mountpoint, p.fstype) for p in raw
            )
            self._partitions_at = now
        return self._partitions

    def _disks(self, now: float) -> tuple[DiskUsage, ...]:
        try:
            io = psutil.disk_io_counters(perdisk=True, nowrap=True) or {}
        except (OSError, RuntimeError):
            io = {}
        dt = None if self._prev_time is None else now - self._prev_time

        disks = []
        current_io: dict[str, tuple[int, int]] = {}
        for part in self._current_partitions(now):
            read_bps = write_bps = None
            counters = io.get(_io_key(part.device))
            if counters is not None:
                current_io[part.mountpoint] = (counters.read_bytes, counters.write_bytes)
                prev = self._prev_io.get(part.mountpoint)
                if prev is not None and dt and dt > 0:
                    read_bps = max(0.0, (counters.read_bytes - prev[0]) / dt)
                    write_bps = max(0.0, (counters.write_bytes - prev[1]) / dt)

            usage = self._probe.usage(part.mountpoint, part.fstype)
            if usage is None:
                last = self._last_usage.get(part.mountpoint)
                disks.append(
                    DiskUsage(
                        mount=part.mountpoint,
                        device=part.device,
                        fstype=part.fstype,
                        total=last.total if last else 0,
                        used=last.used if last else 0,
                        free=last.free if last else 0,
                        percent=last.percent if last else 0.0,
                        read_bps=None,
                        write_bps=None,
                        responding=False,
                    )
                )
                continue
            disk = DiskUsage(
                mount=part.mountpoint,
                device=part.device,
                fstype=part.fstype,
                total=usage.total,
                used=usage.used,
                free=usage.free,
                percent=float(usage.percent),
                read_bps=read_bps,
                write_bps=write_bps,
            )
            self._last_usage[part.mountpoint] = disk
            disks.append(disk)

        self._prev_io = current_io
        return tuple(disks)

    def sample(self) -> SystemSnapshot:
        now = time.monotonic()
        per_cpu = tuple(float(v) for v in psutil.cpu_percent(percpu=True))
        try:
            freq = psutil.cpu_freq()
        except (OSError, NotImplementedError):
            freq = None
        try:
            load = tuple(float(v) for v in os.getloadavg())
        except OSError:
            load = (0.0, 0.0, 0.0)
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        disks = self._disks(now)
        self._prev_time = now
        return SystemSnapshot(
            timestamp=time.time(),
            cpu_percent=sum(per_cpu) / len(per_cpu) if per_cpu else 0.0,
            per_cpu=per_cpu,
            freq_mhz=float(freq.current) if freq is not None and freq.current else None,
            load_avg=load,  # type: ignore[arg-type]
            cpu_temp_c=_cpu_temperature(),
            ram_total=vm.total,
            # "Used" the way htop and free(1) mean it: not reclaimable cache.
            ram_used=vm.total - vm.available,
            ram_available=vm.available,
            ram_percent=float(vm.percent),
            swap_total=sw.total,
            swap_used=sw.used,
            swap_percent=float(sw.percent),
            disks=disks,
        )

    def close(self) -> None:
        """Kept for symmetry with other monitors; there is nothing to release."""
