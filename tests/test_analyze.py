from pathlib import Path

import pytest
from helpers import UDB_DATA

from simq.analyze import amber, udb
from simq.analyze.folder import inspect_folder
from simq.analyze.namd import evaluate_config, explain, parse_log


def test_real_stage_configs():
    expected = {"01_minimize": ("minimize", 2000), "02_heat": ("run", 100000),
                "03_nvt_equil": ("run", 250000), "04_npt_equil": ("run", 5000000),
                "05_production": ("run", 45000000)}  # fmt: skip
    for stem, (kind, steps) in expected.items():
        cfg = evaluate_config(UDB_DATA / f"{stem}.conf")
        assert cfg.error is None
        assert [(s.kind, s.steps) for s in cfg.segments] == [(kind, steps)]
        assert cfg.output_name == stem
        assert cfg.timestep_fs == 2.0
        assert cfg.dcd_freq == 1000
        assert not cfg.gpu_resident  # `set gpu 0` in udb_md.conf
    heat = evaluate_config(UDB_DATA / "02_heat.conf")
    assert ("bincoordinates", "01_minimize.restart.coor") in heat.inputs


def test_all_in_one_phases():
    p1 = evaluate_config(UDB_DATA / "dendrimer_pamam_gen4_full.namd", env={"UDB_PHASE": "1"})
    p2 = evaluate_config(UDB_DATA / "dendrimer_pamam_gen4_full.namd", env={"UDB_PHASE": "2"})
    assert p1.error is None and p2.error is None
    assert p1.min_steps == 2000
    assert p1.md_steps == 100000 + 250000  # heating in 20 windows + NVT
    assert len(p1.segments) == 1 + 20 + 1
    assert p2.md_steps == 5000000 + 45000000
    assert p1.output_name.endswith("_p1") and p2.output_name.endswith("_p2")
    assert "stage 5/5" in p2.segments[-1].label


def test_udb_package():
    pkg = udb.read_package(UDB_DATA)
    assert pkg.errors == []
    assert pkg.prefix == "dendrimer_pamam_gen4"
    assert pkg.scripts == ["run_namd.sh", "run_full.sh"]
    assert pkg.namd_bin == "/home/hirusha/.local/bin/namd3"
    assert not pkg.gpu
    for script in pkg.scripts:
        assert pkg.total_steps(script) == 50_352_000
        assert pkg.md_ns(script) == 100.7
        assert [s.number for s in pkg.stages(script)] == [1, 2, 3, 4, 5]
    pkg.atoms = 275_000
    traj = pkg.trajectory_bytes("run_namd.sh")
    assert 150e9 < traj < 180e9  # ~50,352 frames × 3.3 MB


def test_resume_plans(tmp_path):
    import shutil

    folder = tmp_path / "pkg"
    shutil.copytree(UDB_DATA, folder)
    pkg = udb.read_package(folder)
    assert udb.resume_plan(pkg, "run_namd.sh").args == ""
    # Stages 1-2 finished, stage 3 stopped part-way.
    for stem in ("01_minimize", "02_heat"):
        (folder / f"{stem}.coor").write_text("x")
        (folder / f"{stem}.restart.coor").write_text("x")
    (folder / "03_nvt_equil.restart.coor").write_text("x")
    (folder / "03_nvt_equil.restart.xsc").write_text("x")
    plan = udb.resume_plan(pkg, "run_namd.sh")
    assert plan.args == "" and {p.name for p in plan.move_aside} == {
        "03_nvt_equil.restart.coor",
        "03_nvt_equil.restart.xsc",
    }
    moved = udb.move_aside(plan.move_aside)
    assert all(".simq-interrupted-" in p.name for p in moved)
    assert not (folder / "03_nvt_equil.restart.coor").exists()
    # Production interrupted with its restart config present: continue it.
    for stem in ("03_nvt_equil", "04_npt_equil"):
        (folder / f"{stem}.coor").write_text("x")
    (folder / "05_production.restart.coor").write_text("x")
    (folder / "05_production_restart.conf").write_text("x")
    assert udb.resume_plan(pkg, "run_namd.sh").args == "restart"
    assert udb.command_for("run_namd.sh", "restart") == "bash run_namd.sh restart {cores}"
    # All-in-one: phase 2 interrupted.
    (folder / "dendrimer_pamam_gen4_full_p1.coor").write_text("x")
    (folder / "dendrimer_pamam_gen4_full_p2.restart.coor").write_text("x")
    assert udb.resume_plan(pkg, "run_full.sh").args == "resume"


NAMD_LOG = """\
Info: NAMD 3.0.2 for Linux-x86_64-multicore-CUDA
Info: 275123 ATOMS
Info: Benchmark time: 48 CPUs 0.0250 s/step 0.144 days/ns 1000 MB memory
ENERGY:       0    1.0 2.0
ENERGY:    1000    1.0 2.0
TIMING: 1000  CPU: 24.5, 0.0245/step  Wall: 24.9, 0.0249/step, 3.45 hours remaining, 1000.2 MB of memory in use.
ENERGY:    2000    1.0 2.0
"""  # noqa: E501


def test_parse_namd_log(tmp_path):
    log = tmp_path / "md.log"
    log.write_text(NAMD_LOG)
    p = parse_log(log)
    assert p.atoms == 275123
    assert p.step == 2000 and p.first_step == 0
    assert p.s_per_step == 0.0249
    assert p.hours_remaining == 3.45
    assert not p.finished and not p.nan and p.fatal is None
    log.write_text(
        NAMD_LOG + "ENERGY:    3000    nan  -nan\nFATAL ERROR: Periodic cell has "
        "become too small for original patch grid!\n"
    )
    p = parse_log(log)
    assert p.nan and "Periodic cell" in p.fatal
    assert "box shrank" in explain(p.fatal)
    log.write_text(NAMD_LOG + "WallClock: 99.5  CPUTime: 98.0  Memory: 1000 MB\n")
    assert parse_log(log).finished


MDIN = """Production
 &cntrl
  imin=0, irest=1, ntx=5,
  nstlim=500000, dt=0.002,   ! 1 ns
  ntt=3, gamma_ln=1.0, temp0=300.0,
 /
"""

MDINFO = """\
 NSTEP =   250000   TIME(PS) =     500.000  TEMP(K) =   300.12  PRESS =     0.0
|  Average timings for last    5000 steps:
|     Elapsed(s) =      12.34 Per Step(ms) =       2.47
|         ns/day =      70.04   seconds/ns =    1233.56
|
|  Estimated time remaining:       10.3 minutes.
"""


def test_amber(tmp_path):
    values = amber.parse_mdin(MDIN)
    plan = amber.mdin_plan(values)
    assert plan.steps == 500000 and not plan.minimization and plan.ns == 1.0
    assert amber.mdin_plan(amber.parse_mdin("&cntrl\n imin=1, maxcyc=5000,\n/\n")).steps == 5000
    info = amber.parse_mdinfo(MDINFO)
    assert info.step == 250000 and info.ns_per_day == 70.04
    assert info.s_per_step == pytest.approx(0.00247)
    assert info.remaining_s == pytest.approx(10.3 * 60)
    cmd = amber.parse_command(
        "mpirun -np 8 pmemd.MPI -O -i md.in -p sys.prmtop -c eq.rst7 -o md.out -inf md.info"
    )
    assert (cmd.program, cmd.mdin, cmd.prmtop, cmd.inpcrd, cmd.mdinfo) == (
        "pmemd.MPI",
        "md.in",
        "sys.prmtop",
        "eq.rst7",
        "md.info",
    )
    top = tmp_path / "x.prmtop"
    top.write_text("%VERSION\n%FLAG POINTERS\n%FORMAT(10I8)\n   12345       7\n")
    assert amber.prmtop_atoms(top) == 12345


def test_inspect_folder(tmp_path):
    assert inspect_folder(UDB_DATA).kind == "udb"
    namd = tmp_path / "namd"
    namd.mkdir()
    (namd / "md.conf").write_text("structure x.psf\ncoordinates x.pdb\ntimestep 2\nrun 5000\n")
    guess = inspect_folder(namd)
    assert guess.kind == "namd" and guess.choices["namd"] == ["md.conf"]
    amb = tmp_path / "amber"
    amb.mkdir()
    (amb / "prod.in").write_text(MDIN)
    assert inspect_folder(amb).kind == "amber"
    other = tmp_path / "other"
    other.mkdir()
    (other / "go.sh").write_text("echo hi\n")
    assert inspect_folder(other).kind == "command"
    assert inspect_folder(Path(tmp_path / "missing")).kind == "command"
