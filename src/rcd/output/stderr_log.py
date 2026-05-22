"""Human-readable stderr logger with optional ANSI colour.

Used by the orchestrator and CLI for status messages that should *not*
appear in the machine-readable NDJSON stream on stdout.

Output format is line-oriented: ``<LEVEL> <message>\\n``. The level
column is fixed-width so columns line up in a terminal.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import IO

__all__ = ["Level", "StderrLog", "should_use_color"]


class Level(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


# ANSI 16-colour foreground codes. Chosen for readability on both light
# and dark terminals.
_COLOR_BY_LEVEL: dict[Level, str] = {
    Level.INFO: "\x1b[36m",   # cyan
    Level.WARN: "\x1b[33m",   # yellow
    Level.ERROR: "\x1b[31m",  # red
}
_RESET = "\x1b[0m"


def should_use_color(*, stream_isatty: bool, explicit: bool | None) -> bool:
    """Resolve whether to colour output.

    Order of precedence:

    1. ``explicit`` (CLI flag) wins if set.
    2. ``NO_COLOR`` env var disables colour (https://no-color.org).
    3. Otherwise colour is on iff the target stream is a TTY.
    """
    if explicit is not None:
        return explicit
    if os.environ.get("NO_COLOR"):
        return False
    return stream_isatty


class StderrLog:
    """Tiny line-oriented logger writing to a stream (typically stderr)."""

    def __init__(self, stream: IO[str], color: bool = False) -> None:
        self._stream = stream
        self._color = color

    def log(self, level: Level, message: str) -> None:
        prefix = f"{level.value:<5}"
        if self._color:
            color = _COLOR_BY_LEVEL[level]
            self._stream.write(f"{color}{prefix}{_RESET} {message}\n")
        else:
            self._stream.write(f"{prefix} {message}\n")

    def info(self, message: str) -> None:
        self.log(Level.INFO, message)

    def warn(self, message: str) -> None:
        self.log(Level.WARN, message)

    def error(self, message: str) -> None:
        self.log(Level.ERROR, message)
