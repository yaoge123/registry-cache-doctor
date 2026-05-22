"""Tests for the stderr human-readable logger (Phase 3)."""

from __future__ import annotations

import io

import pytest

from rcd.output.stderr_log import Level, StderrLog


def test_info_writes_a_line_to_stream() -> None:
    buf = io.StringIO()
    log = StderrLog(buf, color=False)

    log.info("hello world")

    assert buf.getvalue() == "INFO  hello world\n"


def test_warn_and_error_use_level_prefix() -> None:
    buf = io.StringIO()
    log = StderrLog(buf, color=False)

    log.warn("watch out")
    log.error("boom")

    assert buf.getvalue() == "WARN  watch out\nERROR boom\n"


def test_color_enabled_emits_ansi_for_each_level() -> None:
    buf = io.StringIO()
    log = StderrLog(buf, color=True)

    log.info("a")
    log.warn("b")
    log.error("c")

    text = buf.getvalue()
    # Must contain a CSI introducer at all.
    assert "\x1b[" in text
    # Each level uses a distinct foreground colour token.
    info_seen = "\x1b[34m" in text or "\x1b[36m" in text  # blue or cyan
    assert info_seen
    assert "\x1b[33m" in text  # yellow for warn
    assert "\x1b[31m" in text  # red for error
    # And every escape sequence is closed with a reset.
    assert text.count("\x1b[0m") == 3


def test_color_default_off_when_constructed_without_color_arg() -> None:
    buf = io.StringIO()
    log = StderrLog(buf)

    log.info("x")

    assert "\x1b[" not in buf.getvalue()


@pytest.mark.parametrize("level", list(Level))
def test_each_level_can_be_called_via_log_method(level: Level) -> None:
    buf = io.StringIO()
    log = StderrLog(buf, color=False)

    log.log(level, "msg")

    assert "msg" in buf.getvalue()


def test_multi_line_messages_keep_line_oriented_output() -> None:
    """Multi-line content should still produce one record per call."""
    buf = io.StringIO()
    log = StderrLog(buf, color=False)

    log.info("line1\nline2")

    out = buf.getvalue()
    # Whatever format we choose, output must start with the level prefix
    # and end with exactly one trailing newline.
    assert out.startswith("INFO")
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def test_should_use_color_respects_no_color_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rcd.output.stderr_log import should_use_color

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    assert should_use_color(stream_isatty=True, explicit=None) is False


def test_should_use_color_explicit_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rcd.output.stderr_log import should_use_color

    monkeypatch.setenv("NO_COLOR", "1")

    # Explicit True wins over NO_COLOR.
    assert should_use_color(stream_isatty=False, explicit=True) is True
    # Explicit False wins over a TTY.
    assert should_use_color(stream_isatty=True, explicit=False) is False


def test_should_use_color_defaults_to_isatty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rcd.output.stderr_log import should_use_color

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    assert should_use_color(stream_isatty=True, explicit=None) is True
    assert should_use_color(stream_isatty=False, explicit=None) is False
