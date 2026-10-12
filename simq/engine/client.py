"""What the window and the command line use to manage the queue."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..analyze.folder import default_command, default_resources, inspect_folder
from ..db import FAILED, HELD, KILLED, PAUSED, QUEUED, RUNNING, Database, Job
from ..detect.software import DetectionReport
from ..paths import data_dir
from .scheduler import usable_cpus

HEARTBEAT_STALE_S = 6.0


def service_status(db: Database) -> dict[str, Any]:
    info = db.setting("service") or {}
    pid = info.get("pid")
    age = time.time() - float(info.get("time") or 0)
    running = bool(pid) and age < HEARTBEAT_STALE_S
    return {"running": running, "pid": pid, "age": age, "version": info.get("version")}


def start_service(wait: float = 8.0, db: Database | None = None) -> bool:
    """Start the service in the background (it outlives this process)."""
    db = db or Database()
    if service_status(db)["running"]:
        return True
    log = open(data_dir() / "service.log", "ab")  # noqa: SIM115 (handed to the child)
    subprocess.Popen(
        [sys.executable, "-m", "simq", "service", "run"],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    log.close()
    deadline = time.time() + wait
    while time.time() < deadline:
        if service_status(db)["running"]:
            return True
        time.sleep(0.2)
    return False


def stop_service(db: Database, wait: float = 10.0) -> bool:
    status = service_status(db)
    if not status["running"]:
        return True
    db.request("stop")
    deadline = time.time() + wait
    while time.time() < deadline:
        if not service_status(db)["running"] or (db.setting("service") or {}).get("stopped"):
            return True
        time.sleep(0.2)
    pid = status["pid"]
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
    return False


def default_retries(db: Database, kind: str) -> int:
    key = {"udb": "retries_udb", "namd": "retries_namd"}.get(kind, "retries_other")
    return int(db.setting(key) or 0)


def queue_cpus(db: Database) -> int:
    return len(usable_cpus(int(db.setting("cpu_budget") or 0)))


def suggest(
    folder: str | Path,
    report: DetectionReport | None,
    budget: int,
    kind: str | None = None,
    choice: str | None = None,
) -> dict[str, Any]:
    """Defaults for a new job in ``folder``: kind, file, command, cores, GPU."""
    guess = inspect_folder(folder)
    kind = kind or guess.kind
    choices = guess.choices.get(kind, [])
    if choice is None or choice not in choices:
        choice = choices[0] if choices else None
    cores, gpu, reason = default_resources(kind, folder, choice, report, budget)
    command = default_command(kind, folder, choice, gpu != "none", report)
    return {"kind": kind, "choice": choice, "choices": guess.choices, "summary": guess.summary,
            "command": command, "cores": cores, "gpu": gpu, "reason": reason,
            "name": Path(folder).resolve().name}  # fmt: skip


def add_job(
    db: Database, folder: str | Path, report: DetectionReport | None = None, **values: Any
) -> int:
    """Add a job; anything not given is filled in from :func:`suggest`."""
    folder = str(Path(folder).expanduser().resolve())
    defaults = suggest(folder, report, queue_cpus(db), values.get("kind"), values.get("choice"))
    job = {
        "name": values.get("name") or defaults["name"],
        "kind": values.get("kind") or defaults["kind"],
        "folder": folder,
        "choice": values.get("choice", defaults["choice"]),
        "command": values.get("command") or defaults["command"],
        "cores": int(values.get("cores") or defaults["cores"]),
        "gpu": str(values.get("gpu") or defaults["gpu"]),
        "after_job": values.get("after_job"),
        "start_at": values.get("start_at"),
        "manual_hours": values.get("manual_hours"),
    }
    job["retries"] = (
        int(values["retries"])
        if values.get("retries") is not None
        else default_retries(db, job["kind"])
    )
    if not job["command"]:
        raise ValueError("A command is needed: nothing in this folder says how to run it.")
    job_id = db.add_job(**job)
    db.event(job_id, "info", f"Added ({job['kind']}): {job['command']}")
    return job_id


def update_job(db: Database, job_id: int, **values: Any) -> None:
    job = db.job(job_id)
    if job is None:
        raise ValueError(f"No job {job_id}.")
    if job.state in (RUNNING, PAUSED):
        raise ValueError("A running job cannot be edited; kill it first.")
    values["check_state"] = "none"  # anything may have changed: check again
    db.update_job(job_id, **values)
    db.merge_meta(job_id, check=None, speed=None)
    db.event(job_id, "info", "Edited.")


def duplicate_job(db: Database, job_id: int) -> int:
    job = db.job(job_id)
    if job is None:
        raise ValueError(f"No job {job_id}.")
    new_id = db.add_job(
        name=f"{job.name} (copy)",
        kind=job.kind,
        folder=job.folder,
        choice=job.choice,
        command=job.command,
        cores=job.cores,
        gpu=job.gpu,
        retries=job.retries,
        manual_hours=job.manual_hours,
    )
    db.event(new_id, "info", f"Copied from job #{job_id}.")
    return new_id


def remove_job(db: Database, job_id: int) -> None:
    job = db.job(job_id)
    if job is None:
        return
    if job.state in (RUNNING, PAUSED) or any(
        r.state in ("waiting", "running") for r in db.job_runs(job_id)
    ):
        raise ValueError("This job (or its test) is running; kill it first.")
    db.delete_job(job_id)


def move_job(db: Database, job_id: int, direction: int) -> None:
    """Swap the job with its neighbour above (-1) or below (+1)."""
    jobs = db.jobs()
    index = next((i for i, j in enumerate(jobs) if j.id == job_id), None)
    if index is None:
        return
    other = index + direction
    if not 0 <= other < len(jobs):
        return
    a, b = jobs[index], jobs[other]
    with db.transaction():
        db.update_job(a.id, position=b.position)
        db.update_job(b.id, position=a.position)


def hold(db: Database, job_id: int) -> None:
    job = db.job(job_id)
    if job and job.state == QUEUED:
        db.update_job(job_id, state=HELD, status_text="on hold")
        db.event(job_id, "info", "Put on hold.")


def release(db: Database, job_id: int) -> None:
    job = db.job(job_id)
    if job and job.state == HELD:
        db.update_job(job_id, state=QUEUED, status_text=None)
        db.event(job_id, "info", "Released from hold.")


def set_queue_running(db: Database, running: bool) -> None:
    db.set_setting("queue_running", running)
    db.event(
        None,
        "info",
        "Queue started." if running else "Queue paused: running jobs carry on, nothing new starts.",
    )


def can(job: Job, action: str) -> bool:
    """Whether a button for ``action`` makes sense for this job right now."""
    state = job.state
    testing = job.test_state in ("waiting", "running")
    return {
        "check": state not in (RUNNING, PAUSED),
        "test": state not in (RUNNING, PAUSED) and not testing,
        "cancel_test": testing,
        "corescan": state not in (RUNNING, PAUSED) and not testing
                    and job.kind in ("udb", "namd", "amber"),
        "run_now": state in (QUEUED, HELD) and not testing,
        "kill": state in (RUNNING, PAUSED),
        "pause": state == RUNNING,
        "resume": state == PAUSED,
        "retry": state in (FAILED, KILLED, "done"),
        "hold": state == QUEUED,
        "release": state == HELD,
        "edit": state not in (RUNNING, PAUSED) and not testing,
        "remove": state not in (RUNNING, PAUSED) and not testing,
        "duplicate": True,
    }.get(action, False)  # fmt: skip
