"""Look at a simulation folder and propose how to run it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..detect.software import DetectionReport
from . import udb
from .amber import parse_mdin
from .namd import evaluate_config

KINDS = ("udb", "namd", "amber", "command")
KIND_LABELS = {
    "udb": "UDB package",
    "namd": "NAMD config",
    "amber": "AMBER",
    "command": "Any command",
}

_NAMD_HINT = re.compile(r"^\s*(run|minimize|numsteps)\s", re.IGNORECASE | re.MULTILINE)
_NAMD_INPUT = re.compile(r"^\s*(structure|parmfile|coordinates|ambercoor|grotopfile)\s",
                         re.IGNORECASE | re.MULTILINE)  # fmt: skip


@dataclass
class FolderGuess:
    kind: str
    summary: str
    # Files the user may choose between: launch scripts, configs or mdin files.
    choices: dict[str, list[str]] = field(default_factory=dict)  # kind -> files


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def namd_configs(folder: Path) -> list[str]:
    found = []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() in (".conf", ".namd", ".inp", ".in") and path.is_file():
            text = _read(path)
            if _NAMD_HINT.search(text) and (_NAMD_INPUT.search(text) or "source" in text):
                found.append(path.name)
    return found


def amber_inputs(folder: Path) -> list[str]:
    found = []
    for path in sorted(folder.iterdir()):
        if (
            path.is_file()
            and path.suffix.lower() in (".in", ".mdin", ".inp", "")
            and path.stat().st_size < 200_000
            and re.search(r"&cntrl", _read(path), re.I)
        ):
            found.append(path.name)
    return found


def shell_scripts(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.glob("*.sh") if p.is_file())


def inspect_folder(folder: str | Path) -> FolderGuess:
    folder = Path(folder)
    if not folder.is_dir():
        return FolderGuess("command", "This folder does not exist.")
    choices = {
        "udb": [s for s in udb.SCRIPTS if (folder / s).is_file()] if udb.is_package(folder) else [],
        "namd": namd_configs(folder),
        "amber": amber_inputs(folder),
        "command": shell_scripts(folder),
    }
    if choices["udb"]:
        pkg = udb.read_package(folder)
        script = choices["udb"][0]
        mode = "GPU" if pkg.gpu else "CPU"
        summary = (
            f"UDB package · {pkg.prefix or folder.name} · {len(pkg.stages(script))} stages · "
            f"{pkg.md_ns(script):g} ns of MD · {mode} mode (set gpu in udb_md.conf)"
        )
        return FolderGuess("udb", summary, choices)
    if choices["namd"]:
        cfg = evaluate_config(folder / choices["namd"][0])
        summary = f"NAMD configuration {choices['namd'][0]} · {cfg.total_steps:,} steps"
        if cfg.md_steps:
            summary += f" · {cfg.ns:g} ns"
        return FolderGuess("namd", summary, choices)
    if choices["amber"]:
        return FolderGuess("amber", f"AMBER input {choices['amber'][0]}", choices)
    if choices["command"]:
        return FolderGuess("command", f"Shell script {choices['command'][0]}", choices)
    return FolderGuess(
        "command", "No known simulation input found: type the command to run.", choices
    )


# ---------------------------------------------------------------------------
# Default commands
# ---------------------------------------------------------------------------


def _program(report: DetectionReport | None, *names: str) -> str | None:
    if report is None:
        return None
    for name in names:
        for inst in report.installations:
            if inst.name == name and not inst.missing:
                return inst.path
    return None


def namd_program(report: DetectionReport | None) -> str:
    return _program(report, "namd3", "namd2") or "namd3"


def namd_is_cuda(report: DetectionReport | None, path: str | None) -> bool | None:
    """Whether the NAMD at ``path`` (or the default one) is a GPU build."""
    if report is None:
        return None
    for inst in report.installations:
        if inst.name.startswith("namd") and (
            path is None or inst.path == path or inst.realpath == path
        ):
            return inst.gpu
    return None


def _pick(folder: Path, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        files = sorted(folder.glob(pattern))
        if files:
            return files[0].name
    return None


def default_command(
    kind: str, folder: str | Path, choice: str | None, gpu: bool, report: DetectionReport | None
) -> str:
    folder = Path(folder)
    if kind == "udb" and choice:
        return udb.command_for(choice)
    if kind == "namd" and choice:
        stem = Path(choice).stem
        namd = namd_program(report)
        cfg = evaluate_config(folder / choice)
        if gpu and cfg.gpu_resident:
            # GPU-resident NAMD 3 wants one PE per GPU; more cores make it slower.
            return f"{namd} +p1 +setcpuaffinity +devices {{gpu}} {choice} > {stem}.log 2>&1"
        devices = " +devices {gpu}" if gpu else ""
        return f"{namd} +p{{cores}}{devices} {choice} > {stem}.log 2>&1"
    if kind == "amber" and choice:
        stem = Path(choice).stem
        prmtop = _pick(folder, ("*.prmtop", "*.parm7", "*.top")) or "prmtop"
        inpcrd = _pick(folder, ("*.rst7", "*.inpcrd", "*.ncrst", "*.crd", "*.rst")) or "inpcrd"
        files = (f"-O -i {choice} -p {prmtop} -c {inpcrd} -o {stem}.out -r {stem}.rst7 "
                 f"-x {stem}.nc -inf {stem}.mdinfo")  # fmt: skip
        if gpu:
            return f"{_program(report, 'pmemd.cuda') or 'pmemd.cuda'} {files}"
        mpi = _program(report, "pmemd.MPI")
        if mpi:
            return f"mpirun -np {{cores}} {mpi} {files}"
        return f"{_program(report, 'pmemd', 'sander') or 'sander'} {files}"
    if kind == "command" and choice:
        return f"bash {choice}"
    return ""


def default_resources(
    kind: str, folder: str | Path, choice: str | None, report: DetectionReport | None, budget: int
) -> tuple[int, str, str]:
    """(cores, gpu, reason): sensible defaults for a new job."""
    folder = Path(folder)
    if kind == "udb" and choice:
        pkg = udb.read_package(folder)
        cuda = namd_is_cuda(report, pkg.namd_bin)
        if pkg.gpu:
            if choice == "run_full.sh":
                return (
                    2,
                    "auto",
                    "udb_md.conf has gpu 1: run_full.sh runs everything with +p1 on the "
                    "GPU, so 2 CPUs are plenty.",
                )
            return (
                min(8, budget),
                "auto",
                "udb_md.conf has gpu 1: run_namd.sh minimises on the CPU with these "
                "cores, then runs dynamics on the GPU with +p1.",
            )
        if cuda:
            return (
                budget,
                "auto",
                "udb_md.conf has gpu 0, but this NAMD is a CUDA build: it still uses the "
                "GPU, so the queue reserves one.",
            )
        return (budget, "none", "udb_md.conf has gpu 0: a CPU run on all queue CPUs.")
    if kind == "namd" and choice:
        cfg = evaluate_config(folder / choice)
        if cfg.gpu_resident:
            return (1, "auto", "GPU-resident configuration: one CPU and one GPU.")
        if namd_is_cuda(report, None):
            return (budget, "auto", "This NAMD is a CUDA build, so the queue reserves a GPU.")
        return (budget, "none", "")
    if kind == "amber" and choice:
        if _program(report, "pmemd.cuda"):
            return (1, "auto", "pmemd.cuda runs on one GPU and one CPU.")
        return (min(16, budget), "none", "")
    return (min(4, budget), "none", "")


def amber_plan_from(folder: Path, mdin: str):
    from .amber import mdin_plan

    return mdin_plan(parse_mdin(_read(folder / mdin)))
