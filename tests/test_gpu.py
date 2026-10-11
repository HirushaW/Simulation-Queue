import types

import pytest

from simq.monitor.gpu import (
    DemoBackend,
    GpuMonitor,
    NvmlBackend,
    SmiBackend,
    cuda_version_text,
    parse_smi_csv,
)

SMI_ROW = (
    "0, NVIDIA GeForce RTX 4090, GPU-abc, 00000000:01:00.0, 550.54.15, 24564, 1024, "
    "37, 52, 61.20, 450.00, 30"
)
SMI_ROW_NA = (
    "1, NVIDIA RTX A5000, GPU-def, 00000000:41:00.0, 550.54.15, 24564, 0, "
    "[N/A], 40, [N/A], [Not Supported], [N/A]"
)


def test_cuda_version_text():
    assert cuda_version_text(12040) == "12.4"
    assert cuda_version_text(11080) == "11.8"


def test_parse_smi_csv_skips_bad_rows():
    rows = parse_smi_csv(SMI_ROW + "\ngarbage line\n" + SMI_ROW_NA)
    assert [r["index"] for r in rows] == ["0", "1"]


def test_smi_backend():
    def run(cmd):
        if len(cmd) == 1:
            return "| NVIDIA-SMI 550.54.15   Driver Version: 550.54.15   CUDA Version: 12.4 |"
        return SMI_ROW + "\n" + SMI_ROW_NA + "\n"

    mon = GpuMonitor(SmiBackend("nvidia-smi", run=run))
    info = mon.info
    assert info.backend == "nvidia-smi"
    assert info.driver_version == "550.54.15" and info.cuda_version == "12.4"
    assert [g.index for g in info.gpus] == [0, 1]
    assert info.gpus[0].memory_total == 24564 * 1024 * 1024

    s0, s1 = mon.sample()
    assert s0.util_percent == 37 and s0.power_w == pytest.approx(61.2)
    assert s0.mem_percent == pytest.approx(100 * 1024 / 24564)
    assert s1.util_percent is None and s1.power_w is None and s1.power_limit_w is None
    assert s1.temperature_c == 40


class FakeNvmlError(Exception):
    pass


def fake_nvml(count=2, broken_fan=True):
    def mem(_h):
        return types.SimpleNamespace(total=24 * 1024**3, used=6 * 1024**3)

    def fan(_h):
        if broken_fan:
            raise FakeNvmlError("not supported")
        return 40

    return types.SimpleNamespace(
        NVMLError=FakeNvmlError,
        NVML_TEMPERATURE_GPU=0,
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlDeviceGetCount=lambda: count,
        nvmlDeviceGetHandleByIndex=lambda i: i,
        nvmlSystemGetDriverVersion=lambda: "550.54.15",
        nvmlSystemGetCudaDriverVersion=lambda: 12040,
        nvmlDeviceGetName=lambda h: f"Fake GPU {h}".encode(),
        nvmlDeviceGetUUID=lambda h: f"GPU-{h}",
        nvmlDeviceGetPciInfo=lambda h: types.SimpleNamespace(busId=f"0000:0{h}:00.0"),
        nvmlDeviceGetMemoryInfo=mem,
        nvmlDeviceGetUtilizationRates=lambda h: types.SimpleNamespace(gpu=50 + h),
        nvmlDeviceGetTemperature=lambda h, _kind: 60,
        nvmlDeviceGetPowerUsage=lambda h: 250_000,
        nvmlDeviceGetEnforcedPowerLimit=lambda h: 450_000,
        nvmlDeviceGetFanSpeed=fan,
        nvmlDeviceGetComputeRunningProcesses=lambda h: [object()] * h,
    )


def test_nvml_backend():
    mon = GpuMonitor(NvmlBackend(fake_nvml()))
    assert mon.available
    assert mon.info.cuda_version == "12.4"
    assert [g.name for g in mon.info.gpus] == ["Fake GPU 0", "Fake GPU 1"]
    s = mon.sample()
    assert [x.util_percent for x in s] == [50.0, 51.0]
    assert s[0].power_w == 250.0 and s[0].power_limit_w == 450.0
    assert s[0].fan_percent is None  # unsupported reading → None, not a crash
    assert [x.process_count for x in s] == [0, 1]
    mon.close()


def test_monitor_survives_failing_backend():
    class Broken:
        def system_info(self):
            raise RuntimeError("driver/library version mismatch")

        def sample(self):
            raise AssertionError("never called")

        def close(self):
            pass

    mon = GpuMonitor(Broken())
    assert not mon.available
    assert "version mismatch" in mon.info.error
    assert mon.sample() == []


def test_demo_backend_values_in_range():
    mon = GpuMonitor(DemoBackend())
    assert len(mon.info.gpus) == 2
    for s in mon.sample():
        assert 0 <= s.util_percent <= 100
        assert 0 <= s.mem_percent <= 100
