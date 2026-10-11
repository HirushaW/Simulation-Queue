from simq.engine.scheduler import (
    Busy,
    PlanItem,
    Want,
    allocate_cpus,
    choose,
    cpu_list_text,
    parse_cpu_list,
    plan,
)


def W(key, cpus, gpu="none", est=None):  # noqa: N802
    return Want(key, cpus, gpu, est)


def keys(started):
    return [w.key for w, _ in started]


def test_in_order_while_they_fit():
    assert keys(choose(0, 16, [], [], [W("a", 8), W("b", 8), W("c", 1)])) == ["a", "b"]


def test_small_job_uses_spare_cpus_but_never_delays_the_blocked_job():
    busy = [Busy(48, 0, 100.0)]  # a 48-CPU GPU job ends at t=100 on a 64-CPU machine
    # B (48 CPUs) is blocked. At t=100, 64-48 = 16 CPUs stay spare: C (8) may run.
    assert keys(choose(0, 64, [0], busy, [W("B", 48), W("C", 8), W("D", 16)])) == ["C"]
    # D alone needs 16 = all spare CPUs: allowed.
    assert keys(choose(0, 64, [0], busy, [W("B", 48), W("D", 16)])) == ["D"]
    # E would eat into B's CPUs, but its estimate says it ends before B could start.
    busy2 = [Busy(56, None, 100.0)]
    assert keys(choose(0, 64, [], busy2, [W("B", 48), W("E", 8, est=50)])) == ["E"]
    assert keys(choose(0, 64, [], busy2, [W("B", 48), W("E", 12, est=50)])) == []
    # Without an estimate, E must stay out of B's way.
    busy3 = [Busy(60, None, 100.0)]
    assert keys(choose(0, 64, [], busy3, [W("B", 64), W("E", 2)])) == []


def test_gpu_rules():
    # One GPU, busy: the GPU job waits, a CPU job fits next to it.
    busy = [Busy(2, 0, None)]
    started = choose(0, 32, [0], busy, [W("g", 2, "auto"), W("c", 8)])
    assert keys(started) == ["c"]
    # Two GPUs: auto takes the free one; a specific GPU waits for its own.
    started = choose(0, 32, [0, 1], busy, [W("x", 2, "auto"), W("y", 2, "0")])
    assert [(w.key, g) for w, g in started] == [("x", 1)]


def test_impossible_jobs_never_block():
    started = choose(0, 8, [], [], [W("huge", 64), W("gpu", 1, "auto"), W("ok", 2)])
    assert keys(started) == ["ok"]


def test_plan_predicts_start_and_end():
    running = {"A": Busy(48, 0, 100.0)}
    queue = [
        PlanItem("B", 48, "none", 200.0),
        PlanItem("C", 8, "none", None),
        PlanItem("E", 4, "none", 10.0, after="B"),
        PlanItem("L", 2, "none", 5.0, not_before=1000.0),
        PlanItem("X", 2, "none", 5.0, ready=False),
    ]
    result = plan(0, 64, [0], running, queue)
    assert result["C"] == (0, None)
    assert result["B"] == (100.0, 300.0)
    assert result["E"] == (300.0, 310.0)
    assert result["L"] == (1000.0, 1005.0)
    assert result["X"] == (None, None)


def test_cpu_helpers():
    assert cpu_list_text([0, 1, 2, 5, 7, 8]) == "0-2,5,7-8"
    assert parse_cpu_list("0-2,5,7-8") == [0, 1, 2, 5, 7, 8]
    assert allocate_cpus([0, 1, 2, 3], {1}, 2) == [0, 2]
    assert allocate_cpus([0, 1, 2, 3], {0, 1, 2}, 2) is None
