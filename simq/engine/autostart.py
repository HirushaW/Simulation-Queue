"""Start the queue service automatically, as a systemd user service."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "simq.service"


def unit_path() -> Path:
    return Path.home() / ".config/systemd/user" / UNIT_NAME


def unit_text(python: str | None = None) -> str:
    python = python or sys.executable
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return (
        "[Unit]\n"
        "Description=SimQ simulation queue\n"
        "After=default.target\n\n"
        "[Service]\n"
        f"ExecStart={python} -m simq service run\n"
        f"Environment=PATH={path}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        # Jobs must outlive a service restart: only stop the service process itself.
        "KillMode=process\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )


def enable() -> str:
    """Install and enable the unit. Returns a message for the user."""
    if not shutil.which("systemctl"):
        return "systemd was not found, so SimQ cannot start itself at boot on this system."
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text())
    for args in (("daemon-reload",), ("enable", UNIT_NAME)):
        result = _systemctl(*args)
        if result.returncode != 0:
            return f"systemctl {' '.join(args)} failed: {result.stderr.strip()}"
    linger = (
        subprocess.run(
            ["loginctl", "enable-linger", getpass.getuser()],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if shutil.which("loginctl")
        else None
    )
    if linger is not None and linger.returncode == 0:
        return "The queue service now starts when the computer boots."
    return (
        "The queue service now starts when you log in. To start it at boot without "
        "logging in, run once: sudo loginctl enable-linger " + getpass.getuser()
    )


def disable() -> str:
    if not shutil.which("systemctl"):
        return "systemd was not found."
    _systemctl("disable", UNIT_NAME)
    unit_path().unlink(missing_ok=True)
    _systemctl("daemon-reload")
    return "The queue service no longer starts automatically."


def enabled() -> bool:
    if not shutil.which("systemctl") or not unit_path().exists():
        return False
    return _systemctl("is-enabled", UNIT_NAME).stdout.strip() == "enabled"
