"""Shared helpers for the queue tests: fake programs, a small UDB package, a service."""

from __future__ import annotations

import re
import shutil
import sys
import time
from pathlib import Path

from simq.db import Database
from simq.detect.software import DetectionReport
from simq.engine.service import Service
from simq.monitor.gpu import GpuInfo, GpuMonitor, GpuSystemInfo

ROOT = Path(__file__).resolve().parent
FAKES = ROOT / "fakes"
UDB_DATA = ROOT / "data" / "udb_g4"

# Small stage lengths so the real UDB scripts finish in seconds with the fake NAMD.
SMALL_STEPS = {
    "min_steps": 100,
    "heat_steps": 400,
    "nvt_steps": 400,
    "npt_free": 400,
    "prod_steps": 800,
    "energy_freq": 100,
    "restart_freq": 200,
    "traj_freq": 100,
}


def install_fakes(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("namd3", "tleap"):
        exe = bin_dir / name
        exe.write_text(
            f"#!{sys.executable}\n"
            "import runpy, sys\n"
            f"sys.argv[0] = {name!r}\n"
            f"runpy.run_path({str(FAKES / (name + '.py'))!r}, run_name='__main__')\n"
        )
        exe.chmod(0o755)
    return bin_dir


def make_udb_package(dest: Path, namd: Path, steps: dict[str, int] | None = None) -> Path:
    """A copy of the PAMAM G4 package with small stage lengths and our fake NAMD."""
    shutil.copytree(UDB_DATA, dest)
    prefix = "dendrimer_pamam_gen4"
    (dest / f"{prefix}_min_gaff.mol2").write_text("@<TRIPOS>MOLECULE\nfake\n")
    (dest / f"{prefix}.frcmod").write_text("fake frcmod\n")
    conf = dest / "udb_md.conf"
    text = conf.read_text()
    for key, value in (steps or SMALL_STEPS).items():
        text = re.sub(rf"^(set\s+{key}\s+)\S+", rf"\g<1>{value}", text, flags=re.MULTILINE)
    conf.write_text(text)
    for script in ("run_namd.sh", "run_full.sh"):
        path = dest / script
        body = path.read_text()
        body = re.sub(r'^NAMD_BIN=".*"$', f'NAMD_BIN="{namd}"', body, flags=re.MULTILINE)
        path.write_text(body)
    return dest


class _NoGpu:
    def system_info(self) -> GpuSystemInfo:
        return GpuSystemInfo("none", None, None, ())

    def sample(self):
        return []

    def close(self) -> None:
        pass


class _OneGpu(_NoGpu):
    def system_info(self) -> GpuSystemInfo:
        return GpuSystemInfo("demo", "1", "12.4", (GpuInfo(0, "Test GPU", None, 8 << 30, None),))


def make_service(gpus: int = 0) -> Service:
    gpu = GpuMonitor(backend=_OneGpu() if gpus else _NoGpu())
    service = Service(db=Database(), gpu=gpu, report=DetectionReport())
    service.db.set_setting("min_free_gb", 0.1)
    service.db.set_setting("critical_free_gb", 0.0)
    return service


def run_until(service: Service, condition, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        service.tick(time.time())
        if condition():
            return
        time.sleep(0.1)
    events = "\n".join(f"  [{e['level']}] {e['text']}" for e in service.db.events(limit=40))
    raise AssertionError(f"timed out; recent events:\n{events}")
