"""User settings stored in ``~/.simq/config.json``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .paths import data_dir, read_json, write_json


@dataclass
class Config:
    # Extra folders the software scan should look in.
    search_dirs: list[str] = field(default_factory=list)
    # Programs the user pointed at by hand.
    programs: list[str] = field(default_factory=list)

    @classmethod
    def path(cls) -> Path:
        return data_dir() / "config.json"

    @classmethod
    def load(cls) -> Config:
        raw = read_json(cls.path(), {})
        if not isinstance(raw, dict):
            raw = {}
        return cls(
            search_dirs=_str_list(raw.get("search_dirs")),
            programs=_str_list(raw.get("programs")),
        )

    def save(self) -> None:
        write_json(self.path(), {"search_dirs": self.search_dirs, "programs": self.programs})


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v]
