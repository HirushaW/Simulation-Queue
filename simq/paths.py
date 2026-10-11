"""Where SimQ keeps its files (``~/.simq`` unless ``SIMQ_HOME`` is set)."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    """Return SimQ's data directory, creating it if needed."""
    root = Path(os.environ.get("SIMQ_HOME") or Path.home() / ".simq").expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def read_json(path: Path, default: Any) -> Any:
    """Read a JSON file, returning ``default`` if it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: Path, data: Any) -> None:
    """Write JSON atomically, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
