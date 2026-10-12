"""The queue service: a background process that runs the queue.

It is the only process that starts, pauses or stops simulations. Once a
second it:

1. records a heartbeat (so the window can show that the service is running),
2. carries out requests from the window and the command line,
3. notices runs that ended, and retries or fails them,
4. reads progress from the running jobs' logs,
5. watches health (stalls, NaN energies, disk space, GPU temperature),
6. checks newly added jobs (in "verify all first" mode),
7. starts the jobs that fit on the free CPUs and GPUs.

Jobs run in their own process groups and outlive the service: if it stops,
or the window closes, they keep running and are picked up again when the
service starts.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any

from .. import __version__, fmt
from ..analyze import udb
from ..analyze.namd import explain
from ..db import (
    DONE,
    FAILED,
    HELD,
    KILLED,
    PAUSED,
    QUEUED,
    RUNNING,
    Database,
    Job,
    Run,
)
from ..detect.software import DetectionReport, detect_software, last_report
from ..monitor.gpu import GpuMonitor
from ..paths import data_dir
from . import launch, testrun
from .check import extra_path_dirs, free_bytes, quick_check
from .estimate import estimate, mode_of
from .notify import Notifier, send_email
from .progress import latest_activity, read_progress
from .scheduler import Busy, Want, allocate_cpus, choose, cpu_list_text, usable_cpus

KILL_GRACE_S = 20
RETRY_DELAY_S = 30
PROGRESS_EVERY_S = 3
HEALTH_EVERY_S = 15
WARN_REPEAT_S = 3600

_EXIT_MEANINGS = {
    96: "the job folder could not be entered",
    126: "the program is not executable",
    127: "a command was not found",
    134: "the program aborted",
    137: "the job was killed (SIGKILL), possibly because the machine ran out of memory",
    139: "the program crashed (segmentation fault)",
    143: "the job was stopped (SIGTERM)",
}


class ServiceAlreadyRunning(RuntimeError):
    pass


def lock_service() -> int:
    """Hold the single-instance lock for the life of the process."""
    fd = os.open(data_dir() / "service.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise ServiceAlreadyRunning("the SimQ service is already running") from exc
    return fd


class Service:
    def __init__(
        self,
        db: Database | None = None,
        gpu: GpuMonitor | None = None,
        report: DetectionReport | None = None,
    ) -> None:
        self.db = db or Database()
        self.gpu = gpu if gpu is not None else GpuMonitor()
        self.report = report
        self.procs: dict[int, subprocess.Popen] = {}
        self.samples: dict[int, deque[tuple[float, int]]] = {}
        self.warned: dict[tuple[int, str], float] = {}
        self.notifier = Notifier(on_email_error=self._email_failed)
        self.stopping = False
        self._last_progress = 0.0
        self._last_health = 0.0
        self._busy = False
        self._finished_while_busy = 0
        self.settings: dict[str, Any] = self.db.settings()

    # -- lifecycle ----------------------------------------------------------

    def load_software(self) -> None:
        if self.report is None:
            self.report = last_report()
        if self.report is None:
            try:
                self.report = detect_software()
            except Exception as exc:  # detection problems must not stop the queue
                self.db.event(None, "warn", f"Software detection failed: {exc}")

    def adopt(self) -> None:
        """Pick up runs that were going when the service last stopped."""
        for run in self.db.runs(RUNNING):
            if launch.alive(run.pid, run.proc_start):
                self.db.event(run.job_id, "info", "Service restarted; the job is still running.")
            else:
                code = launch.exit_code(run.run_dir)
                self._finish(run, code, time.time(), interrupted=code is None)

    def run_forever(self, tick: float = 1.0) -> None:
        lock = lock_service()
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stopping", True))
        try:
            self.load_software()
            self.adopt()
            self.db.event(None, "info", f"Queue service started (pid {os.getpid()}).")
            while not self.stopping:
                started = time.monotonic()
                try:
                    self.tick(time.time())
                except Exception as exc:  # one bad tick must not stop the queue
                    self.db.event(None, "error", f"Service error: {exc!r}")
                time.sleep(max(0.05, tick - (time.monotonic() - started)))
            self.db.event(None, "info", "Queue service stopped; running jobs keep running.")
            self.db.set_setting("service", {"pid": None, "time": time.time(), "stopped": True})
        finally:
            os.close(lock)

    def tick(self, now: float) -> None:
        self.settings = self.db.settings()
        self.db.set_setting(
            "service",
            {
                "pid": os.getpid(),
                "time": now,
                "version": __version__,
                "gpus": self._gpus(),
                "cpus": len(self._usable()),
            },
        )
        self.handle_requests(now)
        self.poll_runs(now)
        if now - self._last_progress >= PROGRESS_EVERY_S:
            self._last_progress = now
            self.update_progress(now)
        if now - self._last_health >= HEALTH_EVERY_S:
            self._last_health = now
            self.check_health(now)
        self.auto_checks()
        self.schedule(now)
        self.track_queue_done()

    # -- helpers ----------------------------------------------------------------

    def _usable(self) -> list[int]:
        return usable_cpus(int(self.settings.get("cpu_budget") or 0))

    def _gpus(self) -> list[int]:
        return [g.index for g in self.gpu.info.gpus]

    def _notify(self, kind: str, title: str, body: str) -> None:
        self.notifier.send(self.settings, kind, title, body)

    def _email_failed(self, error: str) -> None:
        with contextlib.suppress(Exception):
            self.db.event(None, "warn", f"Email could not be sent: {error}")

    def _warn_once(self, job: Job, key: str, text: str, now: float) -> None:
        last = self.warned.get((job.id, key), 0.0)
        if now - last < WARN_REPEAT_S:
            return
        self.warned[(job.id, key)] = now
        self.db.event(job.id, "warn", text)
        self._notify("warnings", f"Warning: {job.name}", text)

    def active_runs(self, job_id: int) -> list[Run]:
        return [r for r in self.db.job_runs(job_id) if r.state in ("waiting", RUNNING)]

    def refresh_estimate(self, job_id: int) -> None:
        job = self.db.job(job_id)
        if job is None:
            return
        seconds, source = estimate(job, self.db.speeds())
        self.db.update_job(job_id, est_seconds=seconds, est_source=source)

    # -- requests -------------------------------------------------------------

    def handle_requests(self, now: float) -> None:
        for job_id, action, arg in self.db.take_requests():
            if action == "stop":
                self.stopping = True
                continue
            if action == "rescan_software":
                self.report = None
                self.load_software()
                continue
            if action == "test_email":
                self._test_email()
                continue
            job = self.db.job(job_id) if job_id is not None else None
            if job is None:
                continue
            handler = {
                "check": self.req_check,
                "test": self.req_test,
                "corescan": self.req_corescan,
                "cancel_test": self.req_cancel_test,
                "run_now": self.req_run_now,
                "kill": self.req_kill,
                "pause": self.req_pause,
                "resume": self.req_resume,
                "retry": self.req_retry,
            }.get(action)
            if handler is not None:
                handler(job, arg, now)

    def _test_email(self) -> None:
        try:
            send_email(
                self.settings,
                "SimQ: test email",
                "This is a test email from SimQ. Notifications are working.",
            )
            self.db.event(None, "info", "Test email sent.")
        except Exception as exc:
            self.db.event(None, "error", f"Test email failed: {exc}")

    def req_check(self, job: Job, _arg: Any, _now: float) -> None:
        self.run_check(job)

    def req_test(self, job: Job, _arg: Any, _now: float) -> None:
        if any(r.purpose == "test" for r in self.active_runs(job.id)):
            return
        self.db.add_run(job.id, "test", job.cores)
        self.db.update_job(job.id, test_state="waiting")
        self.db.event(job.id, "info", "Test run requested; it starts when CPUs/GPU are free.")

    def req_corescan(self, job: Job, arg: Any, _now: float) -> None:
        counts = sorted({int(c) for c in (arg or []) if int(c) > 0})
        if not counts:
            return
        self.db.merge_meta(job.id, corescan={"counts": counts, "results": {}})
        for cores in counts:
            self.db.add_run(job.id, "test", cores, meta={"corescan": True})
        self.db.update_job(job.id, test_state="waiting")
        self.db.event(
            job.id,
            "info",
            "Core-count test requested for " + ", ".join(str(c) for c in counts) + " CPUs.",
        )

    def req_cancel_test(self, job: Job, _arg: Any, now: float) -> None:
        for run in self.active_runs(job.id):
            if run.purpose != "test":
                continue
            if run.state == "waiting":
                self.db.update_run(run.id, state=KILLED, ended=now)
            else:
                launch.stop(run.pid)
                self.db.update_run(run.id, kill_sent=now)
        self.db.update_job(job.id, test_state="none")

    def req_run_now(self, job: Job, _arg: Any, _now: float) -> None:
        if job.state in (QUEUED, HELD):
            first = min((j.position for j in self.db.jobs()), default=1.0)
            self.db.update_job(job.id, state=QUEUED, position=first - 1, start_at=None)
            self.db.merge_meta(job.id, run_now=True)

    def req_kill(self, job: Job, _arg: Any, now: float) -> None:
        runs = [r for r in self.active_runs(job.id) if r.purpose == "job"]
        for run in runs:
            launch.stop(run.pid)
            self.db.update_run(run.id, kill_sent=now)
        if not runs and job.state in (QUEUED, HELD, PAUSED):
            self.db.update_job(job.id, state=KILLED, ended=now, status_text="cancelled")
        self.db.event(job.id, "info", "Kill requested.")

    def req_pause(self, job: Job, _arg: Any, _now: float) -> None:
        for run in self.active_runs(job.id):
            if run.purpose == "job" and run.state == RUNNING and launch.pause(run.pid):
                self.db.update_run(run.id, paused=1)
                self.db.update_job(job.id, state=PAUSED)
                self.db.event(job.id, "info", "Paused.")

    def req_resume(self, job: Job, _arg: Any, _now: float) -> None:
        for run in self.active_runs(job.id):
            if run.purpose == "job" and run.paused and launch.resume(run.pid):
                meta = dict(run.meta)
                meta.pop("guard_paused", None)
                self.db.update_run(run.id, paused=0, meta=meta)
                self.db.update_job(job.id, state=RUNNING)
                self.db.event(job.id, "info", "Resumed.")

    def req_retry(self, job: Job, _arg: Any, _now: float) -> None:
        if job.state not in (FAILED, KILLED, DONE, HELD):
            return
        self.db.update_job(
            job.id,
            state=QUEUED,
            attempt=0,
            exit_code=None,
            ended=None,
            retry_at=None,
            check_state="none",
            status_text=None,
        )
        self.db.merge_meta(job.id, resume=True, failure=None)
        self.db.event(job.id, "info", "Queued again.")

    # -- checks -----------------------------------------------------------------

    def run_check(self, job: Job) -> bool:
        self.db.update_job(job.id, check_state="checking")
        try:
            result = quick_check(
                job,
                self.report,
                len(self._usable()),
                self._gpus(),
                float(self.settings.get("min_free_gb") or 0),
            )
        except Exception as exc:
            self.db.update_job(job.id, check_state="failed")
            self.db.merge_meta(
                job.id,
                check={
                    "time": time.time(),
                    "state": "failed",
                    "findings": [["error", f"Check crashed: {exc}"]],
                },
            )
            return False
        meta: dict[str, Any] = {"check": result.to_meta()}
        if result.analysis:
            meta["analysis"] = result.analysis
        self.db.merge_meta(job.id, **meta)
        self.db.update_job(job.id, check_state=result.state)
        self.refresh_estimate(job.id)
        if result.state == "failed":
            first = next(t for lvl, t in result.findings if lvl == "error")
            self.db.event(job.id, "error", f"Check failed: {first}")
        return result.state != "failed"

    def auto_checks(self) -> None:
        if self.settings.get("verify_mode") != "all_first":
            return
        done = 0
        for job in self.db.jobs():
            if job.state == QUEUED and job.check_state == "none":
                self.run_check(job)
                done += 1
                if done >= 3:
                    return

    # -- running and finishing ---------------------------------------------------

    def poll_runs(self, now: float) -> None:
        for run in self.db.runs(RUNNING):
            proc = self.procs.get(run.id)
            if proc is not None:
                code = proc.poll()
                finished = code is not None
                if finished:
                    recorded = launch.exit_code(run.run_dir)
                    code = recorded if recorded is not None else (128 - code if code < 0 else code)
            else:
                finished = not launch.alive(run.pid, run.proc_start)
                code = launch.exit_code(run.run_dir) if finished else None
            if finished:
                self._finish(run, code, now, interrupted=code is None)
                continue
            limit = float(self.settings.get("test_minutes") or 10) * 60
            if run.purpose == "test" and run.started and not run.kill_sent:
                smoke = run.meta.get("smoke")
                if now - run.started > (testrun.SMOKE_SECONDS if smoke else limit):
                    meta = dict(run.meta, timeout=True)
                    launch.stop(run.pid)
                    self.db.update_run(run.id, kill_sent=now, meta=meta)
            if run.kill_sent and now - run.kill_sent > KILL_GRACE_S:
                launch.force_stop(run.pid)

    def _finish(self, run: Run, code: int | None, now: float, interrupted: bool = False) -> None:
        self.procs.pop(run.id, None)
        self.samples.pop(run.id, None)
        user_stop = bool(run.kill_sent) and not run.meta.get("timeout")
        if user_stop:
            state = KILLED
        else:
            state = DONE if code == 0 else FAILED
        self.db.update_run(run.id, state=state, ended=now, exit_code=code)
        job = self.db.job(run.job_id)
        if job is None:
            return
        if run.purpose == "test":
            self._finish_test(job, run, code, now, user_stop)
        else:
            self._finish_job(job, run, code, now, user_stop, interrupted)

    def _failure_reason(self, job: Job, run: Run, code: int | None, interrupted: bool) -> str:
        analysis = job.meta.get("analysis") or {}
        prog = read_progress(
            job,
            analysis,
            Path(job.folder),
            Path(run.run_dir) if run.run_dir else None,
            run.started or 0,
        )
        if prog.fatal:
            text = f"{prog.fatal}"
            hint = explain(prog.fatal)
            return f"{text} — {hint}" if hint else text
        if interrupted:
            return (
                "The job stopped while the service was not watching (was the computer restarted?)."
            )
        meaning = _EXIT_MEANINGS.get(code or 0, "")
        last = [line for line in launch.tail(Path(run.run_dir or "") / "output.log", 30)
                .splitlines() if line.strip()]  # fmt: skip
        detail = f" Last output: {last[-1].strip()[:200]}" if last else ""
        return f"Exit code {code}" + (f": {meaning}." if meaning else ".") + detail

    def _record_speed(
        self, job: Job, s_per_step: float | None, cores: int, source: str, atoms: int | None = None
    ) -> None:
        analysis = job.meta.get("analysis") or {}
        program = analysis.get("program")
        if not s_per_step or program not in ("namd", "amber"):
            return
        self.db.add_speed(
            job_id=job.id,
            job_name=job.name,
            program=program,
            mode=mode_of(job),
            cores=cores,
            atoms=atoms or analysis.get("atoms"),
            s_per_step=s_per_step,
            timestep_fs=analysis.get("timestep_fs"),
            source=source,
        )

    def _finish_job(
        self, job: Job, run: Run, code: int | None, now: float, user_stop: bool, interrupted: bool
    ) -> None:
        last_speed = (job.meta.get("speed") or {}).get("s_per_step")
        if last_speed and job.meta.get("speed", {}).get("source") == "live":
            self._record_speed(job, last_speed, run.cores, "run")
        self._finished_while_busy += 1
        if user_stop:
            self.db.update_job(
                job.id,
                state=KILLED,
                ended=now,
                exit_code=code,
                status_text="killed",
                remaining_s=None,
            )
            self.db.event(job.id, "info", "Killed.")
            return
        if code == 0:
            self.db.update_job(
                job.id,
                state=DONE,
                ended=now,
                exit_code=0,
                progress=1.0 if job.progress is not None else None,
                remaining_s=0,
                status_text="done",
            )
            took = fmt.duration(now - (job.started or now))
            self.db.event(job.id, "info", f"Finished successfully after {took}.")
            self._notify(
                "done",
                f"Done: {job.name}",
                f"{job.name} finished after {took}.\nFolder: {job.folder}",
            )
            return
        reason = self._failure_reason(job, run, code, interrupted)
        self.db.merge_meta(job.id, failure=reason)
        if job.attempt <= job.retries:
            retry_at = now + RETRY_DELAY_S
            self.db.update_job(
                job.id,
                state=QUEUED,
                retry_at=retry_at,
                exit_code=code,
                status_text=f"retry {job.attempt}/{job.retries}",
            )
            self.db.merge_meta(job.id, resume=True)
            self.db.event(
                job.id,
                "warn",
                f"Failed ({reason}). Retrying in "
                f"{RETRY_DELAY_S} s (retry {job.attempt} of "
                f"{job.retries}).",
            )
            return
        self.db.update_job(
            job.id, state=FAILED, ended=now, exit_code=code, status_text="failed", remaining_s=None
        )
        self.db.event(job.id, "error", f"Failed: {reason}")
        self._notify(
            "failed",
            f"Failed: {job.name}",
            f"{job.name} failed.\n\n{reason}\n\nFolder: {job.folder}",
        )
        self._hold_dependents(job)

    def _hold_dependents(self, failed: Job) -> None:
        for other in self.db.jobs():
            if other.after_job == failed.id and other.state == QUEUED:
                self.db.update_job(
                    other.id, state=HELD, status_text=f"held: job #{failed.id} failed"
                )
                self.db.event(
                    other.id, "warn", f"On hold: it waits for job #{failed.id}, which failed."
                )

    def _finish_test(
        self, job: Job, run: Run, code: int | None, now: float, user_stop: bool
    ) -> None:
        work = Path(run.work_dir) if run.work_dir else None
        smoke = bool(run.meta.get("smoke"))
        timed_out = bool(run.meta.get("timeout"))
        others = [r for r in self.active_runs(job.id) if r.purpose == "test" and r.id != run.id]
        if user_stop:
            testrun.cleanup(work)
            if not others:
                self.db.update_job(job.id, test_state="none")
            self.db.event(job.id, "info", "Test run cancelled.")
            return
        analysis = job.meta.get("analysis") or {}
        measured = testrun.measure(job, analysis, work) if work else testrun.Measurement()
        if smoke:
            ok = code == 0 or timed_out
        else:
            ok = code == 0 or (timed_out and measured.s_per_step is not None)
        speed_text = ""
        if measured.s_per_step:
            ts = analysis.get("timestep_fs") or 2.0
            ns_day = 86400 / measured.s_per_step * ts / 1e6
            speed_text = f"{measured.s_per_step:.4g} s/step ({ns_day:.3g} ns/day)"
            self._record_speed(
                job,
                measured.s_per_step,
                run.cores,
                "corescan" if run.meta.get("corescan") else "test",
                measured.atoms,
            )
        if run.meta.get("corescan"):
            scan = dict(job.meta.get("corescan") or {})
            results = dict(scan.get("results") or {})
            results[str(run.cores)] = measured.s_per_step
            scan["results"] = results
            self.db.merge_meta(job.id, corescan=scan)
        elif measured.s_per_step and run.cores == job.cores:
            self.db.merge_meta(
                job.id,
                speed={
                    "s_per_step": measured.s_per_step,
                    "source": "test run",
                    "cores": run.cores,
                    "time": now,
                },
            )
        if measured.atoms and not analysis.get("atoms"):
            analysis = dict(analysis, atoms=measured.atoms)
            self.db.merge_meta(job.id, analysis=analysis)

        if ok:
            result = {
                "state": "ok",
                "time": now,
                "speed": speed_text,
                "detail": measured.detail,
                "cores": run.cores,
            }
            message = "Test run passed" + (f": {speed_text}." if speed_text else ".")
            testrun.cleanup(work)
        else:
            reason = self._failure_reason(job, run, code, False)
            result = {"state": "failed", "time": now, "reason": reason, "work_dir": str(work)}
            message = f"Test run failed: {reason} The test copy was kept in {work}."
        if not run.meta.get("corescan"):
            self.db.merge_meta(job.id, test=result)
        if not others:
            state = "ok" if ok else "failed"
            if run.meta.get("corescan"):
                state = job.test_state if job.test_state in ("ok", "failed") else "none"
                scan = (self.db.job(job.id) or job).meta.get("corescan") or {}
                message = "Core-count test finished: " + ", ".join(
                    f"{c} CPUs {f'{s:.4g} s/step' if s else 'no result'}"
                    for c, s in sorted(scan.get("results", {}).items(), key=lambda x: int(x[0]))
                )
            self.db.update_job(job.id, test_state=state)
        self.db.event(job.id, "info" if ok else "error", message)
        self.refresh_estimate(job.id)

    # -- progress and health ------------------------------------------------------

    def update_progress(self, now: float) -> None:
        for run in self.db.runs(RUNNING):
            if run.purpose != "job":
                continue
            job = self.db.job(run.job_id)
            if job is None:
                continue
            analysis = job.meta.get("analysis") or {}
            prog = read_progress(
                job,
                analysis,
                Path(job.folder),
                Path(run.run_dir) if run.run_dir else None,
                run.started or now,
            )
            s_per_step = prog.s_per_step
            if prog.steps_done is not None:
                samples = self.samples.setdefault(run.id, deque(maxlen=200))
                if not samples or samples[-1][1] != prog.steps_done:
                    samples.append((now, prog.steps_done))
                if s_per_step is None and len(samples) >= 2:
                    (t0, s0), (t1, s1) = samples[0], samples[-1]
                    if s1 > s0:
                        s_per_step = (t1 - t0) / (s1 - s0)
            remaining = prog.remaining_s
            known = s_per_step and prog.steps_total and prog.steps_done is not None
            if remaining is None and known:
                remaining = max(0.0, (prog.steps_total - prog.steps_done) * s_per_step)
            fraction = prog.fraction
            if fraction is None and job.manual_hours and job.started:
                total = job.manual_hours * 3600
                fraction = min(0.99, (now - job.started) / total)
                remaining = max(0.0, total - (now - job.started))
            values: dict[str, Any] = {"progress": fraction, "remaining_s": remaining}
            if run.paused:
                values["status_text"] = "paused"
            elif prog.stage:
                values["status_text"] = prog.stage
            else:
                values["status_text"] = "running"
            if s_per_step and analysis.get("total_steps"):
                values["est_seconds"] = analysis["total_steps"] * s_per_step
                values["est_source"] = "live"
            self.db.update_job(job.id, **values)
            if s_per_step:
                self.db.merge_meta(
                    job.id,
                    speed={
                        "s_per_step": s_per_step,
                        "source": "live",
                        "cores": job.cores,
                        "time": now,
                    },
                )
            if prog.atoms and not analysis.get("atoms"):
                self.db.merge_meta(job.id, analysis=dict(analysis, atoms=prog.atoms))
            if prog.nan:
                self._warn_once(
                    job,
                    "nan",
                    "Energies are NaN: the simulation has probably blown up. Check the log.",
                    now,
                )

    def check_health(self, now: float) -> None:
        stall_s = float(self.settings.get("stall_minutes") or 30) * 60
        critical = float(self.settings.get("critical_free_gb") or 0) * 1024**3
        for run in self.db.runs(RUNNING):
            job = self.db.job(run.job_id)
            if job is None or run.purpose != "job":
                continue
            folder = Path(job.folder)
            if not run.paused and run.started and now - run.started > stall_s:
                newest = latest_activity(folder, Path(run.run_dir) if run.run_dir else None)
                if newest is not None and now - newest > stall_s:
                    self._warn_once(
                        job,
                        "stall",
                        f"No output for {fmt.duration(now - newest)}: the job may be stuck.",
                        now,
                    )
            free = free_bytes(folder)
            if free is None or not critical:
                continue
            if free < critical and not run.paused:
                if launch.pause(run.pid):
                    self.db.update_run(run.id, paused=1, meta=dict(run.meta, guard_paused=True))
                    self.db.update_job(job.id, state=PAUSED, status_text="paused: disk full")
                    text = (
                        f"Only {fmt.size(free)} free on the disk: the job was paused so it "
                        "does not fail half-way. Free some space; it resumes automatically."
                    )
                    self.db.event(job.id, "warn", text)
                    self._notify("warnings", f"Paused: {job.name}", text)
            elif (
                free > 2 * critical
                and run.paused
                and run.meta.get("guard_paused")
                and launch.resume(run.pid)
            ):
                meta = dict(run.meta)
                meta.pop("guard_paused", None)
                self.db.update_run(run.id, paused=0, meta=meta)
                self.db.update_job(job.id, state=RUNNING, status_text="running")
                self.db.event(job.id, "info", "Disk space is back: resumed.")
        limit = float(self.settings.get("gpu_temp_warn") or 0)
        if limit:
            for sample in self.gpu.sample():
                if sample.temperature_c and sample.temperature_c >= limit:
                    key = (-1, f"gpu{sample.index}")
                    if now - self.warned.get(key, 0.0) >= WARN_REPEAT_S:
                        self.warned[key] = now
                        text = f"GPU {sample.index} is at {sample.temperature_c:.0f} °C."
                        self.db.event(None, "warn", text)
                        self._notify("warnings", "GPU temperature", text)

    # -- scheduling ---------------------------------------------------------------

    def _job_ready(self, job: Job, now: float, queue_running: bool) -> bool:
        if job.state != QUEUED or job.check_state in ("failed", "checking"):
            return False
        if not (queue_running or job.meta.get("run_now")):
            return False
        if job.start_at and job.start_at > now:
            return False
        if job.retry_at and job.retry_at > now:
            return False
        if job.after_job:
            other = self.db.job(job.after_job)
            if other is not None and other.state != DONE:
                if other.state in (FAILED, KILLED):
                    self.db.update_job(
                        job.id, state=HELD, status_text=f"held: job #{other.id} did not finish"
                    )
                return False
        return not self.active_runs(job.id)

    def schedule(self, now: float) -> None:
        usable = self._usable()
        gpus = self._gpus()
        test_limit = float(self.settings.get("test_minutes") or 10) * 60
        busy: list[Busy] = []
        used: set[int] = set()
        for run in self.db.runs(RUNNING):
            used.update(run.cpu_list)
            if run.purpose == "test":
                end = (run.started or now) + test_limit
            else:
                job = self.db.job(run.job_id)
                end = now + job.remaining_s if job and job.remaining_s is not None else None
            busy.append(Busy(run.cores, run.gpu_index, end))

        wants: list[Want] = []
        jobs = {j.id: j for j in self.db.jobs()}
        for run in self.db.runs("waiting"):
            job = jobs.get(run.job_id)
            if job is None:
                self.db.update_run(run.id, state=KILLED)
                continue
            if job.state in (RUNNING, PAUSED):
                continue  # test after the job itself, not alongside it
            wants.append(Want(("test", run.id), run.cores, job.gpu, test_limit))
        queue_running = bool(self.settings.get("queue_running"))
        ordered = sorted(jobs.values(), key=lambda j: (not j.meta.get("run_now"), j.position))
        for job in ordered:
            if self._job_ready(job, now, queue_running):
                wants.append(Want(("job", job.id), job.cores, job.gpu, job.est_seconds))
        if not wants:
            return

        for want, gpu in choose(now, len(usable), gpus, busy, wants):
            kind, ident = want.key  # type: ignore[misc]
            cpus = allocate_cpus(usable, used, want.cpus)
            if cpus is None:
                continue
            if kind == "job":
                job = self.db.job(ident)
                if job is None:
                    continue
                # Always look again just before starting: folders get moved.
                if not self.run_check(job):
                    self._notify(
                        "failed",
                        f"Check failed: {job.name}",
                        "The job was not started because its check failed. Open SimQ to see why.",
                    )
                    continue
                if self.launch_job(self.db.job(ident) or job, cpus, gpu, now):
                    used.update(cpus)
            else:
                run = self.db.run(ident)
                job = jobs.get(run.job_id) if run else None
                if run is None or job is None:
                    continue
                if self.launch_test(job, run, cpus, gpu, now):
                    used.update(cpus)

    def _launch_spec(
        self,
        job: Job,
        run_id: int,
        work: Path,
        command: str,
        cores: int,
        cpus: list[int],
        gpu: int | None,
    ) -> launch.LaunchSpec:
        sources = []
        if self.report and self.report.amber_sh:
            sources.append(self.report.amber_sh)
        env = {"NCORES": str(cores)} if job.kind == "udb" else {}
        return launch.LaunchSpec(
            job_id=job.id,
            run_id=run_id,
            work_dir=work,
            command=launch.fill_placeholders(command, cores, gpu),
            cores=cores,
            cpus=cpus if self.settings.get("pin_cpus", True) else None,
            gpu=gpu,
            path_dirs=extra_path_dirs(self.report),
            source_files=sources,
            env=env,
        )

    def launch_job(self, job: Job, cpus: list[int], gpu: int | None, now: float) -> bool:
        command = job.command
        launch_args = ""
        note = ""
        if job.meta.get("resume") and job.kind == "udb" and job.choice:
            pkg = udb.read_package(job.folder)
            plan = udb.resume_plan(pkg, job.choice)
            if plan.move_aside:
                moved = udb.move_aside(plan.move_aside)
                note = " Moved aside: " + ", ".join(p.name for p in moved) + "."
            launch_args = plan.args
            command = udb.command_for(job.choice, plan.args)
            self.db.event(job.id, "info", plan.note + note)
        run_id = self.db.add_run(job.id, "job", job.cores)
        spec = self._launch_spec(job, run_id, Path(job.folder), command, job.cores, cpus, gpu)
        try:
            proc, created, run_dir = launch.start(spec)
        except OSError as exc:
            self.db.update_run(run_id, state=FAILED, ended=now)
            self.db.update_job(job.id, state=FAILED, ended=now, status_text="failed")
            self.db.event(job.id, "error", f"Could not start: {exc}")
            return False
        self.procs[run_id] = proc
        self.db.update_run(
            run_id,
            state=RUNNING,
            pid=proc.pid,
            proc_start=created,
            cpus=",".join(map(str, cpus)),
            gpu_index=gpu,
            run_dir=str(run_dir),
            work_dir=job.folder,
            started=now,
        )
        attempt = job.attempt + 1
        values: dict[str, Any] = {
            "state": RUNNING,
            "attempt": attempt,
            "retry_at": None,
            "status_text": "starting",
            "exit_code": None,
            "ended": None,
        }
        if attempt == 1 or not job.started:
            values["started"] = now
            # Progress is only shown when it can be known.
            trackable = (job.meta.get("analysis") or {}).get("total_steps") or job.manual_hours
            values["progress"] = 0.0 if trackable else None
        self.db.update_job(job.id, **values)
        self.db.merge_meta(
            job.id, run_now=None, resume=None, launch_args=launch_args or None, run_dir=str(run_dir)
        )
        where = f"CPU{'s' if len(cpus) > 1 else ''} {cpu_list_text(cpus)}"
        where += f", GPU {gpu}" if gpu is not None else ""
        retry = f" (attempt {attempt})" if attempt > 1 else ""
        self.db.event(job.id, "info", f"Started on {where}{retry}.")
        self._busy = True
        return True

    def launch_test(self, job: Job, run: Run, cpus: list[int], gpu: int | None, now: float) -> bool:
        try:
            plan = testrun.prepare(job, run.id)
        except OSError as exc:
            self.db.update_run(run.id, state=FAILED, ended=now)
            self.db.update_job(job.id, test_state="failed")
            self.db.merge_meta(
                job.id,
                test={
                    "state": "failed",
                    "time": now,
                    "reason": f"Could not copy the folder: {exc}",
                },
            )
            self.db.event(job.id, "error", f"Test run could not start: {exc}")
            return False
        spec = self._launch_spec(job, run.id, plan.work_dir, job.command, run.cores, cpus, gpu)
        try:
            proc, created, run_dir = launch.start(spec)
        except OSError as exc:
            testrun.cleanup(plan.work_dir)
            self.db.update_run(run.id, state=FAILED, ended=now)
            self.db.update_job(job.id, test_state="failed")
            self.db.event(job.id, "error", f"Test run could not start: {exc}")
            return False
        self.procs[run.id] = proc
        self.db.update_run(
            run.id,
            state=RUNNING,
            pid=proc.pid,
            proc_start=created,
            cpus=",".join(map(str, cpus)),
            gpu_index=gpu,
            run_dir=str(run_dir),
            work_dir=str(plan.work_dir),
            started=now,
            meta=dict(run.meta, smoke=plan.smoke),
        )
        self.db.update_job(job.id, test_state="running")
        self.db.event(
            job.id,
            "info",
            f"Test run started ({plan.note}) on {run.cores} CPUs"
            + (f" and GPU {gpu}" if gpu is not None else "")
            + ".",
        )
        return True

    def track_queue_done(self) -> None:
        active = self.db.runs(RUNNING, "waiting")
        queued = [j for j in self.db.jobs() if j.state == QUEUED and j.check_state != "failed"]
        busy = bool(active) or (bool(queued) and bool(self.settings.get("queue_running")))
        if self._busy and not busy and self._finished_while_busy:
            self.db.event(None, "info", "Queue finished: nothing left to run.")
            self._notify("queue_done", "Queue finished", "All queued jobs have finished.")
            self._finished_while_busy = 0
        self._busy = busy


def main() -> int:
    try:
        Service().run_forever()
    except ServiceAlreadyRunning:
        print("simq: the queue service is already running.")
        return 0
    return 0
