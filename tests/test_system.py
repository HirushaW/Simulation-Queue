import time

from simq.monitor import system
from simq.monitor.system import Partition, SystemSampler, host_info, select_disks


def test_host_info_is_sane():
    info = host_info()
    assert info.hostname
    assert info.cpu.logical_cpus >= info.cpu.physical_cores >= 1
    assert info.ram_total > 0


def test_sampler_values_in_range():
    sampler = SystemSampler()
    time.sleep(0.2)
    sampler.sample()
    snap = sampler.sample()
    assert len(snap.per_cpu) == host_info().cpu.logical_cpus
    assert all(0 <= v <= 100 for v in snap.per_cpu)
    assert 0 <= snap.cpu_percent <= 100
    assert 0 < snap.ram_used <= snap.ram_total
    assert any(d.mount == "/" for d in snap.disks)
    for d in snap.disks:
        assert d.responding and d.total > 0
    sampler.close()


def test_select_disks_keeps_real_and_network_drops_virtual():
    parts = [
        Partition("proc", "/proc", "proc"),
        Partition("/dev/nvme0n1p2", "/", "ext4"),
        Partition("/dev/nvme0n1p1", "/boot/efi", "vfat"),
        Partition("tmpfs", "/dev/shm", "tmpfs"),
        Partition("/dev/loop3", "/snap/core/123", "squashfs"),
        Partition("/dev/sda1", "/data", "xfs"),
        Partition("/dev/sda1", "/home/u/data-bind", "xfs"),  # bind mount of /data
        Partition("nas:/export/sims", "/mnt/sims", "nfs4"),
        Partition("overlay", "/var/lib/docker/overlay2/x/merged", "overlay"),
    ]
    assert [p.mountpoint for p in select_disks(parts)] == ["/", "/data", "/mnt/sims"]


def test_overlay_root_is_kept():
    parts = [Partition("overlay", "/", "overlay")]
    assert [p.mountpoint for p in select_disks(parts)] == ["/"]


def test_hung_network_mount_does_not_block(monkeypatch):
    import threading

    release = threading.Event()
    real_usage = system.psutil.disk_usage

    def fake_usage(path):
        if path == "/mnt/dead":
            release.wait(5)
        return real_usage("/")

    monkeypatch.setattr(system.psutil, "disk_usage", fake_usage)
    probe = system._DiskUsageProbe(timeout=0.05)
    started = time.monotonic()
    assert probe.usage("/mnt/dead", "nfs4") is None
    assert probe.usage("/", "ext4") is not None
    assert time.monotonic() - started < 1.0
    release.set()
    time.sleep(0.1)
    assert probe.usage("/mnt/dead", "nfs4") is not None  # answers once the mount recovers
