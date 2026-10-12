"""Start a job's command, signal it, and find out how it ended.

Every run gets a folder ``~/.simq/runs/<run id>/`` holding:

* ``command.sh`` – the job's command, exactly as the user wrote it
* ``run.sh``     – a small wrapper SimQ writes: it enters the job's folder,
  sets up the environment (CPU pinning, GPU choice, AMBER), runs
  ``command.sh`` and records the exit code in ``exit``
* ``output.log`` – everything the command printed

Recording the exit code in a file means SimQ learns how a job ended even if
the service was restarted while the job ran.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from ..paths import data_dir
from .scheduler import cpu_list_text


def runs_dir() -> Path:
    path = data_dir() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def fill_placeholders(command: str, cores: int, gpu: int | None) -> str:
    """Replace ``{cores}`` and ``{gpu}``.

    ``{gpu}`` is always ``0`` when a GPU is assigned: the job only sees its own
    GPU (``CUDA_VISIBLE_DEVICES``), and to the job that GPU is device 0.
    """
    return command.replace("{cores}", str(cores)).replace("{gpu}", "0" if gpu is not None else "")


@dataclass
class LaunchSpec:
    job_id: int
    run_id: int
    work_dir: Path
    command: str  # placeholders already filled in
    cores: int
    cpus: list[int] | None  # None: do not pin
    gpu: int | None
    path_dirs: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def write_scripts(spec: LaunchSpec) -> Path:
    run_dir = runs_dir() / str(spec.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    q = shlex.quote
    (run_dir / "command.sh").write_text(f"#!/bin/bash\n{spec.command}\n")
    exit_file = run_dir / "exit"
    lines = [
        "#!/bin/bash",
        f"# Written by SimQ for job {spec.job_id} (run {spec.run_id}).",
        "# It runs the job's command and records how it ended.",
        f'cd -- {q(str(spec.work_dir))} || {{ echo "SimQ: cannot enter the job folder" >&2; '
        f"echo 96 > {q(str(exit_file))}; exit 96; }}",
    ]
    for script in spec.source_files:
        lines.append(f"source {q(script)} >/dev/null 2>&1 || true")
    if spec.path_dirs:
        joined = ":".join(spec.path_dirs)
        lines.append(f'export PATH={q(joined)}:"$PATH"')
    env = {
        "SIMQ_JOB_ID": str(spec.job_id),
        "SIMQ_RUN_ID": str(spec.run_id),
        "SIMQ_CORES": str(spec.cores),
        "OMP_NUM_THREADS": str(spec.cores),
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
    }
    if spec.gpu is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(spec.gpu)
        env["SIMQ_GPU"] = str(spec.gpu)
    env.update(spec.env)
    for key, value in env.items():
        lines.append(f"export {key}={q(value)}")
    runner = f"/bin/bash {q(str(run_dir / 'command.sh'))}"
    taskset = shutil.which("taskset")
    if spec.cpus and taskset:
        runner = f"{q(taskset)} -c {cpu_list_text(spec.cpus)} {runner}"
    lines += [
        runner,
        "rc=$?",
        f"printf '%s\\n' \"$rc\" > {q(str(exit_file))}.tmp && mv -f {q(str(exit_file))}.tmp "
        f"{q(str(exit_file))}",
        'exit "$rc"',
    ]
    wrapper = run_dir / "run.sh"
    wrapper.write_text("\n".join(lines) + "\n")
    return wrapper


def start(spec: LaunchSpec) -> tuple[subprocess.Popen, float, Path]:
    """Start the run in its own process group; return (process, start time, run dir)."""
    wrapper = write_scripts(spec)
    run_dir = wrapper.parent
    for stale in ("exit", "exit.tmp"):
        (run_dir / stale).unlink(missing_ok=True)
    with open(run_dir / "output.log", "ab") as log:
        proc = subprocess.Popen(
            ["/bin/bash", str(wrapper)],
            cwd=run_dir,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    try:
        created = psutil.Process(proc.pid).create_time()
    except psutil.Error:
        created = time.time()
    return proc, created, run_dir


def alive(pid: int | None, created: float | None) -> bool:
    """Is this exact process still running? (Process ids get reused.)"""
    if not pid:
        return False
    try:
        proc = psutil.Process(pid)
        if created is not None and abs(proc.create_time() - created) > 1.0:
            return False
        return proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def signal_group(pid: int | None, sig: int) -> bool:
    if not pid:
        return False
    try:
        os.killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop(pid: int | None) -> None:
    """Ask the whole job to stop (a paused job is woken so it can)."""
    signal_group(pid, signal.SIGTERM)
    signal_group(pid, signal.SIGCONT)


def force_stop(pid: int | None) -> None:
    signal_group(pid, signal.SIGKILL)


def pause(pid: int | None) -> bool:
    return signal_group(pid, signal.SIGSTOP)


def resume(pid: int | None) -> bool:
    return signal_group(pid, signal.SIGCONT)


def exit_code(run_dir: str | Path | None) -> int | None:
    if not run_dir:
        return None
    try:
        return int((Path(run_dir) / "exit").read_text().strip())
    except (OSError, ValueError):
        return None


def tail(path: str | Path, lines: int = 60) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 64_000))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
