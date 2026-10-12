"""The queue service end to end: real processes, the real UDB scripts, fake NAMD/tleap."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import psutil
import pytest
from helpers import install_fakes, make_service, make_udb_package, run_until

from simq.db import Database
from simq.detect.software import DetectionReport
from simq.engine import client, launch
from simq.engine import service as service_module
from simq.engine.check import main_program

REPORT = DetectionReport()


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    bin_dir = install_fakes(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(service_module, "RETRY_DELAY_S", 0)
    return bin_dir


@pytest.fixture
def svc(fakes):
    service = make_service()
    service.db.set_setting("cpu_budget", 2)
    yield service
    for run in service.db.runs("running"):
        launch.force_stop(run.pid)


def script_job(db: Database, folder: Path, body: str, **values) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "run.sh").write_text(body)
    return client.add_job(db, folder, REPORT, **values)


def state(db: Database, job_id: int) -> str:
    return db.job(job_id).state


def events(db: Database, job_id: int) -> str:
    return "\n".join(e["text"] for e in db.events(job_id))


# ---------------------------------------------------------------------------
# Generic jobs
# ---------------------------------------------------------------------------


def test_jobs_share_cpus_each_pinned_to_its_own(svc, tmp_path):
    db = svc.db
    a = script_job(db, tmp_path / "a", "taskset -cp $$ > cpus.txt; sleep 2\n", cores=1)
    b = script_job(db, tmp_path / "b", "taskset -cp $$ > cpus.txt; sleep 2\n", cores=1)
    c = script_job(db, tmp_path / "c", "echo $SIMQ_CORES $OMP_NUM_THREADS > env.txt\n", cores=2)
    client.set_queue_running(db, True)
    run_until(svc, lambda: state(db, a) == "running" and state(db, b) == "running")
    assert state(db, c) == "queued"  # needs both CPUs: waits
    run_until(svc, lambda: state(db, c) == "done")
    cpus_a = (tmp_path / "a/cpus.txt").read_text().split(":")[-1].strip()
    cpus_b = (tmp_path / "b/cpus.txt").read_text().split(":")[-1].strip()
    assert cpus_a != cpus_b
    assert (tmp_path / "c/env.txt").read_text().split() == ["2", "2"]
    assert state(db, a) == state(db, b) == "done"


def test_paused_queue_starts_nothing_but_run_now_works(svc, tmp_path):
    db = svc.db
    job = script_job(db, tmp_path / "j", "true\n", cores=1)
    for _ in range(5):
        svc.tick(time.time())
    assert state(db, job) == "queued"
    db.request("run_now", job)
    run_until(svc, lambda: state(db, job) == "done")


def test_failure_is_retried_then_reported(svc, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(svc.notifier, "send", lambda s, kind, title, body: sent.append(kind))
    db = svc.db
    job = script_job(db, tmp_path / "bad", "echo going wrong; exit 3\n", cores=1, retries=1)
    client.set_queue_running(db, True)
    run_until(svc, lambda: state(db, job) == "failed")
    j = db.job(job)
    assert j.attempt == 2 and j.exit_code == 3
    assert "going wrong" in j.meta["failure"]
    assert "Retrying" in events(db, job)
    # One failure notification (not one per attempt), then the queue is empty.
    assert sent == ["failed", "queue_done"]


def test_kill_stops_the_whole_process_group(svc, tmp_path):
    db = svc.db
    job = script_job(db, tmp_path / "k", "sleep 300 & echo $! > child.pid; wait\n", cores=1)
    client.set_queue_running(db, True)
    run_until(svc, lambda: (tmp_path / "k/child.pid").exists())
    child = int((tmp_path / "k/child.pid").read_text())
    db.request("pause", job)
    run_until(svc, lambda: state(db, job) == "paused")
    assert psutil.Process(child).status() == psutil.STATUS_STOPPED
    db.request("resume", job)
    run_until(svc, lambda: state(db, job) == "running")
    db.request("kill", job)
    run_until(svc, lambda: state(db, job) == "killed")
    time.sleep(0.3)
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_dependent_job_is_held_when_its_dependency_fails(svc, tmp_path):
    db = svc.db
    first = script_job(db, tmp_path / "first", "exit 1\n", cores=1, retries=0)
    second = script_job(db, tmp_path / "second", "true\n", cores=1, after_job=first)
    client.set_queue_running(db, True)
    run_until(svc, lambda: state(db, second) == "held")
    assert state(db, first) == "failed"


def test_dependent_job_waits_then_runs(svc, tmp_path):
    db = svc.db
    first = script_job(db, tmp_path / "first", "sleep 1\n", cores=1)
    second = script_job(db, tmp_path / "second", "true\n", cores=1, after_job=first)
    client.set_queue_running(db, True)
    run_until(svc, lambda: state(db, first) == "running")
    assert state(db, second) == "queued"  # a CPU is free, but it must wait
    run_until(svc, lambda: state(db, second) == "done")


def test_delayed_start(svc, tmp_path):
    db = svc.db
    job = script_job(db, tmp_path / "later", "true\n", cores=1, start_at=time.time() + 2)
    client.set_queue_running(db, True)
    svc.tick(time.time())
    assert state(db, job) == "queued"
    run_until(svc, lambda: state(db, job) == "done")
    assert db.job(job).started >= db.job(job).start_at


def test_verify_all_first_checks_on_add_and_failed_check_blocks_start(svc, tmp_path):
    db = svc.db
    good = script_job(db, tmp_path / "good", "true\n", cores=1)
    bad = script_job(db, tmp_path / "bad", "true\n", cores=1, command="no_such_program_xyz")
    svc.tick(time.time())  # queue still paused: checks happen anyway
    assert db.job(good).check_state == "ok"
    assert db.job(bad).check_state == "failed"
    assert "not found" in str(db.job(bad).meta["check"]["findings"])
    client.set_queue_running(db, True)
    run_until(svc, lambda: state(db, good) == "done")
    assert state(db, bad) == "queued"


def test_folder_moved_after_adding_is_caught_before_start(svc, tmp_path):
    db = svc.db
    db.set_setting("verify_mode", "as_you_go")
    job = script_job(db, tmp_path / "moving", "true\n", cores=1)
    (tmp_path / "moving").rename(tmp_path / "moved")
    client.set_queue_running(db, True)
    run_until(svc, lambda: db.job(job).check_state == "failed")
    assert state(db, job) == "queued"
    assert "does not exist" in events(db, job)


def test_too_many_cpus_is_reported_and_does_not_block(svc, tmp_path):
    db = svc.db
    huge = script_job(db, tmp_path / "huge", "true\n", cores=999)
    small = script_job(db, tmp_path / "small", "true\n", cores=1)
    client.set_queue_running(db, True)
    run_until(svc, lambda: state(db, small) == "done")
    assert db.job(huge).check_state == "failed"


def test_jobs_outlive_a_service_restart(fakes, tmp_path):
    first = make_service()
    db = first.db
    job = script_job(db, tmp_path / "long", "sleep 2; echo finished > out.txt\n", cores=1)
    client.set_queue_running(db, True)
    run_until(first, lambda: state(db, job) == "running")
    del first  # the service stops; the job keeps running
    second = make_service()
    second.adopt()
    assert state(second.db, job) == "running"
    run_until(second, lambda: state(second.db, job) == "done")
    assert (tmp_path / "long/out.txt").read_text().strip() == "finished"


def test_job_lost_while_service_was_down_counts_as_interrupted(fakes, tmp_path):
    first = make_service()
    db = first.db
    job = script_job(db, tmp_path / "lost", "sleep 60\n", cores=1, retries=0)
    client.set_queue_running(db, True)
    run_until(first, lambda: state(db, job) == "running")
    run = db.runs("running")[0]
    os.killpg(run.pid, signal.SIGKILL)  # e.g. the machine went down
    del first
    time.sleep(0.3)
    second = make_service()
    second.adopt()
    assert state(second.db, job) == "failed"
    assert "service was not watching" in second.db.job(job).meta["failure"]


def test_stall_warning(svc, tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "HEALTH_EVERY_S", 0.2)
    db = svc.db
    db.set_setting("stall_minutes", 0.02)  # 1.2 s
    job = script_job(db, tmp_path / "quiet", "sleep 5\n", cores=1)
    client.set_queue_running(db, True)
    run_until(svc, lambda: "may be stuck" in events(db, job), timeout=20)
    db.request("kill", job)
    run_until(svc, lambda: state(db, job) == "killed")


def test_disk_guard_pauses_and_resumes(svc, tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "HEALTH_EVERY_S", 0.2)
    db = svc.db
    db.set_setting("critical_free_gb", 1.0)
    free = {"bytes": 100 * 1024**3}
    monkeypatch.setattr(service_module, "free_bytes", lambda _folder: free["bytes"])
    job = script_job(db, tmp_path / "writer", "sleep 30\n", cores=1)
    client.set_queue_running(db, True)
    run_until(svc, lambda: state(db, job) == "running")
    free["bytes"] = 100 * 1024**2
    run_until(svc, lambda: state(db, job) == "paused")
    assert "disk" in db.job(job).status_text
    free["bytes"] = 100 * 1024**3
    run_until(svc, lambda: state(db, job) == "running")
    db.request("kill", job)
    run_until(svc, lambda: state(db, job) == "killed")


def test_queue_finished_notification(svc, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(svc.notifier, "send", lambda s, kind, title, body: sent.append(kind))
    db = svc.db
    script_job(db, tmp_path / "one", "true\n", cores=1)
    client.set_queue_running(db, True)
    run_until(svc, lambda: "queue_done" in sent)
    assert sent == ["done", "queue_done"]


# ---------------------------------------------------------------------------
# UDB packages, with the real run_namd.sh / run_full.sh
# ---------------------------------------------------------------------------


@pytest.fixture
def package(fakes, tmp_path):
    return make_udb_package(tmp_path / "pkg", fakes / "namd3")


def test_udb_check(svc, package):
    db = svc.db
    job = client.add_job(db, package, REPORT, cores=2)
    j = db.job(job)
    assert (j.kind, j.choice, j.command) == ("udb", "run_namd.sh", "bash run_namd.sh {cores}")
    svc.run_check(j)
    j = db.job(job)
    assert j.check_state in ("ok", "warn"), j.meta["check"]
    analysis = j.meta["analysis"]
    assert analysis["total_steps"] == 100 + 400 + 400 + 400 + 800
    assert [s["title"] for s in analysis["stages"]][0] == "Minimisation"


def test_udb_check_catches_missing_namd(svc, package):
    script = package / "run_namd.sh"
    script.write_text(
        script.read_text().replace(str(package.parent / "bin/namd3"), "/nonexistent/namd3")
    )
    job = client.add_job(svc.db, package, REPORT, cores=2)
    svc.run_check(svc.db.job(job))
    j = svc.db.job(job)
    assert j.check_state == "failed"
    assert "/nonexistent/namd3" in str(j.meta["check"]["findings"])


def test_udb_staged_run_crash_and_resume(svc, package, monkeypatch, tmp_path):
    flag = tmp_path / "fail-once"
    flag.touch()
    monkeypatch.setenv("FAKE_NAMD_SPS", "0.003")
    monkeypatch.setenv("FAKE_NAMD_FAIL", "03_nvt_equil:200")
    monkeypatch.setenv("FAKE_NAMD_FAIL_FLAG", str(flag))
    # Production lasts ~2.4 s here, shorter than the normal 3 s progress interval.
    monkeypatch.setattr(service_module, "PROGRESS_EVERY_S", 0)
    db = svc.db
    job = client.add_job(db, package, REPORT, cores=2, retries=1)
    client.set_queue_running(db, True)
    stages_seen = set()

    def finished():
        j = db.job(job)
        if j.status_text and "/" in j.status_text:
            stages_seen.add(j.status_text)
        return j.state in ("done", "failed")

    run_until(svc, finished, timeout=120)
    j = db.job(job)
    assert j.state == "done", events(db, job)
    assert j.attempt == 2
    log = events(db, job)
    assert "Atoms moving too fast" in log
    assert "Stage 3 (NVT equilibration) was interrupted" in log
    aside = list(package.glob("03_nvt_equil.restart.*.simq-interrupted-*"))
    assert len(aside) == 3
    assert (package / "05_production.coor").exists()
    assert any("Production" in s for s in stages_seen)
    assert j.meta["speed"]["s_per_step"] == pytest.approx(0.003)


def test_udb_all_in_one_resume_after_phase_2_crash(svc, package, monkeypatch, tmp_path):
    flag = tmp_path / "fail-once"
    flag.touch()
    monkeypatch.setenv("FAKE_NAMD_SPS", "0.001")
    monkeypatch.setenv("FAKE_NAMD_FAIL", "dendrimer_pamam_gen4_full_p2:600")
    monkeypatch.setenv("FAKE_NAMD_FAIL_FLAG", str(flag))
    db = svc.db
    job = client.add_job(db, package, REPORT, choice="run_full.sh", cores=2, retries=1)
    assert db.job(job).command == "bash run_full.sh"
    client.set_queue_running(db, True)
    run_until(svc, lambda: state(db, job) in ("done", "failed"), timeout=120)
    assert state(db, job) == "done", events(db, job)
    assert "run_full.sh resume" in events(db, job)


def test_udb_test_run_measures_speed_and_leaves_folder_alone(svc, package, monkeypatch):
    monkeypatch.setenv("FAKE_NAMD_SPS", "0.002")
    db = svc.db
    before = sorted(p.name for p in package.iterdir())
    job = client.add_job(db, package, REPORT, cores=2)
    db.request("test", job)
    run_until(svc, lambda: db.job(job).test_state in ("ok", "failed"), timeout=120)
    j = db.job(job)
    assert j.test_state == "ok", events(db, job)
    assert j.meta["speed"] == pytest.approx(
        {"s_per_step": 0.002, "source": "test run", "cores": 2, "time": j.meta["speed"]["time"]}
    )
    assert j.est_source == "test run"
    assert j.est_seconds == pytest.approx(2100 * 0.002)
    assert sorted(p.name for p in package.iterdir()) == before
    assert not any((svc.db.path.parent / "scratch").iterdir())
    speeds = db.speeds("namd")
    assert speeds and speeds[0]["source"] == "test" and speeds[0]["atoms"]


def test_speed_memory_estimates_a_similar_new_job(svc, package, fakes, monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setenv("FAKE_NAMD_SPS", "0.002")
    db = svc.db
    first = client.add_job(db, package, REPORT, cores=2)
    db.request("test", first)
    run_until(svc, lambda: db.job(first).test_state == "ok", timeout=120)
    # The same system in another folder, already solvated: its atom count is
    # known, so the speed memory gives an estimate before any test run.
    other = make_udb_package(tmp_path / "pkg2", fakes / "namd3")
    subprocess.run(["bash", "udb_solvate.sh"], cwd=other, check=True, capture_output=True)
    second = client.add_job(db, other, REPORT, cores=2)
    svc.run_check(db.job(second))
    j = db.job(second)
    assert j.est_source and j.est_source.startswith("similar job"), j.est_source
    assert j.est_seconds == pytest.approx(2100 * 0.002, rel=0.01)


def test_corescan(svc, package, monkeypatch):
    monkeypatch.setenv("FAKE_NAMD_SPS", "0.001")
    db = svc.db
    job = client.add_job(db, package, REPORT, cores=2)
    db.request("corescan", job, [1, 2])
    run_until(
        svc,
        lambda: (
            len((db.job(job).meta.get("corescan") or {}).get("results", {})) == 2
            and db.job(job).test_state != "running"
        ),
        timeout=180,
    )
    results = db.job(job).meta["corescan"]["results"]
    assert set(results) == {"1", "2"} and all(results.values())
    assert "Core-count test finished" in events(db, job)


# ---------------------------------------------------------------------------
# Client operations
# ---------------------------------------------------------------------------


def test_client_operations(svc, tmp_path):
    db = svc.db
    a = script_job(db, tmp_path / "a", "true\n", cores=1)
    b = script_job(db, tmp_path / "b", "true\n", cores=1)
    client.move_job(db, b, -1)
    assert [j.id for j in db.jobs()] == [b, a]
    copy = client.duplicate_job(db, a)
    assert db.job(copy).command == db.job(a).command
    client.hold(db, a)
    assert state(db, a) == "held"
    client.release(db, a)
    assert state(db, a) == "queued"
    client.update_job(db, a, cores=2)
    assert db.job(a).cores == 2 and db.job(a).check_state == "none"
    client.remove_job(db, copy)
    assert db.job(copy) is None


def test_running_job_cannot_be_removed_or_edited(svc, tmp_path):
    db = svc.db
    job = script_job(db, tmp_path / "r", "sleep 30\n", cores=1)
    client.set_queue_running(db, True)
    run_until(svc, lambda: state(db, job) == "running")
    with pytest.raises(ValueError):
        client.remove_job(db, job)
    with pytest.raises(ValueError):
        client.update_job(db, job, cores=2)
    db.request("kill", job)
    run_until(svc, lambda: state(db, job) == "killed")


def test_main_program():
    assert main_program("OMP=4 mpirun -np {cores} pmemd.MPI -O -i a.in")[0] == "pmemd.MPI"
    assert main_program("taskset -c 0-3 namd3 +p4 x.conf")[0] == "namd3"
    assert main_program("bash run_namd.sh 8") == ("bash", ["run_namd.sh", "8"])
