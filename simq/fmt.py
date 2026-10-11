"""Human-readable formatting shared by the window and the command line."""

from __future__ import annotations

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def size(n: float | None) -> str:
    """Bytes as e.g. ``"12.3 GB"`` (powers of 1024, as Linux tools show them)."""
    if n is None:
        return "–"
    value = float(n)
    for unit in _UNITS:
        if abs(value) < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{value:.0f} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def rate(bps: float | None) -> str:
    """Bytes per second as e.g. ``"120.5 MB/s"``."""
    if bps is None:
        return "–"
    return f"{size(bps)}/s"


def duration(seconds: float | None) -> str:
    """Seconds as e.g. ``"3 d 4 h"``, ``"2 h 05 min"`` or ``"42 s"``."""
    if seconds is None or seconds < 0:
        return "–"
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {s:02d} s"
    return f"{s} s"


def ghz(mhz: float | None) -> str:
    if not mhz:
        return "–"
    return f"{mhz / 1000:.2f} GHz"
