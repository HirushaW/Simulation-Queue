"""Which waiting job starts next, which CPUs it gets, and when every job should end.

The rule ("EASY backfilling"):

* Jobs start in queue order while their CPUs and GPU are free.
* The first job that does not fit is *blocked*. A job further down the queue
  may still start ("backfill"), but only if it cannot delay the blocked
  job: either its estimate says it ends before the blocked job could start,
  or it only uses CPUs/GPUs the blocked job will not need even then.

The same function drives the live queue and the forward simulation that
predicts start and end times, so the predictions follow the real rules.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Busy:
    """Resources something is holding now."""

    cpus: int
    gpu: int | None
    end: float | None  # estimated end time; None when unknown


@dataclass(frozen=True)
class Want:
    """A job that wants to start."""

    key: object
    cpus: int
    gpu: str  # "none", "auto" or a GPU index as text
    est: float | None  # estimated run time in seconds; None when unknown


def gpu_choice(want: str, free: set[int]) -> int | None | bool:
    """GPU to use: an index, None for no GPU, or False when it cannot fit now."""
    if want == "none":
        return None
    if want == "auto":
        return min(free) if free else False
    try:
        index = int(want)
    except ValueError:
        return False
    return index if index in free else False


def possible(want: Want, total_cpus: int, gpus: list[int]) -> bool:
    """Could this job ever run on this machine?"""
    if want.cpus > total_cpus or want.cpus < 1:
        return False
    return gpu_choice(want.gpu, set(gpus)) is not False


def choose(
    now: float, total_cpus: int, gpus: list[int], busy: list[Busy], wants: list[Want]
) -> list[tuple[Want, int | None]]:
    """Jobs to start now, in order, each with the GPU it gets (or None)."""
    free_cpus = total_cpus - sum(b.cpus for b in busy)
    free_gpus = set(gpus) - {b.gpu for b in busy if b.gpu is not None}
    holding = list(busy)
    started: list[tuple[Want, int | None]] = []

    blocked: Want | None = None
    shadow: float | None = None
    spare_cpus = 0
    spare_gpus: set[int] = set()

    for want in wants:
        if not possible(want, total_cpus, gpus):
            continue  # never blocks the queue; the check reports it
        gpu = gpu_choice(want.gpu, free_gpus)
        fits = want.cpus <= free_cpus and gpu is not False

        if blocked is None:
            if fits:
                started.append((want, gpu))  # type: ignore[arg-type]
                free_cpus -= want.cpus
                if gpu is not None:
                    free_gpus.discard(gpu)  # type: ignore[arg-type]
                end = now + want.est if want.est is not None else None
                holding.append(Busy(want.cpus, gpu, end))  # type: ignore[arg-type]
                continue
            blocked = want
            shadow, spare_cpus, spare_gpus = _reservation(want, free_cpus, free_gpus, holding)
            continue

        if not fits:
            continue
        ends_in_time = shadow is not None and want.est is not None and now + want.est <= shadow
        uses_spare = want.cpus <= spare_cpus and (gpu is None or gpu in spare_gpus)
        if ends_in_time or uses_spare:
            started.append((want, gpu))  # type: ignore[arg-type]
            free_cpus -= want.cpus
            if gpu is not None:
                free_gpus.discard(gpu)  # type: ignore[arg-type]
            if not ends_in_time:
                spare_cpus -= want.cpus
                if gpu is not None:
                    spare_gpus.discard(gpu)  # type: ignore[arg-type]
    return started


def _reservation(
    blocked: Want, free_cpus: int, free_gpus: set[int], holding: list[Busy]
) -> tuple[float | None, int, set[int]]:
    """When the blocked job can start, and what will still be spare then."""
    cpus, gpus = free_cpus, set(free_gpus)
    when: float | None = None
    order = sorted(holding, key=lambda b: (b.end is None, b.end or 0.0))
    for item in order:
        if blocked.cpus <= cpus and gpu_choice(blocked.gpu, gpus) is not False:
            break
        cpus += item.cpus
        if item.gpu is not None:
            gpus.add(item.gpu)
        when = item.end  # None once we reach things with unknown ends
    gpu = gpu_choice(blocked.gpu, gpus)
    if blocked.cpus > cpus or gpu is False:
        return None, 0, set()
    spare = set(gpus)
    if gpu is not None:
        spare.discard(gpu)  # type: ignore[arg-type]
    return when, cpus - blocked.cpus, spare


# ---------------------------------------------------------------------------
# Predicting the whole queue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanItem:
    key: object
    cpus: int
    gpu: str
    est: float | None
    not_before: float | None = None  # delayed start
    after: object | None = None  # key of a job that must finish first
    ready: bool = True  # False: it cannot start (held, check failed …)


def plan(
    now: float,
    total_cpus: int,
    gpus: list[int],
    running: dict[object, Busy],
    queue: list[PlanItem],
) -> dict[object, tuple[float | None, float | None]]:
    """Predicted (start, end) for every queued job; None where it is unknown."""
    result: dict[object, tuple[float | None, float | None]] = {}
    active: list[tuple[object, Busy]] = list(running.items())
    pending = [q for q in queue if q.ready]
    t = now
    for _ in range(10_000):
        if not pending:
            break

        candidates = [
            q for q in pending
            if (q.not_before is None or q.not_before <= t)
            and _dependency_met(q, t, result, running, pending)
        ]  # fmt: skip
        wants = [Want(q.key, q.cpus, q.gpu, q.est) for q in candidates]
        for want, gpu in choose(t, total_cpus, gpus, [b for _, b in active], wants):
            end = t + want.est if want.est is not None else None
            result[want.key] = (t, end)
            active.append((want.key, Busy(want.cpus, gpu, end)))
            pending = [p for p in pending if p.key != want.key]

        upcoming = [b.end for _, b in active if b.end is not None and b.end > t]
        upcoming += [p.not_before for p in pending if p.not_before and p.not_before > t]
        upcoming += [
            result[p.after][1] for p in pending  # type: ignore[misc]
            if p.after in result and result[p.after][1] and result[p.after][1] > t
        ]  # fmt: skip
        if not upcoming:
            break
        t = min(upcoming)  # type: ignore[type-var]
        finished = {k for k, b in active if b.end is not None and b.end <= t}
        active = [(k, b) for k, b in active if k not in finished]
        running = {k: b for k, b in running.items() if k not in finished}
    for q in queue:
        result.setdefault(q.key, (None, None))
    return result


def _dependency_met(
    item: PlanItem, t: float, result: dict, running: dict, pending: list[PlanItem]
) -> bool:
    if item.after is None:
        return True
    if item.after in result:
        end = result[item.after][1]
        return end is not None and end <= t
    if item.after in running:
        return False  # still running at t (finished ones were dropped)
    # Not running and not waiting: it already finished before the plan began.
    return all(p.key != item.after for p in pending)


# ---------------------------------------------------------------------------
# CPU numbering
# ---------------------------------------------------------------------------


def _siblings(cpu: int) -> list[int]:
    path = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list")
    try:
        text = path.read_text().strip()
    except OSError:
        return [cpu]
    out: list[int] = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def cpu_order() -> list[int]:
    """CPUs this process may use, one thread per physical core first.

    Jobs given CPUs in this order land on separate physical cores before
    sharing a core's hyperthreads, which is noticeably faster for MD codes.
    """
    try:
        allowed = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        allowed = list(range(os.cpu_count() or 1))
    first, rest = [], []
    for cpu in allowed:
        siblings = [c for c in _siblings(cpu) if c in allowed]
        (first if cpu == min(siblings or [cpu]) else rest).append(cpu)
    return first + rest


def usable_cpus(budget: int) -> list[int]:
    order = cpu_order()
    return order[:budget] if 0 < budget < len(order) else order


def allocate_cpus(usable: list[int], used: set[int], count: int) -> list[int] | None:
    free = [c for c in usable if c not in used]
    return free[:count] if len(free) >= count else None


def cpu_list_text(cpus: list[int]) -> str:
    """``[0, 1, 2, 5]`` → ``"0-2,5"`` (the form taskset and humans read)."""
    parts = []
    cpus = sorted(cpus)
    i = 0
    while i < len(cpus):
        j = i
        while j + 1 < len(cpus) and cpus[j + 1] == cpus[j] + 1:
            j += 1
        parts.append(str(cpus[i]) if i == j else f"{cpus[i]}-{cpus[j]}")
        i = j + 1
    return ",".join(parts)


def parse_cpu_list(text: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"[,\s]+", text.strip()):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out
