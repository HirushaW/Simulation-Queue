"""The queue database (``~/.simq/simq.db``), shared by the service, window and CLI.

The service is the only process that starts or signals simulations. The
window and the command line add and edit jobs directly, and ask the service
to act (check, test, kill, pause …) by inserting rows into ``requests``.
SQLite in WAL mode lets all of them use the file at the same time.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .paths import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    folder        TEXT NOT NULL,
    choice        TEXT,
    command       TEXT NOT NULL,
    cores         INTEGER NOT NULL,
    gpu           TEXT NOT NULL DEFAULT 'none',
    position      REAL NOT NULL,
    state         TEXT NOT NULL DEFAULT 'queued',
    check_state   TEXT NOT NULL DEFAULT 'none',
    test_state    TEXT NOT NULL DEFAULT 'none',
    retries       INTEGER NOT NULL DEFAULT 0,
    attempt       INTEGER NOT NULL DEFAULT 0,
    after_job     INTEGER,
    start_at      REAL,
    retry_at      REAL,
    manual_hours  REAL,
    created       REAL NOT NULL,
    started       REAL,
    ended         REAL,
    exit_code     INTEGER,
    est_seconds   REAL,
    est_source    TEXT,
    progress      REAL,
    remaining_s   REAL,
    status_text   TEXT,
    meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL,
    purpose       TEXT NOT NULL,           -- job | test
    cores         INTEGER NOT NULL,
    state         TEXT NOT NULL,           -- waiting | running | done | failed | killed
    pid           INTEGER,
    proc_start    REAL,
    cpus          TEXT,
    gpu_index     INTEGER,
    run_dir       TEXT,
    work_dir      TEXT,
    started       REAL,
    ended         REAL,
    exit_code     INTEGER,
    kill_sent     REAL,
    paused        INTEGER NOT NULL DEFAULT 0,
    meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS requests (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   INTEGER,
    action   TEXT NOT NULL,
    arg      TEXT,
    created  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   INTEGER,
    time     REAL NOT NULL,
    level    TEXT NOT NULL,
    text     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS speeds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    time        REAL NOT NULL,
    job_id      INTEGER,
    job_name    TEXT,
    program     TEXT NOT NULL,
    mode        TEXT NOT NULL,             -- cpu | gpu
    cores       INTEGER NOT NULL,
    atoms       INTEGER,
    s_per_step  REAL NOT NULL,
    timestep_fs REAL,
    source      TEXT NOT NULL              -- test | run | corescan
);
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
"""

# Job states.
QUEUED, RUNNING, PAUSED, DONE, FAILED, KILLED, HELD = (
    "queued",
    "running",
    "paused",
    "done",
    "failed",
    "killed",
    "held",
)
FINISHED_STATES = (DONE, FAILED, KILLED)

DEFAULT_SETTINGS: dict[str, Any] = {
    "queue_running": False,
    "verify_mode": "all_first",  # or "as_you_go"
    "cpu_budget": 0,  # 0 = every logical CPU
    "pin_cpus": True,
    "min_free_gb": 20.0,  # don't start a job when its disk has less free
    "critical_free_gb": 5.0,  # pause running jobs below this
    "stall_minutes": 30,
    "gpu_temp_warn": 87,
    "retries_udb": 2,
    "retries_namd": 1,
    "retries_other": 0,
    "test_minutes": 10,
    "notify_desktop": True,
    "notify_done": True,
    "notify_failed": True,
    "notify_warnings": True,
    "notify_queue_done": True,
    "email_enabled": False,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_security": "starttls",  # starttls | ssl | none
    "smtp_user": "",
    "smtp_password": "",
    "email_from": "",
    "email_to": "",
    "start_at_boot": False,
}


def db_path() -> Path:
    return data_dir() / "simq.db"


@dataclass
class Job:
    id: int
    name: str
    kind: str
    folder: str
    choice: str | None
    command: str
    cores: int
    gpu: str
    position: float
    state: str
    check_state: str
    test_state: str
    retries: int
    attempt: int
    after_job: int | None
    start_at: float | None
    retry_at: float | None
    manual_hours: float | None
    created: float
    started: float | None
    ended: float | None
    exit_code: int | None
    est_seconds: float | None
    est_source: str | None
    progress: float | None
    remaining_s: float | None
    status_text: str | None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_gpu(self) -> bool:
        return self.gpu != "none"

    @property
    def finished(self) -> bool:
        return self.state in FINISHED_STATES


@dataclass
class Run:
    id: int
    job_id: int
    purpose: str
    cores: int
    state: str
    pid: int | None
    proc_start: float | None
    cpus: str | None
    gpu_index: int | None
    run_dir: str | None
    work_dir: str | None
    started: float | None
    ended: float | None
    exit_code: int | None
    kill_sent: float | None
    paused: int
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def cpu_list(self) -> list[int]:
        return [int(c) for c in self.cpus.split(",")] if self.cpus else []


def _from_row(cls, row: sqlite3.Row):
    data = dict(row)
    data["meta"] = json.loads(data.get("meta") or "{}")
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


class Database:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self.conn = sqlite3.connect(
            self.path, timeout=15, isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.executescript(SCHEMA)
        if new:
            os.chmod(self.path, 0o600)  # it may hold an email password

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- settings ---------------------------------------------------------------

    def setting(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return DEFAULT_SETTINGS.get(key)
        return json.loads(row["value"])

    def settings(self) -> dict[str, Any]:
        values = dict(DEFAULT_SETTINGS)
        for row in self.conn.execute("SELECT key, value FROM settings"):
            values[row["key"]] = json.loads(row["value"])
        return values

    def set_setting(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # -- jobs -------------------------------------------------------------------

    def add_job(self, **values: Any) -> int:
        values.setdefault("created", time.time())
        values["meta"] = json.dumps(values.get("meta") or {})
        if "position" not in values:
            row = self.conn.execute("SELECT MAX(position) AS p FROM jobs").fetchone()
            values["position"] = (row["p"] or 0) + 1
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.conn.execute(f"INSERT INTO jobs({cols}) VALUES({marks})", tuple(values.values()))
        return int(cur.lastrowid)

    def job(self, job_id: int) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _from_row(Job, row) if row else None

    def jobs(self) -> list[Job]:
        rows = self.conn.execute("SELECT * FROM jobs ORDER BY position, id").fetchall()
        return [_from_row(Job, r) for r in rows]

    def update_job(self, job_id: int, **values: Any) -> None:
        if not values:
            return
        if "meta" in values:
            values["meta"] = json.dumps(values["meta"])
        sets = ", ".join(f"{k}=?" for k in values)
        self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*values.values(), job_id))

    def merge_meta(self, job_id: int, **values: Any) -> dict[str, Any]:
        with self.transaction():
            row = self.conn.execute("SELECT meta FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return {}
            meta = json.loads(row["meta"] or "{}")
            for key, value in values.items():
                if value is None:
                    meta.pop(key, None)
                else:
                    meta[key] = value
            self.conn.execute("UPDATE jobs SET meta=? WHERE id=?", (json.dumps(meta), job_id))
            return meta

    def delete_job(self, job_id: int) -> None:
        with self.transaction():
            self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self.conn.execute("DELETE FROM requests WHERE job_id=?", (job_id,))
            self.conn.execute("UPDATE jobs SET after_job=NULL WHERE after_job=?", (job_id,))

    # -- runs -------------------------------------------------------------------

    def add_run(self, job_id: int, purpose: str, cores: int, meta: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(job_id, purpose, cores, state, meta) VALUES(?, ?, ?, 'waiting', ?)",
            (job_id, purpose, cores, json.dumps(meta or {})),
        )
        return int(cur.lastrowid)

    def run(self, run_id: int) -> Run | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _from_row(Run, row) if row else None

    def runs(self, *states: str) -> list[Run]:
        if states:
            marks = ", ".join("?" for _ in states)
            rows = self.conn.execute(
                f"SELECT * FROM runs WHERE state IN ({marks}) ORDER BY id", states
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
        return [_from_row(Run, r) for r in rows]

    def job_runs(self, job_id: int) -> list[Run]:
        rows = self.conn.execute("SELECT * FROM runs WHERE job_id=? ORDER BY id",
                                 (job_id,)).fetchall()  # fmt: skip
        return [_from_row(Run, r) for r in rows]

    def update_run(self, run_id: int, **values: Any) -> None:
        if "meta" in values:
            values["meta"] = json.dumps(values["meta"])
        sets = ", ".join(f"{k}=?" for k in values)
        self.conn.execute(f"UPDATE runs SET {sets} WHERE id=?", (*values.values(), run_id))

    # -- requests ---------------------------------------------------------------

    def request(self, action: str, job_id: int | None = None, arg: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO requests(job_id, action, arg, created) VALUES(?, ?, ?, ?)",
            (job_id, action, json.dumps(arg), time.time()),
        )

    def take_requests(self) -> list[tuple[int | None, str, Any]]:
        with self.transaction():
            rows = self.conn.execute("SELECT * FROM requests ORDER BY id").fetchall()
            self.conn.execute("DELETE FROM requests")
        return [(r["job_id"], r["action"], json.loads(r["arg"] or "null")) for r in rows]

    # -- events -----------------------------------------------------------------

    def event(self, job_id: int | None, level: str, text: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO events(job_id, time, level, text) VALUES(?, ?, ?, ?)",
            (job_id, time.time(), level, text),
        )
        return int(cur.lastrowid)

    def events(
        self, job_id: int | None = None, limit: int = 200, after_id: int = 0
    ) -> list[sqlite3.Row]:
        if job_id is None:
            sql = "SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?"
            args: tuple = (after_id, limit)
        else:
            sql = "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id DESC LIMIT ?"
            args = (job_id, after_id, limit)
        return list(reversed(self.conn.execute(sql, args).fetchall()))

    # -- speed memory -----------------------------------------------------------

    def add_speed(self, **values: Any) -> None:
        values.setdefault("time", time.time())
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self.conn.execute(f"INSERT INTO speeds({cols}) VALUES({marks})", tuple(values.values()))

    def speeds(self, program: str | None = None) -> list[sqlite3.Row]:
        if program is None:
            return self.conn.execute("SELECT * FROM speeds ORDER BY time DESC").fetchall()
        return self.conn.execute(
            "SELECT * FROM speeds WHERE program=? ORDER BY time DESC", (program,)
        ).fetchall()

    def delete_speed(self, speed_id: int) -> None:
        self.conn.execute("DELETE FROM speeds WHERE id=?", (speed_id,))
