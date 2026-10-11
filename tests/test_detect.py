import os
import stat
import time
from pathlib import Path

import pytest

from simq.detect import software
from simq.detect.software import (
    detect_software,
    last_report,
    parse_gromacs,
    parse_namd,
    version_from_path,
)


def make_exe(path: Path, body: str = "exit 0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


NAMD_BANNER = (
    'echo "Charm++: standalone mode"; echo "Info: NAMD 3.0.2 for Linux-x86_64-multicore-CUDA"'
)


@pytest.fixture
def machine(tmp_path):
    """A fake home folder and PATH with a few programs installed."""
    home = tmp_path / "home"
    bindir = tmp_path / "usrbin"
    make_exe(bindir / "python3", 'echo "Python 3.12.1"')
    # NAMD unpacked in the home folder, not on PATH, plus a symlink on PATH.
    namd = make_exe(home / "NAMD_3.0.2_Linux-x86_64-multicore-CUDA" / "namd3", NAMD_BANNER)
    (bindir / "namd3").symlink_to(namd)
    # AMBER under ~/software/amber24/bin with amber.sh.
    amber = home / "software" / "amber24"
    for name in ("pmemd.cuda", "pmemd", "tleap"):
        make_exe(amber / "bin" / name)
    (amber / "amber.sh").write_text("export AMBERHOME=...\n")
    # GROMACS on a CPU-only build in ~/apps/gromacs/bin.
    make_exe(
        home / "apps" / "gromacs" / "bin" / "gmx",
        'echo "GROMACS version:    2024.2"; echo "GPU support:        disabled"',
    )
    # The GNOME screen reader is also called orca; it must not count as ORCA.
    make_exe(bindir / "orca", "exit 0")
    # Not executable: must be ignored.
    (bindir / "lmp").write_text("not a program")
    env = {"PATH": str(bindir)}
    return home, bindir, env


def by_name(report):
    return {i.name: i for i in report.installations}


def test_finds_programs_everywhere(machine):
    home, bindir, env = machine
    report = detect_software(env=env, home=home)
    found = by_name(report)

    assert found["namd3"].version == "3.0.2"
    assert found["namd3"].build == "multicore-CUDA"
    assert found["namd3"].gpu is True
    # The symlink on PATH and the real file are one program, reported via PATH.
    assert sum(1 for i in report.installations if i.name == "namd3") == 1
    assert found["namd3"].found_via == "PATH"

    assert found["pmemd.cuda"].gpu is True and found["pmemd"].gpu is False
    assert found["tleap"].version == "Amber 24"  # never run, version from folder name
    assert report.amber_home == str(home / "software" / "amber24")
    assert report.amber_sh.endswith("amber.sh")

    assert found["gmx"].version == "2024.2"
    assert found["gmx"].gpu is False and found["gmx"].build == "CPU"

    assert found["python3"].version == "3.12.1"
    assert "orca" not in found
    assert "lmp" not in found


def test_extra_folder_and_manual_program(machine, tmp_path):
    home, _, env = machine
    make_exe(
        tmp_path / "cluster-apps" / "lammps" / "bin" / "lmp",
        'echo "Large-scale Atomic/Molecular Massively Parallel Simulator - 2 Aug 2023"',
    )
    mine = make_exe(tmp_path / "scripts" / "my_md_driver")
    report = detect_software(
        extra_dirs=[str(tmp_path / "cluster-apps")],
        manual_programs=[str(mine), str(tmp_path / "gone" / "old_tool")],
        env=env,
        home=home,
    )
    found = by_name(report)
    assert found["lmp"].version == "2 Aug 2023"
    assert found["lmp"].found_via == "your folder"
    assert found["my_md_driver"].group == "Added by you"
    assert found["my_md_driver"].found_via == "added by you"
    assert found["old_tool"].missing


def test_cache_skips_unchanged_programs(machine):
    home, _, env = machine
    counter = home / "calls"
    make_exe(
        home / "software" / "cp2k" / "bin" / "cp2k.psmp",
        f'echo x >> "{counter}"; echo "CP2K version 2024.1"',
    )
    detect_software(env=env, home=home)
    detect_software(env=env, home=home)
    assert counter.read_text().count("x") == 1  # second scan used the cache

    exe = home / "software" / "cp2k" / "bin" / "cp2k.psmp"
    later = time.time() + 5
    os.utime(exe, (later, later))  # "upgraded": must be probed again
    report = detect_software(env=env, home=home)
    assert counter.read_text().count("x") == 2
    assert by_name(report)["cp2k.psmp"].version == "2024.1"
    assert last_report() is not None
    assert by_name(last_report())["cp2k.psmp"].version == "2024.1"


def test_failed_probe_is_retried_next_scan(machine):
    home, _, env = machine
    flag = home / "works"
    counter = home / "calls"
    make_exe(
        home / "software" / "lmp" / "bin" / "lmp",
        f'echo x >> "{counter}"; [ -f "{flag}" ] && echo "Simulator - 7 Feb 2024"',
    )
    assert by_name(detect_software(env=env, home=home))["lmp"].version is None
    flag.touch()
    assert by_name(detect_software(env=env, home=home))["lmp"].version == "7 Feb 2024"
    assert counter.read_text().count("x") == 2


def test_hanging_program_is_killed(machine, tmp_path):
    home, _, env = machine
    marker = tmp_path / "child-alive"
    # Starts a child that would outlive the probe unless the group is killed.
    make_exe(
        home / "software" / "gmx-hang" / "bin" / "gmx_mpi",
        f'(sleep 30; touch "{marker}") & sleep 30',
    )
    started = time.monotonic()
    report = detect_software(env=env, home=home, probe_timeout=0.5)
    assert time.monotonic() - started < 10
    assert by_name(report)["gmx_mpi"].version is None
    time.sleep(0.3)
    assert not marker.exists()


def test_parsers():
    assert parse_namd("Info: NAMD 2.14 for Linux-x86_64-multicore") == ("2.14", "multicore", False)
    assert parse_namd("nothing") == (None, None, None)
    v, build, gpu = parse_gromacs(
        "GROMACS version:    2023.3\nGPU support:        CUDA\nMPI library:        MPI\n"
    )
    assert (v, build, gpu) == ("2023.3", "CUDA, MPI", True)
    assert version_from_path("charmrun", "/x/NAMD_3.0b6_Linux-x86_64-netlrts/charmrun") == (
        "3.0b6",
        "netlrts",
        None,
    )
    assert version_from_path("sander", "/opt/ambertools23/bin/sander")[0] == "AmberTools 23"
    assert version_from_path("vmd", "/usr/local/bin/vmd") == (None, None, None)


def test_catalog_names_are_unique():
    names = [s.name for s in software.CATALOG]
    assert len(names) == len(set(names))
    assert all(s.group in software.GROUP_ORDER for s in software.CATALOG)
