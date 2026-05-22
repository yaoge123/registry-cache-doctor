"""Tests for the progress reporter (Phase 3)."""

from __future__ import annotations

import io
import time

from rcd.output.progress import LineProgress, NullProgress, ProgressEvent


class _FakeClock:
    def __init__(self) -> None:
        self._t = 1000.0

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def test_null_progress_writes_nothing() -> None:
    buf = io.StringIO()
    p = NullProgress(buf)

    p.start("reg", total=None)
    p.update("reg", redis=10, fs=5)
    p.finish("reg")

    assert buf.getvalue() == ""


def test_line_progress_starts_and_finishes_emit_a_line_each() -> None:
    buf = io.StringIO()
    p = LineProgress(buf, interval_s=5.0, now=time.monotonic)

    p.start("reg", total=None)
    p.finish("reg")

    text = buf.getvalue()
    lines = text.splitlines()
    assert len(lines) == 2
    assert "reg" in lines[0]
    assert "start" in lines[0].lower()
    assert "reg" in lines[1]
    assert "done" in lines[1].lower() or "finish" in lines[1].lower()


def test_line_progress_throttles_updates_to_interval() -> None:
    buf = io.StringIO()
    clock = _FakeClock()
    p = LineProgress(buf, interval_s=5.0, now=clock)

    p.start("reg", total=None)
    p.update("reg", redis=10, fs=5)  # immediately after start, throttled
    clock.advance(2.0)
    p.update("reg", redis=20, fs=10)  # still inside the 5s window
    clock.advance(4.0)
    p.update("reg", redis=30, fs=15)  # 6s since start, should fire
    p.finish("reg")

    update_lines = [
        line for line in buf.getvalue().splitlines() if "redis=" in line
    ]
    assert len(update_lines) == 1
    assert "redis=30" in update_lines[0]
    assert "fs=15" in update_lines[0]


def test_line_progress_handles_multiple_registries_independently() -> None:
    buf = io.StringIO()
    clock = _FakeClock()
    p = LineProgress(buf, interval_s=5.0, now=clock)

    p.start("a", total=None)
    p.start("b", total=None)
    clock.advance(6.0)
    p.update("a", redis=1, fs=1)
    p.update("b", redis=2, fs=2)
    p.finish("a")
    p.finish("b")

    text = buf.getvalue()
    assert text.count("a") >= 3
    assert text.count("b") >= 3
    a_updates = [line for line in text.splitlines() if "a" in line and "redis=1" in line]
    b_updates = [line for line in text.splitlines() if "b" in line and "redis=2" in line]
    assert len(a_updates) == 1
    assert len(b_updates) == 1


def test_progress_event_dataclass_carries_counts() -> None:
    e = ProgressEvent(registry="reg", redis=100, fs=50)
    assert e.registry == "reg"
    assert e.redis == 100
    assert e.fs == 50
