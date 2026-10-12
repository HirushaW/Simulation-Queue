"""How far a running job has got: read from the program's own log files."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..analyze import amber
from ..analyze.namd import parse_log
from ..db import Job


@dataclass
class Progress:
    fraction: float | None = None
    stage: str | None = None  # e.g. "4/5 NPT equilibration"
    steps_done: int | None = None
    steps_total: int | None = None
    s_per_step: float | None = None
    remaining_s: float | None = None
    nan: bool = False
    fatal: str | None = None
    atoms: int | None = None
    log: str | None = None  # the file this was read from


def _fresh(path: Path, since: float) -> bool:
    """A log written by this run (not left over from an earlier one)."""
    try:
        return path.stat().st_mtime >= since - 2
    except OSError:
        return False


def redirect_target(command: str) -> str | None:
    """The file a command's output goes to: ``namd3 md.conf > md.log`` → ``md.log``."""
    m = re.findall(r"(?:^|[^0-9&2])>>?\s*([^\s;&|<>]+)", command)
    return m[-1] if m else None


def _udb_progress(job: Job, analysis: dict[str, Any], folder: Path, since: float) -> Progress:
    stages = analysis.get("stages") or []
    total = sum(s["steps"] for s in stages)
    prog = Progress(steps_total=total)
    log = None
    current = None

    if job.choice == "run_full.sh":
        prefix = analysis.get("prefix", "")
        p2_log = folder / f"logs/{prefix}_full_p2.log"
        resumed = job.meta.get("launch_args") == "resume"
        # The current phase is whichever phase log this run is writing.
        phase = 2 if resumed or _fresh(p2_log, since) else 1
        if resumed:
            # `run_full.sh resume` runs production alone, from the phase-2 restart.
            in_phase = [s for s in stages if s["number"] == 5]
            before = sum(s["steps"] for s in stages if s["number"] < 5)
        else:
            in_phase = [s for s in stages if (s.get("phase") or 1) == phase]
            before = sum(s["steps"] for s in stages if (s.get("phase") or 1) < phase)
        log_path = folder / f"logs/{prefix}_full_p{phase}.log"
        log = parse_log(log_path) if _fresh(log_path, since) else None
        step = (log.step or 0) if log else 0
        reached = 0
        for s in in_phase:
            current = s
            if step < reached + s["steps"]:
                break
            reached += s["steps"]
        done = before + min(step, sum(s["steps"] for s in in_phase))
        prog.log = str(log_path)
    else:
        done = 0
        for s in stages:
            if (folder / f"{s['output']}.coor").is_file():
                done += s["steps"]
                continue
            current = s
            log_path = folder / s["log"]
            log = parse_log(log_path) if _fresh(log_path, since) else None
            if log and log.step:
                done += min(log.step, s["steps"])
            prog.log = str(log_path)
            break

    prog.steps_done = done
    if total:
        prog.fraction = min(1.0, done / total)
    if current is not None:
        prog.stage = f"{current['number']}/{len(stages)} {current['title']}"
    if log:
        prog.s_per_step = log.s_per_step
        prog.nan = log.nan
        prog.fatal = log.fatal
        prog.atoms = log.atoms
    return prog


def _namd_progress(
    job: Job, analysis: dict[str, Any], folder: Path, run_dir: Path | None, since: float
) -> Progress:
    total = analysis.get("total_steps") or 0
    target = redirect_target(job.command)
    path = folder / target if target else (run_dir / "output.log" if run_dir else None)
    prog = Progress(steps_total=total or None)
    if path is None or not _fresh(path, since):
        return prog
    log = parse_log(path)
    prog.log = str(path)
    first = analysis.get("first_timestep", 0) or 0
    if log.step is not None and total:
        prog.steps_done = max(0, min(total, log.step - first))
        prog.fraction = prog.steps_done / total
    prog.s_per_step = log.s_per_step
    prog.nan, prog.fatal, prog.atoms = log.nan, log.fatal, log.atoms
    return prog


def _amber_progress(analysis: dict[str, Any], folder: Path, since: float) -> Progress:
    total = analysis.get("total_steps") or 0
    prog = Progress(steps_total=total or None)
    mdinfo = folder / (analysis.get("mdinfo") or "mdinfo")
    if _fresh(mdinfo, since):
        try:
            info = amber.parse_mdinfo(mdinfo.read_text(errors="replace"))
        except OSError:
            info = amber.AmberProgress()
        prog.log = str(mdinfo)
        if info.step is not None and total:
            prog.steps_done = min(total, info.step)
            prog.fraction = prog.steps_done / total
        prog.s_per_step = info.s_per_step
        prog.remaining_s = info.remaining_s
    mdout = folder / (analysis.get("mdout") or "mdout")
    if _fresh(mdout, since):
        out = amber.parse_mdout(mdout)
        prog.nan = out.nan
        prog.fatal = out.error
    return prog


def read_progress(
    job: Job, analysis: dict[str, Any], folder: Path, run_dir: Path | None, since: float
) -> Progress:
    if job.kind == "udb":
        prog = _udb_progress(job, analysis, folder, since)
    elif job.kind == "namd":
        prog = _namd_progress(job, analysis, folder, run_dir, since)
    elif job.kind == "amber":
        prog = _amber_progress(analysis, folder, since)
    else:
        prog = Progress()
    known = prog.s_per_step and prog.steps_total and prog.steps_done is not None
    if prog.remaining_s is None and known:
        prog.remaining_s = max(0.0, (prog.steps_total - prog.steps_done) * prog.s_per_step)
    return prog


def latest_activity(folder: Path, run_dir: Path | None) -> float | None:
    """When the job last wrote anything we can see (for the stall warning)."""
    newest = None
    places = [folder, folder / "logs"]
    for place in places:
        try:
            with os.scandir(place) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False):
                        mtime = entry.stat().st_mtime
                        newest = mtime if newest is None else max(newest, mtime)
        except OSError:
            continue
    if run_dir is not None:
        try:
            mtime = (run_dir / "output.log").stat().st_mtime
            newest = mtime if newest is None else max(newest, mtime)
        except OSError:
            pass
    return newest
