"""Test runs: the real pipeline, shrunk to a few thousand steps, in a scratch copy.

The job folder is copied to ``~/.simq/scratch/<run id>/`` without
trajectories, restart files and earlier outputs, the step counts in the copy
are cut down, and the job's own command runs there. A test proves the whole
pipeline works (tLeap, every stage hand-over, the GPU mode) and measures the
real speed. The job's folder is never touched.
"""

from __future__ import annotations

import math
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..analyze import amber
from ..analyze.namd import evaluate_config, parse_log
from ..db import Job
from ..paths import data_dir
from .progress import redirect_target

# Step counts used in a UDB test run (rounded to stepspercycle by udb_mult).
UDB_TEST_STEPS = {
    "min_steps": 100,
    "heat_steps": 1000,
    "nvt_steps": 1000,
    "npt_free": 1000,
    "prod_steps": 2000,
}
MD_TEST_STEPS = 2000
MIN_TEST_STEPS = 100
SMOKE_SECONDS = 120  # a generic command passes if it runs this long without failing
_MAX_COPY = 2 * 1024**3

_SKIP_ALWAYS = re.compile(
    r"\.(dcd|dvd|nc|netcdf|mdcrd|crd\.gz|xtc|trr|tng)$|\.simq-interrupted-", re.IGNORECASE
)
_SKIP_UDB = re.compile(r"\.restart\.|^0[1-5]_[a-z_]+\.(coor|vel|xsc|xst)$|_full_p[12]\.")


def scratch_dir() -> Path:
    path = data_dir() / "scratch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ignore(kind: str, root: Path):
    def ignore(directory: str, names: list[str]) -> list[str]:
        skipped = []
        for name in names:
            path = os.path.join(directory, name)
            if name == "logs" and os.path.isdir(path):
                skipped.append(name)
            elif _SKIP_ALWAYS.search(name) or (kind == "udb" and _SKIP_UDB.search(name)):
                skipped.append(name)
            elif os.path.isfile(path) and not os.path.islink(path):
                try:
                    if os.path.getsize(path) > _MAX_COPY:
                        skipped.append(name)
                except OSError:
                    skipped.append(name)
        return skipped

    return ignore


def _set_tcl(text: str, key: str, value: int) -> str:
    return re.sub(rf"^(\s*set\s+{key}\s+)\S+", rf"\g<1>{value}", text, flags=re.MULTILINE)


def _shrink_udb(work: Path) -> None:
    conf = work / "udb_md.conf"
    text = conf.read_text()
    for key, value in UDB_TEST_STEPS.items():
        text = _set_tcl(text, key, value)
    m = re.search(r"^\s*set\s+npt_restrained\s+(\d+)", text, re.MULTILINE)
    if m and int(m.group(1)) > 0:
        text = _set_tcl(text, "npt_restrained", 1000)
    conf.write_text(text)


def _round_up(n: int, multiple: int) -> int:
    multiple = max(1, multiple)
    return multiple * math.ceil(n / multiple)


def _shrink_namd(conf: Path) -> None:
    cfg = evaluate_config(conf)
    spc = cfg.steps_per_cycle or 20
    md, mini = _round_up(MD_TEST_STEPS, spc), _round_up(MIN_TEST_STEPS, spc)
    lines = []
    for line in conf.read_text().splitlines():
        if re.match(r"^\s*run\s", line, re.IGNORECASE):
            line = re.sub(r"^(\s*run)\s.*$", rf"\g<1> {md}", line, flags=re.IGNORECASE)
        elif re.match(r"^\s*minimize\s", line, re.IGNORECASE):
            line = re.sub(r"^(\s*minimize)\s.*$", rf"\g<1> {mini}", line, flags=re.IGNORECASE)
        elif re.match(r"^\s*numsteps\s", line, re.IGNORECASE):
            line = re.sub(r"^(\s*numsteps)\s.*$", rf"\g<1> {md}", line, flags=re.IGNORECASE)
        lines.append(line)
    conf.write_text("\n".join(lines) + "\n")


def _shrink_mdin(mdin: Path) -> None:
    text = mdin.read_text()

    def cap(m: re.Match, limit: int) -> str:
        return f"{m.group(1)}{min(int(m.group(2)), limit)}"

    text = re.sub(r"(nstlim\s*=\s*)(\d+)", lambda m: cap(m, MD_TEST_STEPS), text, flags=re.I)
    text = re.sub(r"(maxcyc\s*=\s*)(\d+)", lambda m: cap(m, MIN_TEST_STEPS), text, flags=re.I)
    mdin.write_text(text)


@dataclass
class TestPlan:
    work_dir: Path
    smoke: bool  # generic command: passes by surviving SMOKE_SECONDS
    note: str


def prepare(job: Job, run_id: int) -> TestPlan:
    """Copy the job folder to scratch and shrink it. Raises OSError on failure."""
    source = Path(job.folder)
    work = scratch_dir() / str(run_id)
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(source, work, symlinks=True, ignore=_ignore(job.kind, source))
    if job.kind == "udb":
        _shrink_udb(work)
        return TestPlan(work, False, "UDB pipeline with every stage cut to 100–2000 steps")
    if job.kind == "namd" and job.choice and (work / job.choice).is_file():
        _shrink_namd(work / job.choice)
        return TestPlan(work, False, f"{job.choice} cut to {MD_TEST_STEPS} steps")
    if job.kind == "amber":
        cmd = amber.parse_command(job.command)
        if cmd and (work / cmd.mdin).is_file():
            _shrink_mdin(work / cmd.mdin)
            return TestPlan(work, False, f"{cmd.mdin} cut to {MD_TEST_STEPS} steps")
    return TestPlan(
        work, True, f"your command, stopped after {SMOKE_SECONDS // 60} minutes if still running"
    )


@dataclass
class Measurement:
    s_per_step: float | None = None
    atoms: int | None = None
    detail: str = ""


def measure(job: Job, analysis: dict[str, Any], work: Path) -> Measurement:
    """Read the speed a finished (or stopped) test run reached."""
    out = Measurement()
    logs: list[Path] = []
    if job.kind == "udb":
        prefix = analysis.get("prefix", "")
        logs = [work / "logs/05_production.log", work / f"logs/{prefix}_full_p2.log",
                work / "logs/04_npt_equil.log", work / "logs/03_nvt_equil.log",
                work / "logs/02_heat.log", work / f"logs/{prefix}_full_p1.log"]  # fmt: skip
    elif job.kind == "namd":
        target = redirect_target(job.command)
        if target:
            logs = [work / target]
    elif job.kind == "amber":
        cmd = amber.parse_command(job.command)
        if cmd:
            info = work / cmd.mdinfo
            if info.is_file():
                parsed = amber.parse_mdinfo(info.read_text(errors="replace"))
                out.s_per_step = parsed.s_per_step
                out.detail = cmd.mdinfo
            out.atoms = amber.prmtop_atoms(work / cmd.prmtop)
        return out
    for path in logs:
        if not path.is_file():
            continue
        log = parse_log(path)
        out.atoms = out.atoms or log.atoms
        if log.s_per_step:
            out.s_per_step = log.s_per_step
            out.detail = str(path.relative_to(work))
            break
        if log.wallclock_s and log.step:
            out.s_per_step = log.wallclock_s / max(1, log.step)
            out.detail = str(path.relative_to(work)) + " (whole-run average)"
            break
    if out.atoms is None and job.kind == "udb":
        prefix = analysis.get("prefix", "")
        out.atoms = amber.prmtop_atoms(work / f"{prefix}_solvated.prmtop")
    return out


def cleanup(work: str | Path | None) -> None:
    if work and Path(work).is_relative_to(scratch_dir()):
        shutil.rmtree(work, ignore_errors=True)
