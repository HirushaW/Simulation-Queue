"""Colours and the application stylesheet (one dark theme)."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

BG = "#0e1116"
SIDEBAR = "#0a0c10"
SURFACE = "#151922"
SURFACE_ALT = "#1b202a"
BORDER = "#252b37"
TRACK = "#232936"
TEXT = "#e7eaf0"
MUTED = "#8b93a6"
FAINT = "#5a6273"
ACCENT = "#5b8cff"

CPU = "#5b8cff"
MEMORY = "#a274ff"
GPU = "#2fd08f"
VRAM = "#26c2d9"
POWER = "#ff9f43"
DISK = "#f2b13b"

OK = "#2fd08f"
WARN = "#f2b13b"
BAD = "#ff5d6c"


def level_color(
    percent: float | None, warn: float = 80.0, bad: float = 92.0, base: str = ACCENT
) -> str:
    """``base`` normally, amber above ``warn``, red above ``bad``."""
    if percent is None:
        return FAINT
    if percent >= bad:
        return BAD
    if percent >= warn:
        return WARN
    return base


def temperature_color(celsius: float | None) -> str:
    if celsius is None:
        return MUTED
    if celsius >= 85:
        return BAD
    if celsius >= 75:
        return WARN
    return TEXT


STYLESHEET = f"""
QWidget {{
    color: {TEXT};
    font-size: 10pt;
}}
QMainWindow, #Page, QScrollArea, QScrollArea > QWidget > QWidget {{
    background: {BG};
}}
QToolTip {{
    background: {SURFACE_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 6px;
}}

/* Sidebar */
#Sidebar {{
    background: {SIDEBAR};
    border-right: 1px solid {BORDER};
}}
#AppName {{ font-size: 17pt; font-weight: 700; }}
#AppTagline {{ color: {MUTED}; font-size: 9pt; }}
#Version {{ color: {FAINT}; font-size: 8.5pt; }}
#NavButton {{
    text-align: left;
    padding: 9px 14px;
    border: none;
    border-radius: 8px;
    background: transparent;
    color: {MUTED};
    font-size: 10.5pt;
}}
#NavButton:hover:!checked:enabled {{ background: {SURFACE}; color: {TEXT}; }}
#NavButton:checked {{ background: {SURFACE_ALT}; color: {TEXT}; font-weight: 600; }}
#NavButton:disabled {{ color: {FAINT}; }}

/* Pages */
#PageTitle {{ font-size: 18pt; font-weight: 700; }}
#PageSubtitle {{ color: {MUTED}; }}

/* Cards */
#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
#CardTitle {{ font-size: 11pt; font-weight: 600; }}
#CardSubtitle {{ color: {MUTED}; font-size: 9pt; }}
#Muted {{ color: {MUTED}; }}
#Faint {{ color: {FAINT}; font-size: 9pt; }}
#StatValue {{ font-size: 12pt; font-weight: 600; }}
#StatLabel {{ color: {MUTED}; font-size: 8.5pt; }}
#HostName {{ font-size: 20pt; font-weight: 700; }}
#Chip {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 11px;
    padding: 3px 10px;
    color: {TEXT};
    font-size: 9pt;
}}

/* Buttons */
QPushButton {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 6px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {FAINT}; border-color: {BORDER}; }}
QPushButton#Primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white;
                       font-weight: 600; }}
QPushButton#Primary:hover {{ background: #6d99ff; }}
QPushButton#Primary:disabled {{ background: {SURFACE_ALT}; color: {FAINT};
                                border-color: {BORDER}; }}

/* Tree (Software page) */
QTreeWidget {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
    padding: 6px;
    alternate-background-color: {SURFACE_ALT};
    outline: none;
}}
QTreeWidget::item {{ padding: 5px 4px; border: none; }}
QTreeWidget::item:selected {{ background: #23304d; color: {TEXT}; }}
QHeaderView::section {{
    background: {SURFACE};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 4px;
    font-size: 9pt;
    font-weight: 600;
}}

/* Scroll bars */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {BORDER}; border-radius: 4px; min-height: 30px;
                      min-width: 30px; }}
QScrollBar::handle:hover {{ background: {FAINT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{ background: {SURFACE_ALT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 5px; }}
QMenu::item:selected {{ background: #23304d; }}
QMessageBox {{ background: {SURFACE}; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: SURFACE,
        QPalette.ColorRole.AlternateBase: SURFACE_ALT,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: SURFACE_ALT,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: SURFACE_ALT,
        QPalette.ColorRole.ToolTipText: TEXT,
        QPalette.ColorRole.PlaceholderText: FAINT,
        QPalette.ColorRole.Link: ACCENT,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(FAINT))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(FAINT))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(FAINT))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
    font = QFont(app.font())
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
