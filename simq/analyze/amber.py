"""AMBER (pmemd / sander): read mdin settings, the command line, and progress."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .namd import read_head_tail

AMBER_PROGRAMS = ("pmemd.cuda", "pmemd.cuda_SPFP", "pmemd.cuda_DPFP", "pmemd.cuda.MPI",
                  "pmemd.MPI", "pmemd", "sander.MPI", "sander")  # fmt: skip


def parse_mdin(text: str) -> dict[str, str]:
    """``key=value`` pairs of the ``&cntrl`` namelist, keys in lower case."""
    m = re.search(r"&cntrl(.*?)(?:^\s*/\s*$|&end)", text, re.IGNORECASE | re.DOTALL | re.MULTILINE)
    if not m:
        return {}
    body = re.sub(r"!.*", "", m.group(1))
    values = {}
    for key, value in re.findall(
        r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*('[^']*'|\"[^\"]*\"|[^,\s]+)", body
    ):
        values[key.lower()] = value.strip("'\"")
    return values


@dataclass
class MdinPlan:
    minimization: bool
    steps: int
    dt_ps: float

    @property
    def ns(self) -> float:
        return 0.0 if self.minimization else self.steps * self.dt_ps / 1000.0


def mdin_plan(values: dict[str, str]) -> MdinPlan:
    def number(key: str, default: float) -> float:
        try:
            return float(values.get(key, default).replace("d", "e").replace("D", "e"))
        except (ValueError, AttributeError):
            return default

    minimization = int(number("imin", 0)) == 1
    steps = int(number("maxcyc", 1)) if minimization else int(number("nstlim", 1))
    return MdinPlan(minimization, steps, number("dt", 0.001))


@dataclass
class AmberCommand:
    program: str
    mdin: str = "mdin"
    mdout: str = "mdout"
    prmtop: str = "prmtop"
    inpcrd: str = "inpcrd"
    mdinfo: str = "mdinfo"
    restrt: str = "restrt"


def parse_command(command: str) -> AmberCommand | None:
    """Find the pmemd/sander call in a shell command and read its file options."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return None
    for k, token in enumerate(tokens):
        name = Path(token).name
        if name in AMBER_PROGRAMS:
            cmd = AmberCommand(program=name)
            options = {"-i": "mdin", "-o": "mdout", "-p": "prmtop", "-c": "inpcrd",
                       "-inf": "mdinfo", "-r": "restrt"}  # fmt: skip
            rest = tokens[k + 1 :]
            for j, opt in enumerate(rest[:-1]):
                if opt in options:
                    setattr(cmd, options[opt], rest[j + 1])
            return cmd
    return None


def prmtop_atoms(path: str | Path) -> int | None:
    """Number of atoms, from the POINTERS section of an AMBER topology."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            in_pointers = False
            for line in fh:
                if line.startswith("%FLAG POINTERS"):
                    in_pointers = True
                    continue
                if in_pointers and line.startswith("%FORMAT"):
                    continue
                if in_pointers:
                    first = line.split()
                    return int(first[0]) if first else None
    except (OSError, ValueError):
        return None
    return None


@dataclass
class AmberProgress:
    step: int | None = None
    s_per_step: float | None = None
    ns_per_day: float | None = None
    remaining_s: float | None = None
    finished: bool = False
    nan: bool = False
    error: str | None = None


_UNITS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}


def parse_mdinfo(text: str) -> AmberProgress:
    p = AmberProgress()
    steps = re.findall(r"NSTEP\s*=\s*(\d+)", text)
    if steps:
        p.step = int(steps[-1])
    per_step = re.findall(r"Per Step\(ms\)\s*=\s*([\d.]+)", text)
    if per_step:
        p.s_per_step = float(per_step[-1]) / 1000.0
    nsday = re.findall(r"ns/day\s*=\s*([\d.]+)", text)
    if nsday:
        p.ns_per_day = float(nsday[-1])
    remaining = re.findall(
        r"Estimated time remaining:\s*([\d.]+)\s*(seconds|minutes|hours|days)", text
    )
    if remaining:
        value, unit = remaining[-1]
        p.remaining_s = float(value) * _UNITS[unit]
    return p


def parse_mdout(path: str | Path) -> AmberProgress:
    _head, tail = read_head_tail(Path(path))
    p = parse_mdinfo(tail)
    p.finished = "Total wall time" in tail or "Final Performance Info" in tail
    recent = "\n".join(re.findall(r"EPtot\s*=.*", tail)[-3:])
    p.nan = bool(re.search(r"NaN|\*{5,}", recent))
    errors = re.findall(
        r"^\s*(?:ERROR|Error|SANDER BOMB|vlimit exceeded)[^\n]*", tail, re.MULTILINE
    )
    if errors:
        p.error = errors[-1].strip()
    return p
