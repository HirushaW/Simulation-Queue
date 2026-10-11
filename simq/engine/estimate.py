"""Time estimates: how long a job will take, and how SimQ knows.

Sources, best first:

1. **live**: the job is running; its own log gives seconds per step.
2. **test run**: measured by this job's test run.
3. **similar job**: speed memory; a finished run or test of the same program
   in the same CPU/GPU mode, scaled to this job's atom count and CPU count.
4. **your estimate**: hours typed in when adding a generic command.
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

from ..db import Job

# How NAMD/AMBER speed grows with CPUs on one node: time ∝ cores^-0.85 is a fair
# middle ground between perfect scaling (1.0) and what large systems achieve.
_CPU_SCALING = 0.85


def mode_of(job: Job) -> str:
    return "gpu" if job.gpu != "none" else "cpu"


def similar_speed(
    rows: list[sqlite3.Row],
    program: str,
    mode: str,
    atoms: int | None,
    cores: int,
    job_id: int | None = None,
) -> tuple[float, str] | None:
    """Seconds per step predicted from the speed memory, with a description."""
    best: tuple[float, float, str] | None = None
    for r in rows:
        if r["program"] != program or r["mode"] != mode or r["s_per_step"] <= 0:
            continue
        same_job = job_id is not None and r["job_id"] == job_id
        if atoms and r["atoms"]:
            ratio = atoms / r["atoms"]
            if not 0.5 <= ratio <= 2.0:
                continue
        elif not same_job:
            continue  # without atom counts only this job's own history is comparable
        else:
            ratio = 1.0
        s = r["s_per_step"] * ratio
        if mode == "cpu" and r["cores"] and cores:
            s *= (r["cores"] / cores) ** _CPU_SCALING
        distance = abs(math.log(ratio)) + 0.3 * abs(math.log((r["cores"] or 1) / max(1, cores)))
        if same_job:
            distance -= 1.0  # this job's own measurement beats any other
        atoms_text = f"{r['atoms']:,} atoms, " if r["atoms"] else ""
        label = f"similar job ({atoms_text}{r['cores']} CPUs{', GPU' if mode == 'gpu' else ''})"
        if same_job:
            label = "this job's earlier run"
        if best is None or distance < best[0]:
            best = (distance, s, label)
    return (best[1], best[2]) if best else None


def estimate(job: Job, rows: list[sqlite3.Row]) -> tuple[float | None, str | None]:
    """Seconds the whole job will take, and where the number comes from."""
    analysis: dict[str, Any] = job.meta.get("analysis") or {}
    steps = analysis.get("total_steps")
    program = analysis.get("program")
    speed = job.meta.get("speed") or {}
    if steps and speed.get("s_per_step") and speed.get("cores") in (None, job.cores):
        return steps * speed["s_per_step"], speed.get("source", "measured")
    if steps and program in ("namd", "amber"):
        found = similar_speed(rows, program, mode_of(job), analysis.get("atoms"), job.cores, job.id)
        if found:
            return steps * found[0], found[1]
    if job.manual_hours:
        return job.manual_hours * 3600.0, "your estimate"
    return None, None
