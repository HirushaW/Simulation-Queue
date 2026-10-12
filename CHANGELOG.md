# Changelog

All notable changes to SimQ. Versions follow [semantic versioning](https://semver.org).

## [0.1.0] - 2026-10-12

The first complete version.

### Added
- **Dashboard:**
  - live CPU (one bar per thread), memory, NVIDIA GPUs (NVML, falling back to `nvidia-smi`) and disks, with 2-minute history graphs
  - the jobs running now
- **Software detection:** NAMD, AMBER (with `AMBERHOME` and `amber.sh`), GROMACS, LAMMPS, ORCA, Gaussian, CP2K, Quantum ESPRESSO, VASP and tools, with versions and GPU builds.
- **Queue service:** a background process that runs the queue; jobs outlive the window and the service.
- **Scheduling:**
  - CPU and GPU aware, with EASY backfilling
  - CPU pinning and GPU assignment through `CUDA_VISIBLE_DEVICES`
  - delayed starts and job dependencies
- **Verification:** a quick check (*verify all first* or *verify as you go*, repeated before every start) and test runs of the real pipeline in a scratch copy.
- **Job types:** UDB packages, NAMD configs (exact step counts from a built-in Tcl interpreter), AMBER mdin, any command.
- **Estimates:** from live logs, test runs and the speed memory; a core-count test; a predicted finish for every job and the whole queue.
- **Crash recovery:** retries, with UDB jobs continuing from the last finished stage.
- **Health watch:** stall, NaN-energy and GPU-temperature warnings; a disk guard that pauses and resumes jobs.
- **Notifications:** desktop pop-ups and email.
- **Automatic start:** an optional systemd user service.
- **Command line:** `simq add`, `ls`, `queue`, `check`, `test`, `corescan`, `run-now`, `kill`, `pause`, `resume`, `retry`, `hold`, `release`, `remove`, `log`, `service`, `info`, `detect`.

### Fixed
- `simq add` printed "1 CPUs, GPU none"; it now prints "1 CPU, no GPU".
- A speed estimated from the clock at the start of a stage could replace the speed NAMD or AMBER reported, and end up in the speed memory. The reported speed is now kept.

[0.1.0]: https://github.com/HirushaW/Simulation-Queue/releases/tag/v0.1.0
