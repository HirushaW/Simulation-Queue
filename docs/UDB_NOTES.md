# Notes for the UDB generator

Building SimQ against the UDB v1.0 PAMAM G4 package turned up a few things worth
changing in the scripts UDB generates. SimQ already works around all of them, so
none is urgent. They would help anyone running the scripts by hand.

## 1. `run_namd.sh` skips a stage that only ran half-way

`run_namd.sh` treats a stage as finished when its `0X_<stage>.restart.coor/.vel/.xsc`
files exist. NAMD writes those every `restartfreq` steps *while the stage
runs*, not only at the end. If a stage is killed part-way (power cut, crash, kill),
re-running the script skips that stage. The next stage then starts from a
half-equilibrated structure, with no error or warning.

**Fix:** test for the stage's *final* output instead. NAMD writes
`<outputName>.coor` only when a run ends normally:

```bash
if [ ! -f "03_nvt_equil.coor" ]; then
    $NAMD_DYN 03_nvt_equil.conf > logs/03_nvt_equil.log 2>&1
    ...
```

(The all-in-one `<prefix>_full.namd` already does this right: phase 2 reads
`${outname}_p1.coor`, the final file, not the restart.)

**What SimQ does:** when it retries such a job, it moves the interrupted stage's
restart files aside (`*.simq-interrupted-<time>`), so the stage is redone from its
start.

## 2. `run_namd.sh` ignores `NAMD_BIN` from the environment

`run_full.sh` uses `NAMD_BIN="${NAMD_BIN:-/home/…/namd3}"`, so the binary can be
chosen from outside. `run_namd.sh` hard-codes `NAMD_BIN="/home/…/namd3"`.

**Fix:** use the same `${NAMD_BIN:-…}` form in `run_namd.sh`.

**What SimQ does:** its check reads `NAMD_BIN` from the script and reports a
clear error if that file does not exist.

## 3. The trajectory size message is fixed at generation time

`run_namd.sh` prints `Estimated trajectory: ~54.0 GB`. That number is written
into the script when UDB generates it, before tLeap has solvated the system. For
this package the solvated box is 140.6 Å, about 275,000 atoms. At
`traj_freq 1000`, production writes 45,000 frames of about 3.3 MB each: **about
150 GB**, and **about 166 GB** for all stages together.

**Fix:** compute it after solvation, from the atom count in the prmtop:
`frames × (atoms × 12 + 80)` bytes.

**What SimQ does:** its check computes this size from the real atom count and
refuses to start a job whose trajectory would not fit on the disk.

## 4. `+devices 0` is hard-coded

Both launchers use `+devices 0`. On a machine with several GPUs this always picks
the first one.

**What SimQ does:** it sets `CUDA_VISIBLE_DEVICES` to the GPU it assigns, so inside
the job that GPU is device 0 and the scripts work unchanged. If you run the
scripts by hand on a multi-GPU machine, set `CUDA_VISIBLE_DEVICES` yourself.

## 5. Re-running a finished package redoes production

`run_namd.sh` has no "already done" test for stage 5, so running a finished
package again overwrites the production output. SimQ's check warns about this
before such a job starts.
