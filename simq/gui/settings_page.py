"""The Settings page."""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..db import Database
from ..engine import autostart, client
from ..engine.scheduler import cpu_order
from . import theme
from .widgets import Card, label


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setHorizontalSpacing(18)
    form.setVerticalSpacing(10)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return form


def _spin(lo: int, hi: int, suffix: str = "") -> QSpinBox:
    box = QSpinBox()
    box.setRange(lo, hi)
    box.setSuffix(suffix)
    box.setMinimumWidth(120)
    box.setMaximumWidth(160)
    return box


def _dspin(lo: float, hi: float, suffix: str = "", decimals: int = 1) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(lo, hi)
    box.setDecimals(decimals)
    box.setSuffix(suffix)
    box.setMinimumWidth(120)
    box.setMaximumWidth(160)
    return box


class SettingsPage(QScrollArea):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        page.setObjectName("Page")
        self.setWidget(page)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 28)
        lay.setSpacing(14)

        title = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(label("Settings", "PageTitle"))
        titles.addWidget(label("Saved for this computer in ~/.simq.", "PageSubtitle"))
        title.addLayout(titles, 1)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("Primary")
        title.addWidget(self.save_button, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(title)

        logical = len(cpu_order())
        # Queue
        card = Card("Queue", "How the queue shares this machine")
        form = _form()
        self.cpu_budget = _spin(1, logical)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self.cpu_budget)
        row.addWidget(
            label(
                f"of {logical} logical CPUs. Keep one or two free if you also work "
                "on this machine.",
                "Small",
            )
        )
        row.addStretch(1)
        form.addRow("CPUs for the queue", row)
        self.pin = QCheckBox("Pin each job to its own CPUs (faster; jobs never share a core)")
        form.addRow("", self.pin)
        self.verify = QComboBox()
        self.verify.setMaximumWidth(520)
        self.verify.addItem("Verify all first: check every job as soon as it is added", "all_first")
        self.verify.addItem("Verify as you go: check each job when its turn comes", "as_you_go")
        form.addRow("Verification", self.verify)
        self.test_minutes = _spin(1, 240, " min")
        form.addRow("Stop a test run after", self.test_minutes)
        card.body.addLayout(form)
        lay.addWidget(card)

        # Safety
        card = Card("Health watch", "Warnings and protection while jobs run")
        form = _form()
        self.min_free = _dspin(0, 100000, " GB")
        form.addRow("Start a job only with", _with_note(self.min_free, "free on its disk"))
        self.critical_free = _dspin(0, 100000, " GB")
        form.addRow(
            "Pause running jobs below",
            _with_note(self.critical_free, "free (they resume by themselves once space is freed)"),
        )
        self.stall = _spin(1, 10000, " min")
        form.addRow("Warn when a job writes nothing for", self.stall)
        self.gpu_temp = _spin(0, 120, " °C")
        self.gpu_temp.setSpecialValueText("never")
        form.addRow("Warn when a GPU reaches", self.gpu_temp)
        card.body.addLayout(form)
        lay.addWidget(card)

        # Recovery
        card = Card("Crash recovery", "Automatic retries for new jobs (each job can override)")
        form = _form()
        self.retries_udb = _spin(0, 10)
        form.addRow(
            "UDB packages",
            _with_note(self.retries_udb, "retries · continue from the last finished stage"),
        )
        self.retries_namd = _spin(0, 10)
        form.addRow("NAMD configs", _with_note(self.retries_namd, "retries · run again"))
        self.retries_other = _spin(0, 10)
        form.addRow("Other commands", _with_note(self.retries_other, "retries · run again"))
        card.body.addLayout(form)
        lay.addWidget(card)

        # Notifications
        card = Card("Notifications", "A desktop pop-up and, if you like, an email")
        form = _form()
        self.notify_desktop = QCheckBox("Show desktop pop-ups")
        form.addRow("", self.notify_desktop)
        events = QHBoxLayout()
        events.setSpacing(22)
        self.notify_done = QCheckBox("job done")
        self.notify_failed = QCheckBox("job failed")
        self.notify_warnings = QCheckBox("warnings")
        self.notify_queue_done = QCheckBox("queue finished")
        for box in (
            self.notify_done,
            self.notify_failed,
            self.notify_warnings,
            self.notify_queue_done,
        ):
            events.addWidget(box)
        events.addStretch(1)
        form.addRow("Tell me when", events)
        self.email_enabled = QCheckBox("Also send these by email")
        form.addRow("", self.email_enabled)
        self.smtp_host = QLineEdit()
        self.smtp_port = _spin(1, 65535)
        server = QHBoxLayout()
        server.addWidget(self.smtp_host, 1)
        server.addWidget(label("port"))
        server.addWidget(self.smtp_port)
        form.addRow("SMTP server", server)
        self.smtp_security = QComboBox()
        self.smtp_security.setMaximumWidth(260)
        self.smtp_security.addItem("STARTTLS (port 587)", "starttls")
        self.smtp_security.addItem("SSL (port 465)", "ssl")
        self.smtp_security.addItem("None", "none")
        form.addRow("Security", self.smtp_security)
        self.smtp_user = QLineEdit()
        form.addRow("Username", self.smtp_user)
        self.smtp_password = QLineEdit()
        self.smtp_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password", self.smtp_password)
        self.email_from = QLineEdit()
        self.email_from.setPlaceholderText("defaults to the username")
        form.addRow("From", self.email_from)
        self.email_to = QLineEdit()
        self.email_to.setPlaceholderText("you@example.com (several: separate with commas)")
        form.addRow("To", self.email_to)
        form.addRow(
            "",
            label(
                "Gmail: server smtp.gmail.com, port 587, STARTTLS, your Gmail address as the "
                "username, and an App Password as the password (Google Account → Security → "
                "2-Step Verification → App passwords). Your normal password will not work. The "
                "password is stored only on this computer, readable only by you.",
                "Small",
                wrap=True,
            ),
        )
        test_row = QHBoxLayout()
        self.test_email = QPushButton("Send a test email")
        self.email_status = label("", "Small")
        test_row.addWidget(self.test_email)
        test_row.addWidget(self.email_status, 1)
        form.addRow("", test_row)
        card.body.addLayout(form)
        lay.addWidget(card)

        # Service
        card = Card("Queue service", "The background process that runs the queue")
        form = _form()
        self.service_status = label("", "Muted")
        restart = QPushButton("Restart service")
        row = QHBoxLayout()
        row.addWidget(self.service_status, 1)
        row.addWidget(restart)
        form.addRow("Status", row)
        self.autostart = QCheckBox("Start the queue service automatically (systemd user service)")
        form.addRow("", self.autostart)
        form.addRow(
            "",
            label(
                "Closing this window never stops your jobs. With automatic start, "
                "the queue also carries on after a reboot, and crashed jobs are "
                "recovered without opening SimQ.",
                "Small",
                wrap=True,
            ),
        )
        card.body.addLayout(form)
        lay.addWidget(card)
        lay.addStretch(1)

        self.save_button.clicked.connect(self.save)
        self.test_email.clicked.connect(self._test_email)
        restart.clicked.connect(self._restart_service)
        self.autostart.toggled.connect(self._autostart_toggled)
        self._email_poll = QTimer(self)
        self._email_poll.timeout.connect(self._check_email_result)
        self._email_sent_at = 0.0
        self._loading = False
        self.load()

    # -- load / save ------------------------------------------------------------

    def load(self) -> None:
        self._loading = True
        s = self.db.settings()
        logical = len(cpu_order())
        self.cpu_budget.setValue(int(s["cpu_budget"]) or logical)
        self.pin.setChecked(bool(s["pin_cpus"]))
        self.verify.setCurrentIndex(max(0, self.verify.findData(s["verify_mode"])))
        self.test_minutes.setValue(int(s["test_minutes"]))
        self.min_free.setValue(float(s["min_free_gb"]))
        self.critical_free.setValue(float(s["critical_free_gb"]))
        self.stall.setValue(int(s["stall_minutes"]))
        self.gpu_temp.setValue(int(s["gpu_temp_warn"]))
        self.retries_udb.setValue(int(s["retries_udb"]))
        self.retries_namd.setValue(int(s["retries_namd"]))
        self.retries_other.setValue(int(s["retries_other"]))
        self.notify_desktop.setChecked(bool(s["notify_desktop"]))
        self.notify_done.setChecked(bool(s["notify_done"]))
        self.notify_failed.setChecked(bool(s["notify_failed"]))
        self.notify_warnings.setChecked(bool(s["notify_warnings"]))
        self.notify_queue_done.setChecked(bool(s["notify_queue_done"]))
        self.email_enabled.setChecked(bool(s["email_enabled"]))
        self.smtp_host.setText(s["smtp_host"])
        self.smtp_port.setValue(int(s["smtp_port"]))
        self.smtp_security.setCurrentIndex(max(0, self.smtp_security.findData(s["smtp_security"])))
        self.smtp_user.setText(s["smtp_user"])
        self.smtp_password.setText(s["smtp_password"])
        self.email_from.setText(s["email_from"])
        self.email_to.setText(s["email_to"])
        self.autostart.setChecked(autostart.enabled())
        self._loading = False
        self.refresh()

    def save(self) -> None:
        logical = len(cpu_order())
        budget = self.cpu_budget.value()
        values = {
            "cpu_budget": 0 if budget >= logical else budget,
            "pin_cpus": self.pin.isChecked(),
            "verify_mode": self.verify.currentData(),
            "test_minutes": self.test_minutes.value(),
            "min_free_gb": self.min_free.value(),
            "critical_free_gb": self.critical_free.value(),
            "stall_minutes": self.stall.value(),
            "gpu_temp_warn": self.gpu_temp.value(),
            "retries_udb": self.retries_udb.value(),
            "retries_namd": self.retries_namd.value(),
            "retries_other": self.retries_other.value(),
            "notify_desktop": self.notify_desktop.isChecked(),
            "notify_done": self.notify_done.isChecked(),
            "notify_failed": self.notify_failed.isChecked(),
            "notify_warnings": self.notify_warnings.isChecked(),
            "notify_queue_done": self.notify_queue_done.isChecked(),
            "email_enabled": self.email_enabled.isChecked(),
            "smtp_host": self.smtp_host.text().strip(),
            "smtp_port": self.smtp_port.value(),
            "smtp_security": self.smtp_security.currentData(),
            "smtp_user": self.smtp_user.text().strip(),
            "smtp_password": self.smtp_password.text(),
            "email_from": self.email_from.text().strip(),
            "email_to": self.email_to.text().strip(),
        }
        with self.db.transaction():
            for key, value in values.items():
                self.db.set_setting(key, value)
        self.save_button.setText("Saved ✔")
        QTimer.singleShot(1500, lambda: self.save_button.setText("Save"))

    def refresh(self) -> None:
        status = client.service_status(self.db)
        if status["running"]:
            self.service_status.setText(f"● running (pid {status['pid']})")
            self.service_status.setStyleSheet(f"color: {theme.OK};")
        else:
            self.service_status.setText("● not running")
            self.service_status.setStyleSheet(f"color: {theme.BAD};")

    # -- actions ------------------------------------------------------------------

    def _test_email(self) -> None:
        self.save()
        if not self.email_to.text().strip() or not self.smtp_host.text().strip():
            self.email_status.setText("Fill in the SMTP server and the To address first.")
            return
        if not client.service_status(self.db)["running"]:
            self.email_status.setText("The queue service is not running; it sends the emails.")
            return
        self._email_sent_at = time.time()
        self.db.request("test_email")
        self.email_status.setText("Sending…")
        self.email_status.setStyleSheet("")
        self._email_poll.start(500)

    def _check_email_result(self) -> None:
        for event in reversed(self.db.events(limit=20)):
            if event["time"] < self._email_sent_at - 1:
                continue
            if event["text"].startswith("Test email"):
                ok = event["level"] == "info"
                self.email_status.setText(event["text"])
                self.email_status.setStyleSheet(f"color: {theme.OK if ok else theme.BAD};")
                self._email_poll.stop()
                return
        if time.time() - self._email_sent_at > 60:
            self._email_poll.stop()
            self.email_status.setText("No answer from the mail server after a minute.")

    def _restart_service(self) -> None:
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            client.stop_service(self.db)
            ok = client.start_service(db=self.db)
        finally:
            QGuiApplication.restoreOverrideCursor()
        if not ok:
            QMessageBox.warning(
                self,
                "Service",
                f"The service did not start. See {os.path.expanduser('~/.simq/service.log')}.",
            )
        self.refresh()

    def _autostart_toggled(self, on: bool) -> None:
        if self._loading:
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            message = autostart.enable() if on else autostart.disable()
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.db.set_setting("start_at_boot", on)
        QMessageBox.information(self, "Automatic start", message)
        self._loading = True
        self.autostart.setChecked(autostart.enabled())
        self._loading = False


def _with_note(widget: QWidget, note: str) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(12)
    row.addWidget(widget)
    row.addWidget(label(note, "Small"))
    row.addStretch(1)
    return row
