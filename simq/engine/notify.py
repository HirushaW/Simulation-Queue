"""Notifications: a desktop pop-up and, if set up, an email."""

from __future__ import annotations

import os
import shutil
import smtplib
import ssl
import subprocess
import threading
from email.message import EmailMessage
from typing import Any

KINDS = ("done", "failed", "warnings", "queue_done")


def desktop(title: str, body: str) -> bool:
    exe = shutil.which("notify-send")
    if not exe or not (
        os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    ):
        return False
    try:
        subprocess.run(
            [exe, "--app-name=SimQ", title, body],
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def send_email(settings: dict[str, Any], subject: str, body: str) -> None:
    """Send one email with the SMTP settings. Raises on failure."""
    host = settings.get("smtp_host") or ""
    port = int(settings.get("smtp_port") or 587)
    security = settings.get("smtp_security") or "starttls"
    user = settings.get("smtp_user") or ""
    password = settings.get("smtp_password") or ""
    sender = settings.get("email_from") or user
    recipients = [a.strip() for a in (settings.get("email_to") or "").split(",") if a.strip()]
    if not host or not recipients:
        raise ValueError("Email needs an SMTP server and at least one recipient.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    context = ssl.create_default_context()
    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if user:
            server.login(user, password)
        server.send_message(msg)


class Notifier:
    """Sends a notification if the settings ask for that kind of event."""

    def __init__(self, on_email_error=None) -> None:
        self._on_email_error = on_email_error

    def send(self, settings: dict[str, Any], kind: str, title: str, body: str) -> None:
        if kind in KINDS and not settings.get(f"notify_{kind}", True):
            return
        if settings.get("notify_desktop", True):
            desktop(title, body)
        if settings.get("email_enabled"):
            snapshot = dict(settings)

            def worker() -> None:
                try:
                    send_email(snapshot, f"SimQ: {title}", body)
                except Exception as exc:  # report, never crash the service
                    if self._on_email_error:
                        self._on_email_error(str(exc))

            threading.Thread(target=worker, daemon=True, name="simq-email").start()
