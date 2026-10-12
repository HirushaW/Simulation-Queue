"""How jobs are described to people: shared by the window and the command line."""

from __future__ import annotations

import time

from . import fmt
from .db import DONE, FAILED, HELD, KILLED, PAUSED, QUEUED, RUNNING, Database, Job
from .engine.scheduler import Busy, PlanItem, plan

KIND_NAMES = {"udb": "UDB", "namd": "NAMD", "amber": "AMBER", "command": "Command"}


def check_text(job: Job) -> str:
    if job.test_state == "running":
        return "testing…"
    if job.test_state == "waiting":
        return "test queued"
    if job.check_state == "checking":
        return "…"
    if job.check_state == "failed":
        return "✘"
    if job.test_state == "failed":
        return "✘ test"
    if job.test_state == "ok" and job.check_state in ("ok", "warn", "none"):
        return "✔✔"
    return {"ok": "✔", "warn": "✔ !", "none": "–"}.get(job.check_state, "–")


def gpu_text(job: Job) -> str:
    if job.gpu == "none":
        return "–"
    if job.gpu == "auto":
        return "any"
    return f"GPU {job.gpu}"


def clock(ts: float | None, now: float | None = None) -> str:
    """A time of day, with the weekday when it is not today."""
    if ts is None:
        return "–"
    now = now or time.time()
    t = time.localtime(ts)
    if time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d", time.localtime(now)):
        return time.strftime("%H:%M", t)
    if abs(ts - now) < 6 * 86400:
        return time.strftime("%a %H:%M", t)
    return time.strftime("%d %b %H:%M", t)


def status_text(job: Job, now: float, queue_running: bool) -> str:
    if job.state == RUNNING:
        return "▶ " + (job.status_text or "running")
    if job.state == PAUSED:
        return "⏸ " + (job.status_text or "paused")
    if job.state == DONE:
        return "✔ done"
    if job.state == FAILED:
        return "✘ failed"
    if job.state == KILLED:
        return "■ " + (job.status_text or "killed")
    if job.state == HELD:
        return "on hold" if not job.status_text else job.status_text
    if job.state == QUEUED:
        if job.check_state == "failed":
            return "check failed"
        if job.retry_at and job.retry_at > now:
            return f"↻ {job.status_text or 'retry'} at {clock(job.retry_at, now)}"
        if job.start_at and job.start_at > now:
            return f"starts {clock(job.start_at, now)}"
        if job.after_job:
            return f"after #{job.after_job}"
        if job.meta.get("run_now"):
            return "starting…"
        return "waiting" if queue_running else "waiting (queue paused)"
    return job.state


def progress_text(job: Job) -> str:
    if job.progress is None:
        return ""
    return f"{100 * job.progress:.0f}%"


def time_left(job: Job) -> str:
    if job.state in (RUNNING, PAUSED):
        return fmt.duration(job.remaining_s) if job.remaining_s is not None else "?"
    if job.state in (QUEUED, HELD):
        return fmt.duration(job.est_seconds) if job.est_seconds else "?"
    if job.state == DONE and job.started and job.ended:
        return "took " + fmt.duration(job.ended - job.started)
    return ""


def queue_plan(
    db: Database, jobs: list[Job], total_cpus: int, gpus: list[int], now: float | None = None
) -> dict[int, tuple[float | None, float | None]]:
    """Predicted (start, end) for each job, following the scheduler's own rules."""
    now = now or time.time()
    running: dict[object, Busy] = {}
    for run in db.runs(RUNNING):
        if run.purpose != "job":
            continue
        job = next((j for j in jobs if j.id == run.job_id), None)
        end = now + job.remaining_s if job and job.remaining_s is not None else None
        running[run.job_id] = Busy(run.cores, run.gpu_index, end)
    items = []
    for job in jobs:
        if job.state != QUEUED:
            continue
        not_before = max(filter(None, (job.start_at, job.retry_at)), default=None)
        items.append(
            PlanItem(
                job.id,
                job.cores,
                job.gpu,
                job.est_seconds,
                not_before,
                job.after_job,
                job.check_state != "failed",
            )
        )
    result = plan(now, total_cpus, gpus, running, items)
    for job in jobs:
        if job.state in (RUNNING, PAUSED):
            end = now + job.remaining_s if job.remaining_s is not None else None
            result[job.id] = (job.started, end)
    return result


def when_text(job: Job, planned: tuple[float | None, float | None] | None, now: float) -> str:
    if job.state in (RUNNING, PAUSED):
        return "ends " + clock(planned[1], now) if planned and planned[1] else "running"
    if job.state == QUEUED and planned:
        start, end = planned
        if start is None:
            return "?"
        text = "now" if start <= now + 5 else clock(start, now)
        return f"{text} → {clock(end, now)}" if end else text
    if job.finished and job.ended:
        return clock(job.ended, now)
    return ""


def service_gpus(db: Database) -> list[int]:
    return list((db.setting("service") or {}).get("gpus") or [])


def job_rows(db: Database, jobs: list[Job]) -> list[tuple[str, ...]]:
    from .engine.client import queue_cpus

    now = time.time()
    running = bool(db.setting("queue_running"))
    planned = queue_plan(db, jobs, queue_cpus(db), service_gpus(db), now)
    rows = []
    for job in jobs:
        rows.append((
            str(job.id), job.name, KIND_NAMES.get(job.kind, job.kind), str(job.cores),
            gpu_text(job), check_text(job), status_text(job, now, running), progress_text(job),
            time_left(job), when_text(job, planned.get(job.id), now),
        ))  # fmt: skip
    return rows
