"""Reusable dashboard widgets: cards, animated gauges and bars, history plots."""

from __future__ import annotations

import math
from collections import deque

import pyqtgraph as pg
from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import theme

ANIMATION_MS = 650


def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


class Card(QFrame):
    """A rounded panel with a title row; put content in :attr:`body`."""

    def __init__(self, title: str = "", subtitle: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 18, 16)
        outer.setSpacing(12)

        self.header = QHBoxLayout()
        self.header.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.title_label = label(title, "CardTitle")
        self.subtitle_label = label(subtitle, "CardSubtitle")
        self.subtitle_label.setVisible(bool(subtitle))
        titles.addWidget(self.title_label)
        titles.addWidget(self.subtitle_label)
        self.header.addLayout(titles, 1)
        if title or subtitle:
            outer.addLayout(self.header)

        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body, 1)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))


class Chip(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("Chip")


class Stat(QWidget):
    """A small caption above a value, e.g. ``CLOCK`` / ``3.42 GHz``."""

    def __init__(self, caption: str, value: str = "–", parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)
        self.caption = label(caption.upper(), "StatLabel")
        self.value = label(value, "StatValue")
        lay.addWidget(self.caption)
        lay.addWidget(self.value)

    def set(self, text: str, color: str | None = None) -> None:
        self.value.setText(text)
        self.value.setStyleSheet(f"color: {color};" if color else "")


class LegendDot(QWidget):
    """A coloured dot followed by text, for naming plot series."""

    def __init__(self, color: str, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        lay.addWidget(dot)
        lay.addWidget(label(text, "Faint"))


class _Animated:
    """Mixin: an animated ``_shown`` value that eases toward each new target."""

    def _init_animation(self, start: float = 0.0) -> None:
        self._shown = start
        self._anim = QVariantAnimation(self)  # type: ignore[arg-type]
        self._anim.setDuration(ANIMATION_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    def _animate_to(self, target: float) -> None:
        if math.isclose(target, self._shown, abs_tol=0.05):
            return
        self._anim.stop()
        self._anim.setStartValue(float(self._shown))
        self._anim.setEndValue(float(target))
        self._anim.start()

    def _on_anim(self, value: float) -> None:
        self._shown = float(value)
        self.update()  # type: ignore[attr-defined]


class RingGauge(_Animated, QWidget):
    """A 270° ring showing a percentage, with the number in the middle."""

    def __init__(
        self, color: str, caption: str = "", size: int = 132, parent: QWidget | None = None
    ):
        super().__init__(parent)
        self._color = QColor(color)
        self._caption = caption
        self._value: float | None = None
        self._text = "–"
        self.setFixedSize(size, size)
        self._init_animation()

    def set_value(
        self, percent: float | None, text: str | None = None, color: str | None = None
    ) -> None:
        self._value = percent
        if color:
            self._color = QColor(color)
        self._text = text if text is not None else ("–" if percent is None else f"{percent:.0f}%")
        self._animate_to(0.0 if percent is None else max(0.0, min(100.0, percent)))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height())
        thick = max(8.0, side * 0.085)
        rect = QRectF(thick / 2 + 1, thick / 2 + 1, side - thick - 2, side - thick - 2)
        start, span = 225 * 16, -270 * 16

        pen = QPen(QColor(theme.TRACK), thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, start, span)
        if self._shown > 0.2:
            pen.setColor(self._color)
            p.setPen(pen)
            p.drawArc(rect, start, int(span * self._shown / 100.0))

        p.setPen(QColor(theme.TEXT))
        big = QFont(self.font())
        big.setPointSizeF(side * 0.135)
        big.setWeight(QFont.Weight.DemiBold)
        p.setFont(big)
        center_rect = QRectF(0, side * 0.30, side, side * 0.30)
        p.drawText(center_rect, Qt.AlignmentFlag.AlignCenter, self._text)
        if self._caption:
            small = QFont(self.font())
            small.setPointSizeF(max(7.0, side * 0.068))
            p.setFont(small)
            p.setPen(QColor(theme.MUTED))
            p.drawText(
                QRectF(0, side * 0.58, side, side * 0.14),
                Qt.AlignmentFlag.AlignCenter,
                self._caption,
            )
        p.end()


class MeterBar(_Animated, QWidget):
    """A thin horizontal bar, 0–100 %."""

    def __init__(self, color: str = theme.ACCENT, height: int = 8, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._init_animation()

    def set_value(self, percent: float | None, color: str | None = None) -> None:
        if color:
            self._color = QColor(color)
        self._animate_to(0.0 if percent is None else max(0.0, min(100.0, percent)))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = self.height() / 2
        p.setBrush(QColor(theme.TRACK))
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), r, r)
        w = self.width() * self._shown / 100.0
        if w > 0.5:
            p.setBrush(self._color)
            p.drawRoundedRect(QRectF(0, 0, max(w, self.height()), self.height()), r, r)
        p.end()


class CoreGrid(QWidget):
    """One small vertical bar per logical CPU, wrapping onto several rows."""

    MIN_BAR = 9
    MAX_BAR = 26
    GAP = 4
    BAR_HEIGHT = 46
    ROW_GAP = 10

    def __init__(self, count: int, parent: QWidget | None = None):
        super().__init__(parent)
        self._count = max(1, count)
        self._from = [0.0] * self._count
        self._to = [0.0] * self._count
        self._shown = [0.0] * self._count
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMouseTracking(True)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(ANIMATION_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._on_anim)

    def set_values(self, values: tuple[float, ...] | list[float]) -> None:
        values = list(values)[: self._count] + [0.0] * max(0, self._count - len(values))
        self._from = list(self._shown)
        self._to = [max(0.0, min(100.0, v)) for v in values]
        self._anim.stop()
        self._anim.start()

    def _on_anim(self, t: float) -> None:
        self._shown = [a + (b - a) * t for a, b in zip(self._from, self._to, strict=True)]
        self.update()

    def _columns(self, width: int) -> int:
        return max(1, min(self._count, (width + self.GAP) // (self.MIN_BAR + self.GAP)))

    def _rows(self, width: int) -> int:
        return math.ceil(self._count / self._columns(width))

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        rows = self._rows(width)
        return rows * self.BAR_HEIGHT + (rows - 1) * self.ROW_GAP

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(600, self.heightForWidth(600))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(120, self.BAR_HEIGHT)

    def _geometry(self) -> tuple[int, float]:
        cols = self._columns(self.width())
        bar_w = min(self.MAX_BAR, (self.width() - (cols - 1) * self.GAP) / cols)
        return cols, bar_w

    def _index_at(self, pos: QPointF) -> int | None:
        cols, bar_w = self._geometry()
        col = int(pos.x() // (bar_w + self.GAP))
        row = int(pos.y() // (self.BAR_HEIGHT + self.ROW_GAP))
        index = row * cols + col
        return index if 0 <= col < cols and 0 <= index < self._count else None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._index_at(event.position())
        if index is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(
                event.globalPosition().toPoint(), f"CPU {index}: {self._to[index]:.0f}%", self
            )

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        cols, bar_w = self._geometry()
        radius = min(3.0, bar_w / 2)
        track = QColor(theme.TRACK)
        # One colour: on a simulation workstation a fully busy core is the goal,
        # not a warning.
        bar = QColor(theme.CPU)
        for i, value in enumerate(self._shown):
            row, col = divmod(i, cols)
            x = col * (bar_w + self.GAP)
            y = row * (self.BAR_HEIGHT + self.ROW_GAP)
            p.setBrush(track)
            p.drawRoundedRect(QRectF(x, y, bar_w, self.BAR_HEIGHT), radius, radius)
            h = self.BAR_HEIGHT * value / 100.0
            if h > 0.5:
                p.setBrush(bar)
                p.drawRoundedRect(QRectF(x, y + self.BAR_HEIGHT - h, bar_w, h), radius, radius)
        p.end()


class HistoryPlot(QWidget):
    """The last ``seconds`` samples of one or more 0–100 % series, as filled lines."""

    def __init__(
        self,
        series: list[tuple[str, str]],
        seconds: int = 120,
        height: int = 96,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        caption = QHBoxLayout()
        minutes = seconds // 60
        caption.addWidget(
            label(
                f"LAST {minutes} MINUTES" if minutes > 1 else f"LAST {seconds} SECONDS", "StatLabel"
            )
        )
        caption.addStretch(1)
        caption.addWidget(label("dashed line = 50%", "StatLabel"))
        lay.addLayout(caption)

        self.plot = pg.PlotWidget(background=theme.SURFACE)
        self.plot.setFixedHeight(height)
        self.plot.setCursor(Qt.CursorShape.ArrowCursor)
        lay.addWidget(self.plot)

        self._data = {key: deque(maxlen=seconds) for key, _ in series}
        item = self.plot.getPlotItem()
        item.setMouseEnabled(x=False, y=False)
        item.setMenuEnabled(False)
        item.hideButtons()
        item.hideAxis("bottom")
        item.hideAxis("left")
        item.getViewBox().setDefaultPadding(0)
        item.setContentsMargins(0, 0, 0, 0)
        item.setYRange(0, 100, padding=0.03)
        item.setXRange(-(seconds - 1), 0, padding=0)
        for level, style in (
            (0, Qt.PenStyle.SolidLine),
            (50, Qt.PenStyle.DashLine),
            (100, Qt.PenStyle.SolidLine),
        ):
            item.addItem(
                pg.InfiniteLine(
                    pos=level,
                    angle=0,
                    movable=False,
                    pen=pg.mkPen(theme.BORDER, width=1, style=style),
                )
            )

        self._curves = {}
        for key, color in series:
            fill = QColor(color)
            fill.setAlpha(40)
            self._curves[key] = item.plot(
                [],
                [],
                pen=pg.mkPen(color, width=2),
                fillLevel=0,
                brush=fill,
                antialias=True,
            )

    def push(self, values: dict[str, float | None]) -> None:
        for key, curve in self._curves.items():
            value = values.get(key)
            # A missing reading is drawn as 0 rather than as a gap: pyqtgraph
            # cannot fill under a line that has holes in it.
            self._data[key].append(0.0 if value is None else max(0.0, min(100.0, value)))
            ys = list(self._data[key])
            curve.setData(list(range(-len(ys) + 1, 1)), ys)
