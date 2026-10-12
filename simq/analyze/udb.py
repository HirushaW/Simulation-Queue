"""UDB packages: the folders UDB generates for a NAMD pipeline.

A package holds ``udb_md.conf`` (every setting, as Tcl), the five stage
configurations ``01_minimize.conf`` … ``05_production.conf``, the all-in-one
``<prefix>_full.namd``, and two launchers: ``run_namd.sh`` (stage by stage)
and ``run_full.sh`` (two NAMD invocations, "phases").
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..tcl import Interp, TclError
from .amber import prmtop_atoms
from .namd import evaluate_config

SCRIPTS = ("run_namd.sh", "run_full.sh")

# (file stem / outputName, title)
STAGES = (
    ("01_minimize", "Minimisation"),
    ("02_heat", "Heating"),
    ("03_nvt_equil", "NVT equilibration"),
    ("04_npt_equil", "NPT equilibration"),
    ("05_production", "Production"),
)

# One DCD frame: 12 bytes per atom plus record markers and the unit cell.
_FRAME_OVERHEAD = 80


@dataclass
class Stage:
    number: int  # 1..5
    title: str
    steps: int
    minimization: bool
    output: str  # NAMD outputName (staged) or the phase's outputName (all-in-one)
    log: str  # log file, relative to the package folder
    dcd_freq: int | None
    phase: int | None = None  # all-in-one only


@dataclass
class UdbPackage:
    folder: Path
    prefix: str = ""
    settings: dict[str, str] = field(default_factory=dict)
    scripts: list[str] = field(default_factory=list)
    staged: list[Stage] = field(default_factory=list)
    full: list[Stage] = field(default_factory=list)
    atoms: int | None = None
    namd_bin: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def gpu(self) -> bool:
        return self.settings.get("gpu", "0").strip() == "1"

    @property
    def timestep_fs(self) -> float:
        try:
            return float(self.settings.get("timestep_fs", "2.0"))
        except ValueError:
            return 2.0

    @property
    def solvated(self) -> bool:
        return (self.folder / f"{self.prefix}_solvated.prmtop").is_file() and (
            self.folder / f"{self.prefix}_solvated.inpcrd"
        ).is_file()

    def stages(self, script: str) -> list[Stage]:
        return self.full if script == "run_full.sh" else self.staged

    def total_steps(self, script: str) -> int:
        return sum(s.steps for s in self.stages(script))

    def md_ns(self, script: str) -> float:
        md = sum(s.steps for s in self.stages(script) if not s.minimization)
        return md * self.timestep_fs / 1e6

    def trajectory_bytes(self, script: str) -> int | None:
        """Expected size of all DCD files the run writes (needs the atom count)."""
        if not self.atoms:
            return None
        frames = 0
        for stage in self.stages(script):
            if stage.dcd_freq:
                frames += stage.steps // stage.dcd_freq
        return frames * (self.atoms * 12 + _FRAME_OVERHEAD)


def is_package(folder: str | Path) -> bool:
    folder = Path(folder)
    return (folder / "udb_md.conf").is_file() and any((folder / s).is_file() for s in SCRIPTS)


def read_settings(folder: Path) -> dict[str, str]:
    interp = Interp(cwd=folder)
    interp.source("udb_md.conf")
    return {k: v for k, v in interp.globals.items() if isinstance(v, str)}


def _namd_bin_from_script(text: str) -> str | None:
    m = re.search(r'^\s*NAMD_BIN="\$\{NAMD_BIN:-([^}]+)\}"', text, re.MULTILINE)
    if not m:
        m = re.search(r'^\s*NAMD_BIN="([^"$]+)"', text, re.MULTILINE)
    if not m:
        return None
    return os.path.expanduser(m.group(1).replace("$HOME", "~"))


def _udb_mult(n: int, m: int) -> int:
    r = (n // m) * m
    return max(r, m)


def read_package(folder: str | Path) -> UdbPackage:
    folder = Path(folder)
    pkg = UdbPackage(folder=folder)
    pkg.scripts = [s for s in SCRIPTS if (folder / s).is_file()]
    try:
        pkg.settings = read_settings(folder)
    except TclError as exc:
        pkg.errors.append(f"udb_md.conf could not be read: {exc}")
        return pkg
    pkg.prefix = pkg.settings.get("prefix", "")

    for script in pkg.scripts:
        text = (folder / script).read_text(encoding="utf-8", errors="replace")
        pkg.namd_bin = pkg.namd_bin or _namd_bin_from_script(text)

    # Stage by stage: one NAMD run per configuration file.
    for number, (stem, title) in enumerate(STAGES, start=1):
        conf = folder / f"{stem}.conf"
        if not conf.is_file():
            continue
        cfg = evaluate_config(conf, cwd=folder)
        if cfg.error:
            pkg.errors.append(f"{conf.name}: {cfg.error}")
        pkg.staged.append(
            Stage(
                number,
                title,
                cfg.total_steps,
                cfg.min_steps > 0 and cfg.md_steps == 0,
                cfg.output_name or stem,
                f"logs/{stem}.log",
                cfg.dcd_freq,
            )
        )

    # All-in-one: two invocations of <prefix>_full.namd, UDB_PHASE=1 and 2.
    full = folder / f"{pkg.prefix}_full.namd"
    if full.is_file():
        spc = int(float(pkg.settings.get("stepspercycle", "20") or 20))
        try:
            dcd = _udb_mult(int(float(pkg.settings.get("traj_freq", "1000"))), spc)
        except ValueError:
            dcd = None
        by_stage: dict[int, Stage] = {}
        for phase in (1, 2):
            cfg = evaluate_config(full, cwd=folder, env={"UDB_PHASE": str(phase)})
            if cfg.error:
                pkg.errors.append(f"{full.name} (phase {phase}): {cfg.error}")
            for seg in cfg.segments:
                m = re.search(r"stage (\d+)/\d+", seg.label)
                number = int(m.group(1)) if m else (1 if phase == 1 else 5)
                stage = by_stage.get(number)
                if stage is None:
                    title = STAGES[number - 1][1] if 1 <= number <= 5 else f"Stage {number}"
                    stage = Stage(
                        number,
                        title,
                        0,
                        seg.kind == "minimize",
                        cfg.output_name or f"{pkg.prefix}_full_p{phase}",
                        f"logs/{pkg.prefix}_full_p{phase}.log",
                        dcd,
                        phase,
                    )
                    by_stage[number] = stage
                stage.steps += seg.steps
        pkg.full = [by_stage[k] for k in sorted(by_stage)]

    prmtop = folder / f"{pkg.prefix}_solvated.prmtop"
    if prmtop.is_file():
        pkg.atoms = prmtop_atoms(prmtop)
    return pkg


# ---------------------------------------------------------------------------
# Where a run stopped, and how to continue it
# ---------------------------------------------------------------------------


def stage_done(pkg: UdbPackage, stage: Stage) -> bool:
    """NAMD writes ``<outputName>.coor`` only when a run ends normally."""
    return (pkg.folder / f"{stage.output}.coor").is_file()


def stage_interrupted(pkg: UdbPackage, stage: Stage) -> bool:
    """Restart files but no final output: NAMD was stopped part-way through."""
    return not stage_done(pkg, stage) and (pkg.folder / f"{stage.output}.restart.coor").is_file()


@dataclass
class ResumePlan:
    args: str  # extra arguments for the launcher ("", "restart", "resume")
    move_aside: list[Path]  # partial restart files to move out of the way first
    note: str


def resume_plan(pkg: UdbPackage, script: str) -> ResumePlan:
    """How to continue an interrupted run without redoing finished stages."""
    if script == "run_full.sh":
        p1 = pkg.folder / f"{pkg.prefix}_full_p1.coor"
        p2_restart = pkg.folder / f"{pkg.prefix}_full_p2.restart.coor"
        p2_done = pkg.folder / f"{pkg.prefix}_full_p2.coor"
        if p2_restart.is_file() and not p2_done.is_file() and p1.is_file():
            return ResumePlan(
                "resume",
                [],
                "Phase 2 was interrupted: continuing production from its last restart files "
                "(run_full.sh resume).",
            )
        return ResumePlan("", [], "Starting run_full.sh again from the beginning.")

    stages = pkg.staged
    if (
        stages
        and stage_interrupted(pkg, stages[-1])
        and all(stage_done(pkg, s) for s in stages[:-1])
        and (pkg.folder / "05_production_restart.conf").is_file()
    ):
        return ResumePlan(
            "restart",
            [],
            "Production was interrupted: continuing it from its last restart files "
            "(run_namd.sh restart).",
        )
    for stage in stages:
        if stage_interrupted(pkg, stage):
            # run_namd.sh treats restart files as "stage finished", so it would skip
            # this half-run stage. Moving them aside makes it redo the stage.
            files = sorted(pkg.folder.glob(f"{stage.output}.restart.*"))
            return ResumePlan(
                "",
                files,
                f"Stage {stage.number} ({stage.title}) was interrupted. Its partial restart "
                "files were moved aside so the stage is redone from its start; finished "
                "stages are skipped.",
            )
    return ResumePlan("", [], "Running run_namd.sh again; finished stages are skipped.")


def move_aside(paths: list[Path]) -> list[Path]:
    """Rename files to ``<name>.simq-interrupted-<time>``; return the new paths."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for path in paths:
        target = path.with_name(f"{path.name}.simq-interrupted-{stamp}")
        path.rename(target)
        moved.append(target)
    return moved


def command_for(script: str, args: str = "") -> str:
    """The launcher command line; ``{cores}`` is filled in by the queue."""
    if script == "run_full.sh":
        return f"bash run_full.sh {args}".strip()
    if args == "restart":
        return "bash run_namd.sh restart {cores}"
    return "bash run_namd.sh {cores}"
