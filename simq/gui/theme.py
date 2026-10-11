"""Colours and the application stylesheet (one dark theme)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QPolygonF
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
QDialog {{ background: {SURFACE}; }}

/* Tables */
QTableView {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
    gridline-color: transparent;
    selection-background-color: #23304d;
    selection-color: {TEXT};
    outline: none;
}}
QTableView::item {{ padding: 4px 6px; border-bottom: 1px solid {SURFACE_ALT}; }}
QTableView::item:selected {{ background: #23304d; }}
QTableCornerButton::section {{ background: {SURFACE}; border: none; }}

/* Tabs */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 12px;
    background: {SURFACE};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {MUTED};
    padding: 7px 16px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

/* Inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit, QPlainTextEdit {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QDateTimeEdit:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled, QDoubleSpinBox:disabled,
QDateTimeEdit:disabled {{ color: {FAINT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    selection-background-color: #23304d;
    outline: none;
}}
QPlainTextEdit#Log {{
    font-family: "DejaVu Sans Mono", "Ubuntu Mono", monospace;
    font-size: 9pt;
    background: {BG};
}}
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {FAINT};
    background: {BG};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QSplitter::handle {{ background: transparent; }}
#SectionTitle {{ font-size: 11pt; font-weight: 600; margin-top: 6px; }}
#Small {{ color: {MUTED}; font-size: 9pt; }}
#Status {{ font-size: 9pt; }}
QPushButton#Danger {{ color: {BAD}; }}
QPushButton#Danger:hover {{ border-color: {BAD}; }}
QPushButton#Danger:disabled {{ color: {FAINT}; border-color: {BORDER}; }}
QTreeView::indicator, QTableView::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {FAINT};
    border-radius: 4px;
    background: {BG};
}}
QTreeView::indicator:checked, QTableView::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QPushButton#Toolbar {{ padding: 5px 11px; }}
"""


def _arrow_images() -> tuple[str, str]:
    """Small up/down arrows for spin boxes and drop-downs, drawn once per user."""
    folder = Path(tempfile.gettempdir()) / f"simq-theme-{os.getuid()}"
    folder.mkdir(exist_ok=True)
    paths = []
    for name, points in (("up", ((1, 9), (8, 2), (15, 9))), ("down", ((1, 2), (8, 9), (15, 2)))):
        image = QImage(16, 11, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(MUTED))
        painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
        painter.end()
        path = folder / f"{name}.png"
        image.save(str(path))
        paths.append(path.as_posix())
    return paths[0], paths[1]


def _input_arrows() -> str:
    up, down = _arrow_images()
    return f"""
QSpinBox, QDoubleSpinBox {{ padding-right: 22px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QDateTimeEdit::up-button {{
    subcontrol-origin: border; subcontrol-position: top right;
    width: 20px; border: none; background: transparent;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button, QDateTimeEdit::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 20px; border: none; background: transparent;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QDateTimeEdit::up-arrow {{
    image: url({up}); width: 8px; height: 6px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QDateTimeEdit::down-arrow {{
    image: url({down}); width: 8px; height: 6px;
}}
QComboBox::drop-down, QDateTimeEdit::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 24px; border: none;
}}
QComboBox::down-arrow, QDateTimeEdit::down-arrow {{
    image: url({down}); width: 9px; height: 6px;
}}
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
    app.setStyleSheet(STYLESHEET + _input_arrows())
    font = QFont(app.font())
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
