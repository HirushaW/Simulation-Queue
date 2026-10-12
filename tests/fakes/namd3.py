"""A stand-in for NAMD 3, for tests.

It reads the configuration with SimQ's Tcl reader (so `source`, procs and
loops work as in NAMD), checks that the input files exist, then "runs" each
minimize/run segment: it prints ENERGY, TIMING and Benchmark lines in NAMD's
format, writes restart files every restartfreq steps and the final output at
the end, and finishes with a WallClock line.

Environment knobs:
  FAKE_NAMD_SPS   seconds per step to pretend (default 0.0002)
  FAKE_NAMD_FAIL  "<outputName>:<step>" -> die there with a NAMD-style fatal error
  FAKE_NAMD_FAIL_FLAG  only fail while this file exists (it is deleted on failing),
                  so a retry succeeds
"""

import os
import struct
import sys
import time
from pathlib import Path

from simq.analyze.amber import prmtop_atoms
from simq.analyze.namd import evaluate_config


def write_coor(path: Path, natoms: int) -> None:
    with open(path, "wb") as fh:
        fh.write(struct.pack("i", natoms))
        fh.write(struct.pack(f"{3 * natoms}d", *([0.0] * 3 * natoms)))


def write_xsc(path: Path, step: int) -> None:
    path.write_text(
        "# NAMD extended system configuration output file\n"
        "#$LABELS step a_x a_y a_z b_x b_y b_z c_x c_y c_z o_x o_y o_z\n"
        f"{step} 40 0 0 0 40 0 0 0 40 20 20 20\n"
    )


def save(out: str, natoms: int, step: int, restart: bool) -> None:
    stem = f"{out}.restart" if restart else out
    write_coor(Path(f"{stem}.coor"), natoms)
    write_coor(Path(f"{stem}.vel"), natoms)
    write_xsc(Path(f"{stem}.xsc"), step)


def main() -> int:
    args = sys.argv[1:]
    cores, config = "1", None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "+devices":
            i += 2
            continue
        if a.startswith("+p") and a[2:].isdigit():
            cores = a[2:]
        elif not a.startswith("+"):
            config = a
        i += 1
    print("Charm++: standalone mode (not using charmrun)")
    print("Info: NAMD 3.0.2 for Linux-x86_64-multicore", flush=True)
    if config is None:
        print("FATAL ERROR: No simulation config file specified on command line.")
        return 1
    cfg = evaluate_config(config, cwd=os.getcwd(), env=dict(os.environ))
    if cfg.error:
        print(f"FATAL ERROR: {cfg.error}")
        return 1
    for key, value in cfg.inputs:
        if not Path(value).exists():
            print(f"FATAL ERROR: Unable to open {key} file {value}")
            return 1
    parm = cfg.params.get("parmfile")
    natoms = (prmtop_atoms(parm) if parm else None) or 1000
    print(f"Info: {natoms} ATOMS", flush=True)

    sps = float(os.environ.get("FAKE_NAMD_SPS", "0.0002"))
    fail = os.environ.get("FAKE_NAMD_FAIL", "")
    fail_out, _, fail_step = fail.partition(":")
    out = cfg.output_name or "output"
    energy = max(1, int(float(cfg.params.get("outputenergies", 100))))
    restart = int(float(cfg.params.get("restartfreq", 0) or 0))
    total = cfg.total_steps
    step = cfg.first_timestep
    benchmarked = False
    started = time.time()
    print(f"ENERGY: {step:8d}      0.0000      0.0000    -1000.0000    300.0000", flush=True)
    for seg in cfg.segments:
        done = 0
        while done < seg.steps:
            chunk = min(energy, seg.steps - done)
            time.sleep(chunk * sps)
            done += chunk
            step += chunk
            print(f"ENERGY: {step:8d}      0.0000      0.0000    -1000.0000    300.0000")
            if not benchmarked and step - cfg.first_timestep >= 100:
                benchmarked = True
                print(
                    f"Info: Benchmark time: {cores} CPUs {sps:.6g} s/step "
                    f"{sps * 5.787:.6g} days/ns 100 MB memory"
                )
            left = (total - (step - cfg.first_timestep)) * sps / 3600
            wall = time.time() - started
            print(
                f"TIMING: {step}  CPU: {wall:.3f}, {sps:.6g}/step  Wall: {wall:.3f}, "
                f"{sps:.6g}/step, {left:.6g} hours remaining, 100.0 MB of memory in use.",
                flush=True,
            )
            if restart and step % restart == 0:
                save(out, natoms, step, restart=True)
            flag = os.environ.get("FAKE_NAMD_FAIL_FLAG")
            armed = flag is None or Path(flag).exists()
            if fail_out == out and fail_step and step >= int(fail_step) and armed:
                if flag:
                    Path(flag).unlink()
                print(
                    "FATAL ERROR: Atoms moving too fast; simulation has become unstable.",
                    flush=True,
                )
                return 1
    save(out, natoms, step, restart=False)
    print(f"WallClock: {time.time() - started:.3f}  CPUTime: 1.0  Memory: 100 MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
