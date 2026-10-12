# SimQ: Simulation Queue

A desktop app that runs your simulations one after another on a Linux
workstation, sharing the CPUs and GPUs between them.

- **Queue:** add simulation folders; SimQ checks them, runs them in order, runs
  small jobs next to big ones when CPUs are free, and starts the next job as
  soon as one finishes.
- **Verify before running:** a 2-second quick check of every job, and an optional
  *test run* of the real pipeline in a scratch copy.
- **Time estimates:** for every job and for the whole queue, measured from your
  own runs.
- **Crash recovery:** a crashed job is restarted from its last restart files.
  If it still fails you get a notification, and the queue moves on.
- **Live dashboard:** CPU (one bar per thread), memory, every NVIDIA GPU and every disk.
- **Software detection:** NAMD, AMBER, GROMACS, LAMMPS, ORCA, Gaussian, CP2K … with versions and GPU builds.

It runs any command you would type in a terminal, and has built-in knowledge of
**NAMD**, **AMBER** and **UDB** packages (`run_namd.sh` / `run_full.sh`).

## Installation

Needs Linux and Python 3.10 or newer.

```bash
sudo apt install python3-venv libxcb-cursor0     # Ubuntu/Debian

git clone https://github.com/HirushaW/Simulation-Queue.git
cd Simulation-Queue
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Start it with `simq`. Later, from a new terminal:
`cd Simulation-Queue && source .venv/bin/activate && simq`.

## How to use it

1. **Open SimQ** (`simq`). The background *queue service* starts by itself.
2. **Software page:** check that your `namd3`, `tleap`, `pmemd` … were found.
3. **Queue → + Add job:** choose a simulation folder. SimQ recognises what is in
   it and fills in the command, the CPUs and the GPU. For a UDB package it
   picks `run_namd.sh` (or `run_full.sh`) and reads every stage's step count.
   **+ Add folders…** adds many folders at once.
4. Each job gets a **quick check** (✔ / ✘). Select a job and press **Test run**
   to run the whole pipeline shrunk to a few thousand steps in a scratch copy:
   ✔✔ means the pipeline works and its speed has been measured.
5. Press **▶ Start queue.** Jobs start in order while their CPUs and GPU are free.
6. Close the window whenever you like: **the queue keeps running.** Open SimQ
   again to see where it is.

### The queue screen

| Column | Meaning |
|---|---|
| Check | `–` not checked · `✔` passed · `✔ !` passed with warnings · `✔✔` test run passed · `✘` failed |
| Status | waiting · ▶ running (with the current stage) · ⏸ paused · ✔ done · ✘ failed · on hold |
| Time | time left for a running job; the estimated run time for a waiting one |
| When | when a waiting job should start and end; when a running one should end |

Buttons for the selected job: **Check**, **Test run**, **Core test** (speed at
several CPU counts, with a "use N CPUs" suggestion), **Run now** (start even if the
queue is paused), **Pause / Resume**, **Kill**, **Run again** (a UDB job continues
where it stopped), **Hold / Release**, **Edit**, **Duplicate**, **Remove**, and ↑ ↓ to reorder.

Below the table: **Overview** (stages, speed, estimate, last failure), **Check**
(every finding), **Output** (the live NAMD log or the command's output) and **Events**.

### How the queue shares the machine

- Each job asks for a number of **CPUs** and **no GPU / any free GPU / a specific GPU**.
- A job starts when its CPUs and GPU are free. With one GPU, GPU jobs run one at a
  time, and CPU jobs run next to them on the remaining CPUs.
- A smaller job may start ahead of a bigger waiting job only if that cannot delay
  the bigger one.
- Every job is pinned to its own CPUs (`taskset`), so jobs never fight over a core.
- The GPU is chosen with `CUDA_VISIBLE_DEVICES`, so scripts that say `+devices 0`
  run on whichever GPU the queue gives them.
- `{cores}` and `{gpu}` in a command are filled in when the job starts. Jobs also
  get `SIMQ_CORES`, `OMP_NUM_THREADS` and (for UDB) `NCORES`.

### Crash recovery and safety

- A job that fails is retried (Settings → Crash recovery; per job in its dialog).
  A UDB job continues from its last finished stage: an interrupted production run
  uses `run_namd.sh restart` / `run_full.sh resume`.
  An interrupted earlier stage is redone from its start, after its partial restart
  files are moved aside (renamed `*.simq-interrupted-<time>`). Without that,
  `run_namd.sh` would skip the half-done stage.
- After the last retry the job is marked failed, the reason is shown in plain
  words, you are notified, and the queue moves on. Jobs that wait for it are held.
- **Health watch:** a warning when a job writes nothing for 30 minutes, when
  energies become NaN, or when a GPU gets hot. When a disk runs nearly full,
  running jobs on it are paused, and they resume once space is freed.
- Jobs outlive the window and the queue service. If the service restarts it picks
  them up again, and a job lost while it was down (e.g. a power cut) counts as a crash.

### Notifications

Desktop pop-ups, and optionally email (Settings → Notifications). For Gmail use
`smtp.gmail.com`, port 587, STARTTLS, your address as username and an
**App Password** (Google Account → Security → 2-Step Verification → App passwords).
Press **Send a test email** to try it.

### Start automatically

Settings → Queue service → *Start the queue service automatically* installs a
systemd user service, so the queue carries on after a reboot without opening
SimQ. To run it at boot before anyone logs in, also run once:
`sudo loginctl enable-linger $USER`.

## Command line

Everything also works from a terminal (or over SSH):

```bash
simq add ~/sims/PAMAM_G4 --cores 16       # add a job (type and command are detected)
simq add ~/sims/run2 --command "bash go.sh {cores}" --cores 8 --gpu auto --at 22:00
simq ls                                    # the queue
simq queue start                           # start (or: simq queue pause)
simq check 3 · simq test 3 · simq corescan 3
simq kill 3 · simq pause 3 · simq resume 3 · simq retry 3
simq log 3                                 # events and the end of the output
simq service status|start|stop
simq info · simq detect                    # hardware · software
```

## Where things are

`~/.simq/` holds the queue database (`simq.db`), one folder per run under
`runs/` (the generated launch script and the command's output), scratch copies
for test runs, and the service log.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Could not load the Qt platform plugin "xcb"` | `sudo apt install libxcb-cursor0` |
| `libEGL.so.1: cannot open shared object file` | `sudo apt install libegl1` |
| Sidebar says *Queue service stopped* | press **Start service**, or run `simq service start`; see `~/.simq/service.log` |
| A job's check says a program was not found | Software → **Search folders…** or **Add program…** |
| GPU shows *No NVIDIA GPU detected* | check that `nvidia-smi` works in a terminal |

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
QT_QPA_PLATFORM=offscreen pytest
```

The tests run the real `run_namd.sh` / `run_full.sh` from a UDB package
(`tests/data/udb_g4`) against fake `namd3` and `tleap` programs
(`tests/fakes/`). These fakes write the same logs, restart files and outputs as
the real programs, and can crash on purpose.
See [`docs/DESIGN.md`](docs/DESIGN.md) for how SimQ is put together and
[`docs/UDB_NOTES.md`](docs/UDB_NOTES.md) for suggestions for the UDB scripts.
