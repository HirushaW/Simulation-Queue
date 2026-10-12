"""The quick check: everything that can be verified about a job in seconds.

It never changes the job's folder. Besides the verdict it returns what it
learned (stages, step counts, atoms, trajectory size), which the estimates
and progress tracking use.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from .. import fmt
from ..analyze import amber, udb
from ..analyze.namd import evaluate_config
from ..db import Job
from ..detect.software import DetectionReport

OK, WARN, ERROR = "ok", "warn", "error"


@dataclass
class CheckResult:
    findings: list[tuple[str, str]] = field(default_factory=list)  # (level, text)
    analysis: dict[str, Any] = field(default_factory=dict)

    def add(self, level: str, text: str) -> None:
        self.findings.append((level, text))

    @property
    def state(self) -> str:
        levels = {level for level, _ in self.findings}
        if ERROR in levels:
            return "failed"
        return "warn" if WARN in levels else "ok"

    def to_meta(self) -> dict[str, Any]:
        return {"time": time.time(), "state": self.state,
                "findings": [list(f) for f in self.findings]}  # fmt: skip


def free_bytes(folder: str | Path) -> int | None:
    try:
        return psutil.disk_usage(str(folder)).free
    except OSError:
        return None


def job_path(report: DetectionReport | None) -> str:
    """PATH a job runs with: the user's PATH plus folders where software was found."""
    dirs = extra_path_dirs(report)
    return os.pathsep.join(dirs + [os.environ.get("PATH", "")])


def extra_path_dirs(report: DetectionReport | None) -> list[str]:
    if report is None:
        return []
    path_now = set(os.environ.get("PATH", "").split(os.pathsep))
    dirs: list[str] = []
    for inst in report.installations:
        folder = os.path.dirname(inst.path)
        if not inst.missing and folder not in path_now and folder not in dirs:
            dirs.append(folder)
    return dirs


_WRAPPERS = {"nohup", "time", "nice", "env", "exec", "stdbuf"}


def main_program(command: str) -> tuple[str | None, list[str]]:
    """The program a command line runs and its arguments, skipping wrappers.

    ``OMP=4 mpirun -np 8 pmemd.MPI -O …`` → (``pmemd.MPI``, [``-O``, …]).
    """
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return None, []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):
            i += 1
        elif tok == "taskset":
            # taskset -c LIST cmd …  or  taskset MASK cmd …
            i += 3 if i + 1 < len(tokens) and tokens[i + 1] in ("-c", "--cpu-list") else 2
        elif tok in _WRAPPERS:
            i += 1
        elif tok in ("mpirun", "mpiexec", "srun", "charmrun"):
            i += 1
            while i < len(tokens) and tokens[i].startswith(("-", "+")):
                option = tokens[i]
                i += 1
                if option in (
                    "-np",
                    "-n",
                    "--np",
                    "-host",
                    "--host",
                    "-hostfile",
                    "--hostfile",
                    "-machinefile",
                    "-x",
                ) and i < len(tokens):
                    i += 1  # fmt: skip
        else:
            return tok, tokens[i + 1 :]
    return None, []


def _check_program(
    result: CheckResult, command: str, folder: Path, report: DetectionReport | None
) -> None:
    program, args = main_program(command.replace("{cores}", "1").replace("{gpu}", "0"))
    if program is None:
        result.add(ERROR, "The command is empty.")
        return
    if program in ("bash", "sh"):
        script = next((a for a in args if not a.startswith("-")), None)
        if script is None:
            return
        path = Path(script) if os.path.isabs(script) else folder / script
        if not path.is_file():
            result.add(ERROR, f"Script {script} not found in the job folder.")
            return
        syntax = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
        if syntax.returncode != 0:
            message = (syntax.stderr.strip().splitlines() or ["syntax error"])[-1]
            result.add(ERROR, f"{script} has a syntax error: {message}")
        else:
            result.add(OK, f"{script} found, syntax OK.")
        return
    if "/" in program:
        path = Path(program) if os.path.isabs(program) else folder / program
        if not (path.is_file() and os.access(path, os.X_OK)):
            result.add(ERROR, f"Program {program} not found or not executable.")
        else:
            result.add(OK, f"Program {program} found.")
        return
    found = shutil.which(program, path=job_path(report))
    if found:
        result.add(OK, f"{program} found at {found}.")
    else:
        result.add(
            ERROR, f"{program} was not found (not on PATH and not found by the Software scan)."
        )


def _check_resources(result: CheckResult, job: Job, usable_cpus: int, gpus: list[int]) -> None:
    if job.cores < 1:
        result.add(ERROR, "A job needs at least one CPU.")
    elif job.cores > usable_cpus:
        result.add(
            ERROR,
            f"The job asks for {job.cores} CPUs, but the queue may use "
            f"{usable_cpus} (Settings → CPUs for the queue).",
        )
    if job.gpu == "auto" and not gpus:
        result.add(ERROR, "The job asks for a GPU, but no NVIDIA GPU was detected.")
    elif job.gpu not in ("none", "auto"):
        try:
            index = int(job.gpu)
        except ValueError:
            index = -1
        if index not in gpus:
            result.add(ERROR, f"GPU {job.gpu} does not exist on this machine.")


def _check_disk(
    result: CheckResult, folder: Path, min_free_gb: float, needed: int | None = None
) -> None:
    free = free_bytes(folder)
    if free is None:
        return
    result.analysis["disk_free"] = free
    if needed:
        if needed > free:
            result.add(
                ERROR,
                f"The trajectory will need about {fmt.size(needed)}, but only "
                f"{fmt.size(free)} is free on this disk.",
            )
            return
        if needed > 0.8 * free:
            result.add(
                WARN,
                f"The trajectory will need about {fmt.size(needed)} of the "
                f"{fmt.size(free)} free on this disk.",
            )
            return
    if free < min_free_gb * 1024**3:
        result.add(
            ERROR,
            f"Only {fmt.size(free)} free on this disk; the queue starts jobs only "
            f"with at least {min_free_gb:g} GB free (Settings).",
        )
    elif not needed:
        result.add(OK, f"{fmt.size(free)} free on this disk.")


def _have(program: str, report: DetectionReport | None) -> bool:
    return shutil.which(program, path=job_path(report)) is not None


def _check_udb(
    result: CheckResult, job: Job, report: DetectionReport | None, min_free_gb: float
) -> None:
    folder = Path(job.folder)
    script = job.choice or "run_namd.sh"
    pkg = udb.read_package(folder)
    for error in pkg.errors:
        result.add(ERROR, error)
    if script not in pkg.scripts:
        result.add(ERROR, f"{script} not found in the package.")
        return

    stages = pkg.stages(script)
    if script == "run_namd.sh":
        missing = [
            f"{stem}.conf" for stem, _ in udb.STAGES if not (folder / f"{stem}.conf").is_file()
        ]
        if missing:
            result.add(ERROR, "Missing stage files: " + ", ".join(missing))
    elif not (folder / f"{pkg.prefix}_full.namd").is_file():
        result.add(ERROR, f"{pkg.prefix}_full.namd not found.")

    if pkg.solvated:
        atoms = f" ({pkg.atoms:,} atoms)" if pkg.atoms else ""
        result.add(OK, f"Solvated system found{atoms}.")
    else:
        need = ["00_solvate.tleap", "udb_solvate.sh", f"{pkg.prefix}.frcmod"]
        missing = [n for n in need if not (folder / n).is_file()]
        mol2 = [f"{pkg.prefix}_min_gaff.mol2", f"{pkg.prefix}_typed.mol2"]
        if not any((folder / m).is_file() for m in mol2):
            missing.append(mol2[0])
        if missing:
            result.add(
                ERROR, "Not solvated yet, and files tLeap needs are missing: " + ", ".join(missing)
            )
        elif _have("tleap", report):
            result.add(OK, "Not solvated yet: tLeap will solvate it first (tleap found).")
        else:
            result.add(
                ERROR,
                "Not solvated yet, and tleap was not found. Install AmberTools "
                "or add its folder in Software.",
            )

    if pkg.namd_bin:
        if os.path.isfile(pkg.namd_bin) and os.access(pkg.namd_bin, os.X_OK):
            result.add(OK, f"NAMD found at {pkg.namd_bin}.")
        elif shutil.which(pkg.namd_bin, path=job_path(report)):
            result.add(OK, f"NAMD ({pkg.namd_bin}) found on PATH.")
        else:
            result.add(
                ERROR,
                f"The script uses NAMD at {pkg.namd_bin}, which does not exist. "
                "Edit NAMD_BIN at the top of the script.",
            )
    if not _have("python3", report):
        result.add(ERROR, "python3 was not found; the UDB scripts need it.")

    if pkg.gpu and job.gpu == "none":
        result.add(
            ERROR,
            "udb_md.conf says `set gpu 1`, but this job has no GPU. Give the job "
            "a GPU, or set gpu 0 in udb_md.conf.",
        )

    # Where an earlier run stopped.
    done = [s for s in stages if udb.stage_done(pkg, s)]
    interrupted = [s for s in stages if udb.stage_interrupted(pkg, s)]
    if stages and len(done) == len(stages):
        result.add(
            WARN,
            "Every stage has already finished. Running again repeats production "
            "and overwrites its output.",
        )
    elif interrupted:
        plan = udb.resume_plan(pkg, script)
        result.add(WARN, f"An earlier run stopped part-way. {plan.note}")
    elif done:
        result.add(
            OK,
            f"Stages already finished: {', '.join(str(s.number) for s in done)} "
            "(they are skipped).",
        )

    traj = pkg.trajectory_bytes(script)
    result.analysis.update({
        "program": "namd",
        "stages": [{"number": s.number, "title": s.title, "steps": s.steps,
                    "minimization": s.minimization, "log": s.log, "output": s.output,
                    "phase": s.phase} for s in stages],
        "total_steps": pkg.total_steps(script),
        "ns": pkg.md_ns(script),
        "timestep_fs": pkg.timestep_fs,
        "atoms": pkg.atoms,
        "traj_bytes": traj,
        "gpu_mode": pkg.gpu,
        "namd_bin": pkg.namd_bin,
        "prefix": pkg.prefix,
    })  # fmt: skip
    result.add(
        OK,
        f"{len(stages)} stages, {pkg.total_steps(script):,} steps "
        f"({pkg.md_ns(script):g} ns of MD).",
    )
    if traj is None:
        result.add(WARN, "Trajectory size unknown until the system is solvated.")
    _check_disk(result, folder, min_free_gb, traj)


def _check_namd(result: CheckResult, job: Job, min_free_gb: float) -> None:
    folder = Path(job.folder)
    if not job.choice or not (folder / job.choice).is_file():
        result.add(ERROR, f"Configuration file {job.choice} not found.")
        return
    cfg = evaluate_config(folder / job.choice, cwd=folder)
    if cfg.error:
        result.add(ERROR, f"{job.choice} could not be read: {cfg.error}")
    if not cfg.segments:
        result.add(ERROR, f"{job.choice} has no run or minimize command.")
    for key, value in cfg.inputs:
        path = Path(value) if os.path.isabs(value) else folder / value
        if not path.exists():
            level = WARN if job.after_job else ERROR
            note = " (expected from the job this one waits for)" if job.after_job else ""
            result.add(level, f"Input file {value} ({key}) not found{note}.")
    atoms = None
    parm = cfg.params.get("parmfile")
    if parm and (folder / parm).is_file():
        atoms = amber.prmtop_atoms(folder / parm)
    traj = None
    if atoms and cfg.dcd_freq:
        traj = (cfg.total_steps // cfg.dcd_freq) * (atoms * 12 + 80)
    result.analysis.update({
        "program": "namd",
        "stages": [{"number": 1, "title": job.choice, "steps": cfg.total_steps,
                    "minimization": cfg.md_steps == 0}],
        "total_steps": cfg.total_steps,
        "ns": cfg.ns,
        "timestep_fs": cfg.timestep_fs,
        "first_timestep": cfg.first_timestep,
        "atoms": atoms,
        "traj_bytes": traj,
        "gpu_mode": cfg.gpu_resident,
    })  # fmt: skip
    if cfg.segments:
        result.add(OK, f"{cfg.total_steps:,} steps ({cfg.ns:g} ns of MD).")
    _check_disk(result, folder, min_free_gb, traj)


def _check_amber(result: CheckResult, job: Job, min_free_gb: float) -> None:
    folder = Path(job.folder)
    cmd = amber.parse_command(job.command)
    if cmd is None:
        result.add(
            WARN, "No pmemd or sander call found in the command; progress will not be tracked."
        )
        _check_disk(result, folder, min_free_gb)
        return
    for label_, name in (("mdin", cmd.mdin), ("topology", cmd.prmtop), ("coordinates", cmd.inpcrd)):
        path = Path(name) if os.path.isabs(name) else folder / name
        if not path.is_file():
            level = WARN if job.after_job and label_ == "coordinates" else ERROR
            result.add(level, f"{label_.capitalize()} file {name} not found.")
    mdin_path = folder / cmd.mdin
    plan = (
        amber.mdin_plan(amber.parse_mdin(mdin_path.read_text(errors="replace")))
        if mdin_path.is_file()
        else None
    )
    atoms = amber.prmtop_atoms(folder / cmd.prmtop)
    if plan:
        what = "minimisation steps" if plan.minimization else f"steps ({plan.ns:g} ns)"
        result.add(OK, f"{plan.steps:,} {what}.")
        result.analysis.update({
            "program": "amber",
            "stages": [{"number": 1, "title": cmd.mdin, "steps": plan.steps,
                        "minimization": plan.minimization}],
            "total_steps": plan.steps,
            "ns": plan.ns,
            "timestep_fs": plan.dt_ps * 1000,
            "atoms": atoms,
            "mdinfo": cmd.mdinfo,
            "mdout": cmd.mdout,
            "gpu_mode": "cuda" in cmd.program,
        })  # fmt: skip
    _check_disk(result, folder, min_free_gb)


def quick_check(
    job: Job, report: DetectionReport | None, usable_cpus: int, gpus: list[int], min_free_gb: float
) -> CheckResult:
    result = CheckResult()
    folder = Path(job.folder)
    if not folder.is_dir():
        result.add(ERROR, f"The job folder {job.folder} does not exist (moved or renamed?).")
        return result
    if not os.access(folder, os.W_OK):
        result.add(ERROR, "The job folder is not writable.")
    if not job.command.strip():
        result.add(ERROR, "The command is empty.")
        return result
    _check_resources(result, job, usable_cpus, gpus)
    _check_program(result, job.command, folder, report)
    if job.kind == "udb":
        _check_udb(result, job, report, min_free_gb)
    elif job.kind == "namd":
        _check_namd(result, job, min_free_gb)
    elif job.kind == "amber":
        _check_amber(result, job, min_free_gb)
    else:
        result.analysis["program"] = "other"
        _check_disk(result, folder, min_free_gb)
    return result
