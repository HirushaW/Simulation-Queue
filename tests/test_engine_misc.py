"""Notifications, automatic start, and how jobs are described."""

import time

from simq.db import Database
from simq.engine import autostart, notify
from simq.engine.notify import Notifier, send_email
from simq.present import check_text, gpu_text, queue_plan, status_text, time_left


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None, context=None):
        self.calls = [("connect", host, port)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        FakeSMTP.sent.append(self.calls)

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"], msg["Subject"], msg.get_content().strip()))


def test_send_email_starttls(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    settings = {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "smtp_user": "me@gmail.com",
        "smtp_password": "app-pass",
        "email_to": "me@gmail.com, lab@uni.edu",
    }
    send_email(settings, "SimQ: Done", "It finished.")
    (calls,) = FakeSMTP.sent
    assert calls[0] == ("connect", "smtp.gmail.com", 587)
    assert ("starttls",) in calls
    assert ("login", "me@gmail.com", "app-pass") in calls
    assert calls[-1] == ("send", "me@gmail.com, lab@uni.edu", "SimQ: Done", "It finished.")


def test_send_email_needs_recipient():
    import pytest

    with pytest.raises(ValueError):
        send_email({"smtp_host": "x", "email_to": ""}, "s", "b")


def test_notifier_respects_settings(monkeypatch):
    shown = []
    monkeypatch.setattr(notify, "desktop", lambda title, body: shown.append(title) or True)
    n = Notifier()
    n.send({"notify_desktop": True, "notify_done": False}, "done", "Done: x", "")
    n.send({"notify_desktop": True, "notify_failed": True}, "failed", "Failed: x", "")
    n.send({"notify_desktop": False, "notify_failed": True}, "failed", "Failed: y", "")
    assert shown == ["Failed: x"]


def test_email_failure_is_reported_not_raised(monkeypatch):
    errors = []
    monkeypatch.setattr(notify, "desktop", lambda *a: True)

    def boom(*_a):
        raise OSError("connection refused")

    monkeypatch.setattr(notify, "send_email", boom)
    n = Notifier(on_email_error=errors.append)
    n.send({"email_enabled": True}, "failed", "Failed: x", "")
    deadline = time.time() + 5
    while not errors and time.time() < deadline:
        time.sleep(0.05)
    assert errors == ["connection refused"]


def test_systemd_unit_keeps_jobs_alive():
    text = autostart.unit_text("/opt/venv/bin/python")
    assert "ExecStart=/opt/venv/bin/python -m simq service run" in text
    assert "KillMode=process" in text  # restarting the service must not kill jobs
    assert "WantedBy=default.target" in text


def test_descriptions(tmp_path):
    db = Database()
    folder = str(tmp_path)
    job_id = db.add_job(
        name="a",
        kind="command",
        folder=folder,
        command="true",
        cores=2,
        gpu="auto",
        created=time.time(),
    )
    job = db.job(job_id)
    now = time.time()
    assert gpu_text(job) == "any"
    assert check_text(job) == "–"
    assert status_text(job, now, queue_running=False) == "waiting (queue paused)"
    db.update_job(job_id, check_state="failed")
    assert status_text(db.job(job_id), now, True) == "check failed"
    db.update_job(job_id, check_state="ok", test_state="ok", start_at=now + 3600)
    job = db.job(job_id)
    assert check_text(job) == "✔✔"
    assert status_text(job, now, True).startswith("starts ")
    db.update_job(job_id, start_at=None, est_seconds=5400)
    assert time_left(db.job(job_id)) == "1 h 30 min"
    plan = queue_plan(db, db.jobs(), total_cpus=4, gpus=[0], now=now)
    assert plan[job_id] == (now, now + 5400)
