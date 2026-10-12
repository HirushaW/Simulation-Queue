"""NAMD: count a configuration's steps, and read progress from a NAMD log."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..tcl import Interp, TclError

# Keywords whose value is a file NAMD reads.
INPUT_KEYS = (
    "structure", "coordinates", "parameters", "parmfile", "ambercoor", "grotopfile",
    "grocoorfile", "bincoordinates", "binvelocities", "velocities", "extendedsystem",
    "consref", "conskfile", "fixedatomsfile", "colvarsconfig", "tclforcesscript",
)  # fmt: skip


@dataclass
class Segment:
    kind: str  # "minimize" or "run"
    steps: int
    label: str = ""  # the last message the script printed before it


@dataclass
class NamdConfig:
    path: Path
    segments: list[Segment] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)  # first value of each keyword
    inputs: list[tuple[str, str]] = field(default_factory=list)  # (keyword, file)
    printed: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def min_steps(self) -> int:
        return sum(s.steps for s in self.segments if s.kind == "minimize")

    @property
    def md_steps(self) -> int:
        return sum(s.steps for s in self.segments if s.kind == "run")

    @property
    def total_steps(self) -> int:
        return self.min_steps + self.md_steps

    def _number(self, key: str, default: float) -> float:
        try:
            return float(self.params.get(key, default))
        except ValueError:
            return default

    @property
    def timestep_fs(self) -> float:
        return self._number("timestep", 1.0)

    @property
    def steps_per_cycle(self) -> int:
        return int(self._number("stepspercycle", 20))

    @property
    def first_timestep(self) -> int:
        return int(self._number("firsttimestep", 0))

    @property
    def output_name(self) -> str | None:
        return self.params.get("outputname")

    @property
    def dcd_freq(self) -> int | None:
        value = int(self._number("dcdfreq", 0))
        return value or None

    @property
    def gpu_resident(self) -> bool:
        for key in ("gpuresident", "cudasoaintegrate"):
            if self.params.get(key, "").lower() in ("on", "yes", "true", "1"):
                return True
        return False

    @property
    def ns(self) -> float:
        return self.md_steps * self.timestep_fs / 1e6


def evaluate_config(
    path: str | Path, cwd: str | Path | None = None, env: dict[str, str] | None = None
) -> NamdConfig:
    """Run a NAMD configuration through the Tcl reader and record what it does.

    ``cwd`` is the folder NAMD would be started in (relative ``source`` and
    input paths are resolved from it); it defaults to the file's folder.
    """
    path = Path(path)
    result = NamdConfig(path=path)

    def keyword(_it: Interp, name: str, args: list[str]) -> str:
        key = name.lower()
        # NAMD ignores a trailing "# comment" on a keyword line.
        values = []
        for arg in args:
            if arg.startswith("#"):
                break
            values.append(arg)
        value = " ".join(values)
        if key == "numsteps":
            result.params.setdefault(key, value)
            return ""
        result.params.setdefault(key, value)
        if key in INPUT_KEYS and value:
            result.inputs.append((key, value))
        return ""

    interp = Interp(cwd=cwd or path.parent, env=env, unknown=keyword)

    def steps_arg(args: list[str]) -> int:
        numbers = [a for a in args if re.fullmatch(r"[+-]?\d+", a.strip())]
        if not numbers:
            raise TclError(f"run/minimize needs a step count, got {args!r}")
        return int(numbers[-1])

    def label() -> str:
        return interp.output[-1] if interp.output else ""

    def run(_it: Interp, args: list[str]) -> str:
        result.segments.append(Segment("run", steps_arg(args), label()))
        return ""

    def minimize(_it: Interp, args: list[str]) -> str:
        result.segments.append(Segment("minimize", steps_arg(args), label()))
        return ""

    def printer(it: Interp, args: list[str]) -> str:
        it.output.append(" ".join(args))
        return ""

    interp.commands.update({"run": run, "minimize": minimize, "print": printer})
    try:
        interp.run_script(path.read_text(encoding="utf-8", errors="replace"))
    except (TclError, OSError) as exc:
        result.error = str(exc)
    except RecursionError:
        result.error = "the configuration nests too deeply to read"

    # Old-style configurations give `numsteps` and no run command.
    if not result.segments and "numsteps" in result.params:
        try:
            n = int(float(result.params["numsteps"]))
        except ValueError:
            n = 0
        minimizing = result.params.get("minimization", "").lower() in ("on", "yes", "true")
        if n > 0:
            result.segments.append(Segment("minimize" if minimizing else "run", n))
    result.printed = list(interp.output)
    return result


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

_ENERGY = re.compile(r"^ENERGY:\s+(\d+)\s+(.*)$", re.MULTILINE)
_TIMING = re.compile(
    r"^TIMING:\s+(\d+)\s+CPU:\s*[\d.]+,\s*[\d.eE+-]+/step\s+Wall:\s*([\d.]+),\s*([\d.eE+-]+)/step"
    r"(?:,\s*([\d.eE+-]+)\s+hours remaining)?",
    re.MULTILINE,
)
_BENCH = re.compile(r"Benchmark time:\s+\d+\s+CPUs\s+([\d.eE+-]+)\s+s/step", re.MULTILINE)
_ATOMS = re.compile(r"^Info:\s+(\d+)\s+ATOMS\s*$", re.MULTILINE)
_WALLCLOCK = re.compile(r"^WallClock:\s+([\d.]+)", re.MULTILINE)
_FATAL = re.compile(r"^(?:FATAL ERROR|ERROR):\s*(.+)$", re.MULTILINE)
_PRINTED_STAGE = re.compile(r"UDB: phase \d+, stage (\d+)/(\d+)")


@dataclass
class NamdLog:
    step: int | None = None  # last step NAMD reported
    first_step: int | None = None
    s_per_step: float | None = None  # wall seconds per step, most recent
    hours_remaining: float | None = None  # NAMD's own estimate for the current run
    atoms: int | None = None
    wallclock_s: float | None = None  # present once NAMD finished normally
    nan: bool = False
    fatal: str | None = None
    stage_marker: int | None = None  # UDB all-in-one: "stage n/5"

    @property
    def finished(self) -> bool:
        return self.wallclock_s is not None


def read_head_tail(path: Path, head: int = 262_144, tail: int = 524_288) -> tuple[str, str]:
    """The start and end of a possibly large text file (both whole if small)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(0)
            if size <= head + tail:
                text = fh.read().decode("utf-8", "replace")
                return text, text
            start = fh.read(head).decode("utf-8", "replace")
            fh.seek(size - tail)
            end = fh.read().decode("utf-8", "replace")
            return start, end.split("\n", 1)[-1]
    except OSError:
        return "", ""


def parse_log(path: str | Path) -> NamdLog:
    head, tail = read_head_tail(Path(path))
    log = NamdLog()
    m = _ATOMS.search(head)
    if m:
        log.atoms = int(m.group(1))

    energies = _ENERGY.findall(tail)
    if energies:
        log.step = int(energies[-1][0])
        recent = " ".join(values for _, values in energies[-3:]).lower()
        log.nan = bool(re.search(r"(?<![a-z])-?nan(?![a-z])", recent))
    first = _ENERGY.search(head)
    if first:
        log.first_step = int(first.group(1))

    timings = _TIMING.findall(tail)
    if timings:
        step, _wall, per_step, remaining = timings[-1]
        log.s_per_step = float(per_step)
        log.step = max(log.step or 0, int(step))
        log.hours_remaining = float(remaining) if remaining else None
    if log.s_per_step is None:
        benches = _BENCH.findall(head + "\n" + tail)
        if benches:
            log.s_per_step = float(benches[-1])

    clocks = _WALLCLOCK.findall(tail)
    if clocks:
        log.wallclock_s = float(clocks[-1])
    fatals = _FATAL.findall(tail)
    if fatals:
        log.fatal = fatals[-1].strip()
    stages = _PRINTED_STAGE.findall(tail)
    if stages:
        log.stage_marker = int(stages[-1][0])
    return log


# Plain-language explanations for common NAMD failures.
_EXPLAIN = (
    ("Periodic cell has become too small",
     "The simulation box shrank too much (unstable pressure control). Restart from the "
     "last good restart files, or equilibrate longer before switching the barostat on."),
    ("moving too fast",
     "The system blew up: atoms moved too fast. Usually bad contacts or too large a "
     "timestep; minimise longer or lower the timestep."),
    ("RATTLE",
     "Bond constraints failed (RATTLE): the structure became unstable. Check the "
     "minimisation, the restraints and the starting coordinates."),
    ("Unable to open", "NAMD could not open a file it needs. Check that the file exists."),
    ("No such file", "NAMD could not find a file it needs. Check that the file exists."),
    ("Margin is too small",
     "NAMD's patch margin is too small for how much the box changed; increase `margin`."),
    ("CUDA", "A GPU (CUDA) error. Check the GPU with nvidia-smi and that the NAMD build "
             "matches the driver."),
    ("Memory allocation failed", "NAMD ran out of memory."),
    ("Out of memory", "NAMD ran out of memory."),
)  # fmt: skip


def explain(fatal: str) -> str:
    for needle, text in _EXPLAIN:
        if needle.lower() in fatal.lower():
            return text
    return ""
