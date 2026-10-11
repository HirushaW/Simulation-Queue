# SimQ: Simulation Queue

A desktop app for running simulations on a Linux workstation. It watches the
machine's CPU, memory, GPUs and disks live, finds the simulation software that
is installed, and (in the next build) queues, checks and runs your
simulations one after another, sharing the cores and GPUs between them.

It works with any program you can start from a terminal and has built-in
knowledge of **NAMD**, **AMBER** and **UDB** packages.

> **Status: Phase 1 of 4.** The Dashboard and Software pages work. The queue
> arrives in Phase 2. See [`docs/DESIGN.md`](docs/DESIGN.md) for the full plan.

## What works now

**Dashboard**: live view, updated every second:
- CPU load, clock, load average, temperature, and one bar per CPU thread
- Memory and swap
- Every NVIDIA GPU: load, memory, temperature, power and fan
- Every disk: free space and read/write speed (a dead network mount is shown as
  *not responding* instead of freezing the app)
- A 2-minute history graph for CPU, memory and each GPU

**Software**: finds simulation programs automatically:
- NAMD (`namd3`, `namd2`, `charmrun`) with version and build, e.g. *multicore-CUDA*
- AMBER (`pmemd.cuda`, `pmemd`, `sander`, `tleap`, `cpptraj` …), plus `AMBERHOME` and `amber.sh`
- GROMACS, LAMMPS, ORCA, Gaussian, CP2K, Quantum ESPRESSO, VASP
- VMD, Packmol, Open Babel, MPI, Python, Tcl, the NVIDIA driver and CUDA

It looks in `PATH`, `/opt`, `/usr/local`, `~/.local/bin` and the folders in
your home folder, so an unpacked `~/NAMD_3.0.2_Linux-x86_64-multicore-CUDA`
is found without any setup. You can add more search folders, or point at a
program by hand.

## Installation

Needs Linux and Python 3.10 or newer.

```bash
# Qt needs this library on X11 desktops (Ubuntu/Debian):
sudo apt install python3-venv libxcb-cursor0

git clone https://github.com/HirushaW/Simulation-Queue.git
cd Simulation-Queue
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

```bash
simq                 # open the window
simq gui --demo      # preview the dashboard with two simulated GPUs
simq info            # print CPU, memory, GPUs and disks in the terminal
simq detect          # print the simulation software found
simq detect --json   # the same, as JSON
```

To start it later, from a new terminal:

```bash
cd Simulation-Queue && source .venv/bin/activate && simq
```

SimQ keeps its settings in `~/.simq/`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Could not load the Qt platform plugin "xcb"` | `sudo apt install libxcb-cursor0` |
| `libEGL.so.1: cannot open shared object file` | `sudo apt install libegl1` |
| `no graphical display found` | Run it on the workstation's desktop, or connect with `ssh -X` |
| GPU shows *No NVIDIA GPU detected* | Check that `nvidia-smi` works in a terminal |
| A program is not found | Software → **Search folders…** to add its folder, or **Add program…** |

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
QT_QPA_PLATFORM=offscreen pytest
```
