"""Find simulation software installed on this machine.

The search looks in ``PATH``, in the usual install locations (``/opt``,
``/usr/local``, ``~/.local/bin`` …), one or two folders deep under them (so an
unpacked ``~/NAMD_3.0.2_Linux-x86_64-multicore-CUDA/namd3`` or an
``/opt/amber24/bin`` is found without being on ``PATH``), in folders the user
added, and at program paths the user added by hand.

Versions are read by running the program with a harmless flag
(``--version``, ``-h``) under a timeout.  Programs that have no such flag, or
that would wait for input (``tleap``, ``pmemd``), are never run; their version
comes from the install folder's name instead (``/opt/amber24`` → 24).
Results are cached by file size and modification time, so a rescan only runs
programs that are new or have changed.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..paths import data_dir, read_json, write_json

# ---------------------------------------------------------------------------
# What to look for
# ---------------------------------------------------------------------------

GROUP_ORDER = (
    "NAMD",
    "AMBER",
    "GROMACS",
    "LAMMPS",
    "Quantum chemistry & DFT",
    "Tools",
    "NVIDIA",
    "Added by you",
)

# (version, build, gpu) -- each None when unknown
ParseResult = tuple[str | None, str | None, bool | None]


@dataclass(frozen=True)
class ProgramSpec:
    name: str  # executable file name
    group: str
    version_args: tuple[str, ...] | None = None  # None: never run it
    stdin: str | None = None
    parse: Callable[[str], ParseResult] | None = None
    gpu: bool | None = None  # known from the name alone, e.g. pmemd.cuda
    require_elf: bool = False  # e.g. ORCA vs the GNOME "orca" screen reader


def _first_line(text: str) -> ParseResult:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:80], None, None
    return None, None, None


def _regex(pattern: str) -> Callable[[str], ParseResult]:
    rx = re.compile(pattern, re.IGNORECASE)

    def parse(text: str) -> ParseResult:
        m = rx.search(text)
        return (m.group(1).strip(), None, None) if m else (None, None, None)

    return parse


_NAMD_BANNER = re.compile(r"NAMD\s+(\d+\.\d+(?:\.\d+)?[a-z]*\d*)\s+for\s+(\S+)", re.IGNORECASE)
_NAMD_DIR = re.compile(r"NAMD_(\d+\.\d+(?:\.\d+)?[a-z]*\d*)_([^/\s]+)", re.IGNORECASE)


def _namd_build(platform: str) -> tuple[str, bool]:
    """``Linux-x86_64-multicore-CUDA`` → (``multicore-CUDA``, True)."""
    build = re.sub(r"^Linux-[^-]+-", "", platform)
    gpu = bool(re.search(r"CUDA|HIP|SYCL", build, re.IGNORECASE))
    return build, gpu


def parse_namd(text: str) -> ParseResult:
    m = _NAMD_BANNER.search(text)
    if not m:
        return None, None, None
    build, gpu = _namd_build(m.group(2))
    return m.group(1), build, gpu


def parse_gromacs(text: str) -> ParseResult:
    version = re.search(r"GROMACS version:\s*(\S+)", text)
    if not version:
        return None, None, None
    gpu_line = re.search(r"GPU support:\s*(.+)", text)
    mpi_line = re.search(r"MPI library:\s*(.+)", text)
    gpu_support = gpu_line.group(1).strip() if gpu_line else ""
    gpu = None if not gpu_line else gpu_support.lower() not in ("disabled", "none", "")
    parts = [gpu_support if gpu else "CPU"]
    if mpi_line and mpi_line.group(1).strip().upper() == "MPI":
        parts.append("MPI")
    return version.group(1), ", ".join(parts), gpu


def parse_driver(text: str) -> ParseResult:
    version = _first_line(text)[0]
    if version is None or not re.fullmatch(r"[\d.]+", version):
        return None, None, None
    return f"driver {version}", None, None


def parse_nvcc(text: str) -> ParseResult:
    m = re.search(r"release\s+([\d.]+)", text)
    return (m.group(1), None, None) if m else (None, None, None)


_AMBER_DIR = re.compile(r"amber(tools)?[_-]?(\d{2})(?!\d)", re.IGNORECASE)


def version_from_path(name: str, path: str) -> ParseResult:
    """Fallback when running the program is not possible or told us nothing."""
    m = _NAMD_DIR.search(path)
    if m and name.startswith(("namd", "charmrun")):
        build, gpu = _namd_build(m.group(2))
        # charmrun only launches NAMD; whether *it* is a GPU build is meaningless.
        return m.group(1), build, gpu if name.startswith("namd") else None
    m = _AMBER_DIR.search(path)
    if m:
        return ("AmberTools " if m.group(1) else "Amber ") + m.group(2), None, None
    return None, None, None


def _amber(name: str, gpu: bool | None = None) -> ProgramSpec:
    return ProgramSpec(name, "AMBER", gpu=gpu)


def _path_only(name: str, group: str, **kw) -> ProgramSpec:
    return ProgramSpec(name, group, **kw)


_GMX = {"version_args": ("--version",), "parse": parse_gromacs}
_LMP = {"version_args": ("-h",), "parse": _regex(r"Simulator\s+-\s+([^\n]+)")}
_CP2K = {"version_args": ("--version",), "parse": _regex(r"CP2K version\s+(\S+)")}

CATALOG: tuple[ProgramSpec, ...] = (
    # NAMD prints its banner ("NAMD 3.0.2 for Linux-x86_64-multicore-CUDA")
    # before it even looks at its arguments.
    ProgramSpec("namd3", "NAMD", ("--version",), parse=parse_namd),
    ProgramSpec("namd2", "NAMD", ("--version",), parse=parse_namd),
    _path_only("charmrun", "NAMD"),
    _amber("pmemd.cuda", gpu=True),
    _amber("pmemd.cuda_SPFP", gpu=True),
    _amber("pmemd.cuda_DPFP", gpu=True),
    _amber("pmemd.cuda.MPI", gpu=True),
    _amber("pmemd.MPI", gpu=False),
    _amber("pmemd", gpu=False),
    _amber("sander", gpu=False),
    _amber("sander.MPI", gpu=False),
    _amber("tleap"),
    _amber("antechamber"),
    _amber("parmchk2"),
    ProgramSpec("cpptraj", "AMBER", ("--version",), parse=_regex(r"Version\s+V?(\S+)")),
    ProgramSpec("gmx", "GROMACS", **_GMX),
    ProgramSpec("gmx_mpi", "GROMACS", **_GMX),
    ProgramSpec("gmx_d", "GROMACS", **_GMX),
    ProgramSpec("lmp", "LAMMPS", **_LMP),
    ProgramSpec("lmp_serial", "LAMMPS", **_LMP),
    ProgramSpec("lmp_mpi", "LAMMPS", **_LMP),
    _path_only("orca", "Quantum chemistry & DFT", require_elf=True),
    _path_only("g16", "Quantum chemistry & DFT"),
    _path_only("g09", "Quantum chemistry & DFT"),
    ProgramSpec("cp2k.psmp", "Quantum chemistry & DFT", **_CP2K),
    ProgramSpec("cp2k.popt", "Quantum chemistry & DFT", **_CP2K),
    ProgramSpec("cp2k.ssmp", "Quantum chemistry & DFT", **_CP2K),
    _path_only("pw.x", "Quantum chemistry & DFT"),
    _path_only("vasp_std", "Quantum chemistry & DFT"),
    _path_only("vasp_gam", "Quantum chemistry & DFT"),
    _path_only("vasp_ncl", "Quantum chemistry & DFT"),
    _path_only("vmd", "Tools"),
    _path_only("packmol", "Tools"),
    ProgramSpec("obabel", "Tools", ("-V",), parse=_regex(r"(Open Babel\s+[\d.]+)")),
    ProgramSpec("mpirun", "Tools", ("--version",), parse=_first_line),
    ProgramSpec("python3", "Tools", ("--version",), parse=_regex(r"Python\s+(\S+)")),
    ProgramSpec("tclsh", "Tools", (), stdin="puts [info patchlevel]\n", parse=_first_line),
    ProgramSpec(
        "nvidia-smi",
        "NVIDIA",
        ("--query-gpu=driver_version", "--format=csv,noheader"),
        parse=parse_driver,
    ),
    ProgramSpec("nvcc", "NVIDIA", ("--version",), parse=parse_nvcc),
)

SPECS_BY_NAME: dict[str, ProgramSpec] = {spec.name: spec for spec in CATALOG}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Installation:
    name: str
    group: str
    path: str
    realpath: str
    found_via: str  # "PATH", "scan", "your folder", "added by you", "AMBERHOME" …
    version: str | None = None
    build: str | None = None
    gpu: bool | None = None
    missing: bool = False  # a program added by hand whose file has gone


@dataclass
class DetectionReport:
    installations: list[Installation] = field(default_factory=list)
    folders_searched: int = 0
    seconds: float = 0.0
    amber_home: str | None = None
    amber_sh: str | None = None

    def by_group(self) -> dict[str, list[Installation]]:
        groups: dict[str, list[Installation]] = {g: [] for g in GROUP_ORDER}
        for inst in self.installations:
            groups.setdefault(inst.group, []).append(inst)
        return groups

    def to_dict(self) -> dict:
        return {
            "installations": [asdict(i) for i in self.installations],
            "folders_searched": self.folders_searched,
            "seconds": self.seconds,
            "amber_home": self.amber_home,
            "amber_sh": self.amber_sh,
        }

    @classmethod
    def from_dict(cls, raw: Mapping) -> DetectionReport:
        fields = Installation.__dataclass_fields__
        return cls(
            installations=[
                Installation(**{k: v for k, v in item.items() if k in fields})
                for item in raw.get("installations", [])
                if isinstance(item, dict)
            ],
            folders_searched=int(raw.get("folders_searched", 0)),
            seconds=float(raw.get("seconds", 0.0)),
            amber_home=raw.get("amber_home"),
            amber_sh=raw.get("amber_sh"),
        )


# ---------------------------------------------------------------------------
# Where to look
# ---------------------------------------------------------------------------

_MAX_CHILDREN = 400  # don't crawl a folder holding thousands of entries


def _subdirs(path: Path) -> list[Path]:
    try:
        with os.scandir(path) as it:
            dirs = [
                Path(e.path)
                for e in it
                if not e.name.startswith(".") and e.is_dir(follow_symlinks=True)
            ]
    except OSError:
        return []
    dirs.sort()
    return dirs[:_MAX_CHILDREN]


def _scan(base: Path, deep: bool) -> Iterator[Path]:
    """``base/*`` and ``base/*/bin``; with ``deep`` also ``base/*/*/bin``."""
    for child in _subdirs(base):
        yield child
        yield child / "bin"
        if deep:
            for grandchild in _subdirs(child):
                if grandchild.name != "bin":
                    yield grandchild / "bin"


def search_dirs(
    env: Mapping[str, str], home: Path, extra: Iterable[str] = ()
) -> list[tuple[Path, str]]:
    """Folders to look in, most authoritative first, each with how it was found."""
    out: list[tuple[Path, str]] = []

    for entry in env.get("PATH", "").split(os.pathsep):
        if entry:
            out.append((Path(entry), "PATH"))

    for var, sub in (
        ("AMBERHOME", "bin"),
        ("GMXBIN", ""),
        ("CUDA_HOME", "bin"),
        ("NAMD_HOME", ""),
        ("NAMDHOME", ""),
    ):
        if env.get(var):
            out.append((Path(env[var]) / sub if sub else Path(env[var]), var))

    for fixed in (
        home / ".local/bin",
        home / "bin",
        Path("/usr/local/bin"),
        Path("/usr/local/cuda/bin"),
        Path("/opt/bin"),
    ):
        out.append((fixed, "standard folder"))

    for base in (Path("/opt"), Path("/usr/local"), Path("/apps"), Path("/software")):
        out.extend((d, "scan") for d in _scan(base, deep=True))
    for name in ("", "software", "Software", "programs", "apps", "opt", "tools", "local"):
        out.extend((d, "scan") for d in _scan(home / name if name else home, deep=False))

    for folder in extra:
        root = Path(folder).expanduser()
        out.append((root, "your folder"))
        out.append((root / "bin", "your folder"))
        out.extend((d, "your folder") for d in _scan(root, deep=False))

    seen: set[str] = set()
    unique = []
    for path, via in out:
        key = os.path.normpath(str(path))
        if key not in seen:
            seen.add(key)
            unique.append((path, via))
    return unique


# ---------------------------------------------------------------------------
# Running a program to ask its version
# ---------------------------------------------------------------------------


def _is_elf(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def run_probe(path: str, args: tuple[str, ...], stdin: str | None, timeout: float, cwd: str) -> str:
    """Run ``path args`` and return its combined output ('' on failure).

    The program gets its own process group, so on timeout everything it
    started (MPI launchers, Charm++ helpers) is killed too.
    """
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        proc = subprocess.Popen(
            [path, *args],
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError:
        return ""
    try:
        out, _ = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            out, _ = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            out = ""
    return (out or "")[:200_000]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    return data_dir() / "software.json"


def _read_cache() -> dict:
    raw = read_json(_cache_path(), {})
    return raw if isinstance(raw, dict) else {}


def _fingerprint(path: str) -> list[float] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime]


@dataclass
class _Found:
    spec: ProgramSpec
    path: str
    realpath: str
    via: str


def _find(dirs: list[tuple[Path, str]], specs: Mapping[str, ProgramSpec]) -> list[_Found]:
    wanted = set(specs)
    found: list[_Found] = []
    seen_real: set[str] = set()
    for folder, via in dirs:
        try:
            with os.scandir(folder) as it:
                entries = [e for e in it if e.name in wanted]
        except OSError:
            continue
        for entry in sorted(entries, key=lambda e: e.name):
            path = entry.path
            if not (os.path.isfile(path) and os.access(path, os.X_OK)):
                continue
            spec = specs[entry.name]
            real = os.path.realpath(path)
            if spec.require_elf and not _is_elf(real):
                continue
            if real in seen_real:
                continue
            seen_real.add(real)
            found.append(_Found(spec, path, real, via))
    return found


def _custom_spec(path: str) -> ProgramSpec:
    known = SPECS_BY_NAME.get(os.path.basename(path))
    return known if known is not None else ProgramSpec(os.path.basename(path), "Added by you")


def detect_software(
    extra_dirs: Iterable[str] = (),
    manual_programs: Iterable[str] = (),
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    probe_timeout: float = 15.0,
    use_cache: bool = True,
) -> DetectionReport:
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    started = time.monotonic()

    dirs = search_dirs(env, home, extra_dirs)
    found = _find(dirs, SPECS_BY_NAME)

    report = DetectionReport(folders_searched=len(dirs))
    seen_real = {f.realpath for f in found}
    for raw in manual_programs:
        path = os.path.expanduser(raw)
        real = os.path.realpath(path)
        if real in seen_real:
            continue
        seen_real.add(real)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            found.append(_Found(_custom_spec(path), path, real, "added by you"))
        else:
            report.installations.append(
                Installation(
                    os.path.basename(path), "Added by you", path, real, "added by you", missing=True
                )
            )

    cache = _read_cache().get("probes", {}) if use_cache else {}
    if not isinstance(cache, dict):
        cache = {}
    new_cache: dict = {}

    def identify(item: _Found) -> Installation:
        spec = item.spec
        fp = _fingerprint(item.realpath)
        cached = cache.get(item.realpath)
        if isinstance(cached, dict) and fp is not None and cached.get("fp") == fp:
            version, build, gpu = cached.get("version"), cached.get("build"), cached.get("gpu")
        else:
            version = build = gpu = None
            if spec.version_args is not None and spec.parse is not None:
                with tempfile.TemporaryDirectory(prefix="simq-probe-") as tmp:
                    out = run_probe(item.path, spec.version_args, spec.stdin, probe_timeout, tmp)
                version, build, gpu = spec.parse(out)
            if version is None:
                version, path_build, path_gpu = version_from_path(spec.name, item.realpath)
                build = build or path_build
                gpu = gpu if gpu is not None else path_gpu
        # A program that was run but told us nothing (it timed out, say, while the
        # GPU was busy) is not cached, so the next scan asks it again.
        asked_in_vain = version is None and spec.version_args is not None
        if fp is not None and not asked_in_vain:
            new_cache[item.realpath] = {"fp": fp, "version": version, "build": build, "gpu": gpu}
        return Installation(
            name=spec.name,
            group=spec.group,
            path=item.path,
            realpath=item.realpath,
            found_via=item.via,
            version=version,
            build=build,
            gpu=spec.gpu if spec.gpu is not None else gpu,
        )

    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="simq-probe") as pool:
        report.installations = list(pool.map(identify, found)) + report.installations

    report.amber_home, report.amber_sh = _amber_environment(env, report.installations)
    report.seconds = time.monotonic() - started

    if use_cache:
        write_json(_cache_path(), {"probes": new_cache, "last_report": report.to_dict()})
    return report


def _amber_environment(
    env: Mapping[str, str], installs: list[Installation]
) -> tuple[str | None, str | None]:
    """Locate AMBERHOME and ``amber.sh`` (which jobs must source before running)."""
    candidates = []
    if env.get("AMBERHOME"):
        candidates.append(Path(env["AMBERHOME"]))
    for inst in installs:
        if inst.group == "AMBER" and not inst.missing:
            candidates.append(Path(inst.realpath).parent.parent)
    for home in candidates:
        script = home / "amber.sh"
        if script.is_file():
            return str(home), str(script)
    return (str(candidates[0]), None) if candidates else (None, None)


def last_report() -> DetectionReport | None:
    """The report saved by the previous scan, for showing results instantly."""
    raw = _read_cache().get("last_report")
    return DetectionReport.from_dict(raw) if isinstance(raw, dict) else None
