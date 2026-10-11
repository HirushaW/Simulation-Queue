# SimQ — Simulation Queue for a Linux Workstation

Final design, agreed before the build starts.

SimQ is a standalone desktop app that queues, verifies, runs and monitors
simulations on one Linux workstation. It works with any program you can run
from a terminal, and understands NAMD, AMBER and UDB packages in depth.

---

## 1. The window

Five tabs.

### 1.1 Dashboard
Live view of the machine, updated about once a second, with animated gauges and graphs.

- **CPU**: model, physical cores and threads, clock speed, a load bar for each core
- **RAM**: used, free and total
- **GPU(s)**, one card per GPU (supports several, of different models):
  utilisation, VRAM, temperature, power
- **Disks**: used and free space per disk, read/write speed
- **Running jobs**: which job is using which cores and which GPU

### 1.2 Queue
The main screen.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ [+ Add job] [+ Add folder…]   Mode: (•) Verify all first  ( ) Verify as you go│
│ [▶ Start queue] [⏸ Pause queue]        Cores free 40/48   GPU 0: busy         │
├──┬──────────────┬────────┬─────┬─────┬──────┬───────────────┬──────┬─────────┤
│# │ Name         │ Type   │Cores│ GPU │Check │ Status        │ ETA  │ Starts  │
├──┼──────────────┼────────┼─────┼─────┼──────┼───────────────┼──────┼─────────┤
│1 │ PAMAM G4     │ UDB    │  2  │  0  │ ✔✔   │ ▶ 4/5 NPT 62% │ 31 h │ running │
│2 │ PAMAM G5     │ UDB    │ 48  │  –  │ ✔    │ waiting       │ 3.1 d│ Tue 14:00│
│3 │ ligand_opt   │ Command│  8  │  –  │ ✔    │ ▶ running     │ 2 h  │ running │
│4 │ G3 test      │ NAMD   │ 16  │  –  │ ✘    │ check failed  │  –   │ –       │
└──┴──────────────┴────────┴─────┴─────┴──────┴───────────────┴──────┴─────────┘
  Selected job: [Check] [Test run] [Run] [Pause] [Kill] [Resume] [Duplicate]
                [Edit] [Remove] [↑] [↓]
  ── Details: stages with steps and times · estimate · last 50 log lines ──
  Whole queue finishes: ~Thu 09:30
```

### 1.3 Software
Found automatically at startup, and again when you press **Rescan**.

- **NAMD**: `namd2`, `namd3`, `charmrun`. Shows CPU / CUDA / MPI build and version.
- **AMBER**: `pmemd`, `pmemd.cuda`, `pmemd.MPI`, `sander`, `tleap`, `cpptraj`, `$AMBERHOME`, `amber.sh`
- **Others**: GROMACS (`gmx`), LAMMPS, `mpirun`, VMD, `python3`, NVIDIA driver and CUDA
- **Where it looks**: `PATH`, `/opt`, `/usr/local`, `~/.local/bin`, the folders in your home folder, and any folders you add
- Each program gets ✔/✘, its path and version. You can also add a path by hand.
- Jobs are launched with this environment set up (for example `amber.sh` is sourced), so scripts work even when the app wasn't started from a terminal.

### 1.4 History
- Finished and failed jobs, with wall time and speed (ns/day)
- **Speed memory**: measured speeds, used for instant estimates of new jobs
- Results of the core-count test

### 1.5 Settings
- Total core budget, and cores kept free for the system
- Email notifications (Gmail app password, stored only on this machine)
- Retry count after a crash (default 2)
- Health-watch limits: stall time, minimum free disk space, maximum GPU temperature
- Start the queue service at boot (on/off)

---

## 2. Job types

| Type | Recognised by | Step count from | Live progress from |
|---|---|---|---|
| **UDB package** | `udb_md.conf` + `run_namd.sh` / `run_full.sh` | `udb_md.conf`, evaluated as Tcl | stage logs, `UDB: stage n/5` lines, NAMD `TIMING:` |
| **NAMD config** | a `.conf` / `.namd` file | the config, evaluated as Tcl (loops and variables work) | NAMD `TIMING:` lines |
| **AMBER** | `pmemd*` / `sander` + `mdin` | `nstlim`, `dt`, `maxcyc` | `mdinfo` (ns/day, time remaining) |
| **Any command** | anything else | your own estimate, or speed memory | elapsed time |

**Fields in every job**
- name and folder
- command, with `{cores}` and `{gpu}` placeholders
- cores, and GPU (none / a specific GPU / auto)
- start condition: now / at a set time / after job X
- retry count

---

## 3. Scheduling rules

- The machine is a pool of **N cores + M GPUs** (here: all cores + 1 GPU).
- A job starts when its cores **and** its GPU are free.
- With one GPU, GPU jobs run one at a time. CPU jobs run next to them on the remaining cores.
- **Smaller jobs can go first.** A small job may start ahead of a bigger waiting job only if its estimate shows it will finish before the big job could start anyway.
- Every job is **pinned to its own cores** (`taskset`), so a job can't use another job's cores.
- **GPU selection** uses `CUDA_VISIBLE_DEVICES`, so scripts with `+devices 0` work on any GPU without being edited. Jobs also get `CUDA_DEVICE_ORDER=PCI_BUS_ID`, so GPU numbers match the dashboard and `nvidia-smi` (CUDA otherwise numbers GPUs fastest-first).
- A GPU job holds its cores for its whole run.
  - Default for a UDB `run_full.sh` GPU job: 2 cores.
  - `run_namd.sh` needs its full core count for minimisation.
- **Delayed start**: at a set time, or after another job finishes.

### Verification modes
- **Verify all first**: every job gets its quick check up front, then the jobs run in order.
- **Verify as you go**: each job is checked just before it starts.
- In both modes a quick check runs again right before each job starts, in case its folder was moved or changed.

### Job states
`New → Checked ✔ → (Tested ✔✔) → Waiting / Scheduled → Running → Done`

Side states: `Check failed ✘`, `Paused`, `Retrying`, `Failed`, `Killed`, `Held` (the job it depends on failed).

---

## 4. Verification

| Level | Time | What it does | Result |
|---|---|---|---|
| **Quick check** (always) | ~2 s | folder and input files exist; programs found; `bash -n` on the scripts; step and atom counts read; trajectory size compared with free disk space | ✔ |
| **Test run** (optional button) | 2–5 min | copies the folder to scratch (without `*.dcd`, `*.restart.*`, `logs/`), shrinks every stage to 100–2 000 steps, runs your real script from start to finish, measures s/step, deletes the copy | ✔✔ + measured speed |

Your real folder is never modified by verification. A test run waits for free resources like any other job.

---

## 5. Time estimates

1. **Speed memory**: an instant estimate from earlier jobs with a similar atom count, program, mode and cores.
2. **Test run**: a measured speed for this exact job.
3. **Live**: once a job runs, the estimate updates continuously from its own log.
4. **Whole queue**: the start and end time of every job, and when the queue finishes.
5. **Core-count test** (optional): runs at 16, 32 and 48 cores and shows the speed of each, so you can give fewer cores to a job that gains little from more.

---

## 6. Reliability

- **Crash recovery**: if a job dies, it is restarted from its last restart files, up to the retry count. If that still fails, the job is marked Failed with the reason in plain words, you are notified, and **the queue moves on to the next job**.
- **UDB resume logic**: the job's restart point is worked out from its output files.
  - `run_full.sh`: phase 1 / phase 2 / `resume`
  - `run_namd.sh`: a stage that only ran halfway is relaunched with that stage's own `.conf`
- **Health watch**
  - a job whose log hasn't changed in 30 min is flagged as stalled
  - NaN energies are flagged
  - the queue pauses new starts before the disk fills up
  - a GPU temperature warning
- **Notifications**: desktop pop-up and email when a job is done or failed, or when a health warning fires.
- **Closing the window never stops jobs.** The queue service keeps running and the queue is saved on disk. Start-at-boot is optional, in Settings.

---

## 7. Terminal commands

```
simq                      open the window
simq gui --demo           preview the dashboard with two simulated GPUs
simq info                 print CPU, memory, GPUs and disks
simq detect [--json]      print the simulation software found
simq add <folder> [--cores N] [--gpu auto|0|1|none] [--after ID] [--at "22:00"]
simq ls                   list the queue
simq check <ID>           quick check
simq test <ID>            test run
simq kill <ID>  |  simq pause <ID>  |  simq resume <ID>
simq service start|stop|status
```

---

## 8. Code layout

```
Simulation-Queue/
├── simq/
│   ├── __main__.py          `simq` command
│   ├── db.py                SQLite storage (~/.simq/simq.db)
│   ├── service/
│   │   ├── scheduler.py     picks which job runs next
│   │   ├── runner.py        starts/stops processes, pinning, environment
│   │   ├── resources.py     core and GPU pool
│   │   └── api.py           local socket the window and CLI talk to
│   ├── monitor/
│   │   ├── system.py        CPU, RAM, disks (psutil)
│   │   └── gpu.py           NVIDIA GPUs (NVML)
│   ├── detect/software.py   finds NAMD, AMBER, GROMACS, …
│   ├── jobs/                generic.py, namd.py, amber.py, udb.py
│   ├── verify/              quick.py, testrun.py
│   ├── estimate/            tcl_steps.py, progress.py, history.py, corescan.py
│   ├── health.py
│   ├── notify.py            desktop + email
│   └── gui/                 main window, the 5 tabs, gauge widgets
├── tests/
├── docs/DESIGN.md, docs/UDB_NOTES.md
├── pyproject.toml
└── README.md
```

**Requirements**
- Linux, Python ≥ 3.10
- PySide6, pyqtgraph, psutil, nvidia-ml-py
- Tcl through Python's `tkinter`. On Ubuntu this needs the `python3-tk` package; if it's missing, SimQ falls back to `tclsh`.

App data lives in `~/.simq/`: the database, job logs and scratch space for test runs.

---

## 9. Build phases

1. **Phase 1**: app skeleton, Dashboard tab (live CPU, RAM, GPU, disks), Software tab (detection).
2. **Phase 2**: the queue. Add / duplicate / edit jobs, quick check, run / kill / pause / resume, core and GPU scheduling with pinning, both verification modes, moving on after a failure, delayed start and job dependencies, `simq` commands.
3. **Phase 3**: estimates. Tcl step counter, UDB / NAMD / AMBER progress parsing, test run, speed memory, core-count test, whole-queue ETA.
4. **Phase 4**: reliability. Crash recovery and UDB resume logic, health watch, desktop and email notifications, optional start-at-boot.

Each phase is pushed to GitHub. It's built and tested here with simulated jobs, then tested for real on the workstation (NAMD, AMBER, GPU).

## 10. Not in scope (for now)
- Live MD plots (energy, temperature, density)
- Run reports
- Several users or several machines
