"""Progress reporters that render to stderr.

Two implementations:

- :class:`NullProgress` does nothing. Used when ``--quiet`` is set or
  no progress is wanted (e.g. inside the ``daemon`` loop where logs are
  enough).
- :class:`LineProgress` writes a periodic single-line heartbeat per
  registry. Suitable for non-TTY stderr (CI logs, ``docker logs``).

A future Phase can add a TTY multi-bar implementation behind the same
``Progress`` protocol; the orchestrator only depends on the protocol.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Protocol

__all__ = ["LineProgress", "NullProgress", "Progress", "ProgressEvent"]


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One observation of in-flight scan counts for a registry."""

    registry: str
    redis: int
    fs: int


class Progress(Protocol):
    """The minimal interface the orchestrator uses to report progress."""

    def start(self, registry: str, *, total: int | None) -> None: ...
    def update(self, registry: str, *, redis: int, fs: int) -> None: ...
    def finish(self, registry: str) -> None: ...


class NullProgress:
    """A progress reporter that drops every event."""

    def __init__(self, _stream: IO[str]) -> None:  # accept stream for symmetry
        return

    def start(self, registry: str, *, total: int | None) -> None:
        return

    def update(self, registry: str, *, redis: int, fs: int) -> None:
        return

    def finish(self, registry: str) -> None:
        return


class LineProgress:
    """Heartbeat-style progress writer.

    Writes one line on ``start``/``finish`` for each registry, plus a
    throttled line on ``update`` (no more often than ``interval_s``
    seconds per registry).
    """

    def __init__(
        self,
        stream: IO[str],
        *,
        interval_s: float = 5.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._stream = stream
        self._interval = interval_s
        self._now = now or time.monotonic
        # Per-registry timestamp of last emitted update.
        self._last_emit: dict[str, float] = {}

    def start(self, registry: str, *, total: int | None) -> None:
        ts = self._now()
        self._last_emit[registry] = ts
        if total is None:
            self._stream.write(f"[{registry}] start\n")
        else:
            self._stream.write(f"[{registry}] start total={total}\n")

    def update(self, registry: str, *, redis: int, fs: int) -> None:
        now = self._now()
        last = self._last_emit.get(registry, now)
        if (now - last) < self._interval:
            return
        self._last_emit[registry] = now
        self._stream.write(f"[{registry}] redis={redis} fs={fs}\n")

    def finish(self, registry: str) -> None:
        self._last_emit.pop(registry, None)
        self._stream.write(f"[{registry}] done\n")
