"""Tests for ``entrypoint.sh``.

The entrypoint runs in the container before ``rcd`` itself; it picks up
deployment-level environment variables (``RCD_SCHEDULE``,
``RCD_DAEMON_AUTO_CLEAN``, ``RCD_DAEMON_STRICT``, ``RCD_CONFIG``) and
either exec's supercronic with a generated crontab or exec's rcd
directly. We can't exec supercronic here, so the tests stub
``/usr/local/bin/supercronic`` to a script that prints its argv and the
crontab contents, and stub ``rcd`` to record its argv.

Spec:

* ``daemon`` arg -> writes a crontab with one line per RCD action.
* ``RCD_DAEMON_AUTO_CLEAN=true`` -> adds a second crontab line for clean.
* ``RCD_DAEMON_STRICT=true`` -> appends ``--strict`` to each line.
* anything else -> exec rcd with the original argv unchanged.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = _REPO_ROOT / "entrypoint.sh"


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(
    tmp_path: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run entrypoint.sh with stubbed `supercronic` and `rcd`.

    Returns (process, supercronic_log, rcd_log). The stubs record their
    argv (and, for supercronic, the crontab body) into the log files.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    sup_log = tmp_path / "supercronic.log"
    rcd_log = tmp_path / "rcd.log"

    _make_stub(
        bin_dir / "supercronic",
        # Capture argv and the crontab contents.
        '#!/bin/sh\n'
        'set -eu\n'
        'echo "argv=$*" >> "$LOG"\n'
        # POSIX-portable way to grab the last positional argument.
        'last=""\n'
        'for a in "$@"; do last="$a"; done\n'
        'echo "--- crontab ---" >> "$LOG"\n'
        'cat "$last" >> "$LOG"\n'
        'echo "--- end ---" >> "$LOG"\n',
    )
    _make_stub(
        bin_dir / "rcd",
        '#!/bin/sh\n'
        'echo "rcd-argv=$*" >> "$RCD_LOG"\n',
    )

    # entrypoint.sh hardcodes /usr/local/bin/supercronic, so we have to
    # patch the path via a wrapper script that points at our stub.
    patched = tmp_path / "entrypoint-test.sh"
    patched.write_text(
        _ENTRYPOINT.read_text().replace(
            "/usr/local/bin/supercronic",
            str(bin_dir / "supercronic"),
        )
    )
    patched.chmod(patched.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    full_env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "LOG": str(sup_log),
        "RCD_LOG": str(rcd_log),
    }
    if env:
        full_env.update(env)

    result = subprocess.run(
        ["sh", str(patched), *args],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )
    return result, sup_log, rcd_log


# ---------- daemon path ----------------------------------------------------


def test_daemon_default_writes_only_scan_line(tmp_path: Path) -> None:
    result, sup_log, _ = _run(
        tmp_path,
        "daemon",
        env={
            "RCD_CONFIG": "/etc/rcd/config.toml",
            "RCD_SCHEDULE": "0 3 * * *",
            "RCD_DAEMON_AUTO_CLEAN": "false",
            "RCD_DAEMON_STRICT": "false",
        },
    )
    assert result.returncode == 0, result.stderr
    body = sup_log.read_text()
    assert "0 3 * * * rcd-cron-exec rcd --config /etc/rcd/config.toml scan" in body
    assert "clean --apply" not in body
    assert "--strict" not in body


def test_daemon_auto_clean_appends_clean_line(tmp_path: Path) -> None:
    result, sup_log, _ = _run(
        tmp_path,
        "daemon",
        env={
            "RCD_CONFIG": "/etc/rcd/config.toml",
            "RCD_SCHEDULE": "0 3 * * *",
            "RCD_DAEMON_AUTO_CLEAN": "true",
            "RCD_DAEMON_STRICT": "false",
        },
    )
    assert result.returncode == 0, result.stderr
    body = sup_log.read_text()
    assert "0 3 * * * rcd-cron-exec rcd --config /etc/rcd/config.toml scan" in body
    assert "0 3 * * * rcd-cron-exec rcd --config /etc/rcd/config.toml clean --apply" in body
    # The clean line is the "with-clean" variant; --strict is still off.
    assert "--strict" not in body


def test_daemon_strict_adds_strict_flag_to_every_line(tmp_path: Path) -> None:
    result, sup_log, _ = _run(
        tmp_path,
        "daemon",
        env={
            "RCD_CONFIG": "/etc/rcd/config.toml",
            "RCD_SCHEDULE": "*/5 * * * *",
            "RCD_DAEMON_AUTO_CLEAN": "true",
            "RCD_DAEMON_STRICT": "true",
        },
    )
    assert result.returncode == 0, result.stderr
    body = sup_log.read_text()
    assert "*/5 * * * * rcd-cron-exec rcd --config /etc/rcd/config.toml scan --strict" in body
    assert (
        "*/5 * * * * rcd-cron-exec rcd --config /etc/rcd/config.toml clean --apply --strict"
        in body
    )


def test_daemon_default_schedule_when_unset(tmp_path: Path) -> None:
    result, sup_log, _ = _run(
        tmp_path,
        "daemon",
        env={
            # Don't set RCD_SCHEDULE; expect the entrypoint default.
            "RCD_CONFIG": "/etc/rcd/config.toml",
            "RCD_DAEMON_AUTO_CLEAN": "false",
            "RCD_DAEMON_STRICT": "false",
        },
    )
    assert result.returncode == 0, result.stderr
    body = sup_log.read_text()
    assert "0 3 * * * rcd-cron-exec rcd --config /etc/rcd/config.toml scan" in body


# ---------- one-shot path --------------------------------------------------


def test_non_daemon_arg_execs_rcd_with_argv(tmp_path: Path) -> None:
    result, sup_log, rcd_log = _run(
        tmp_path,
        "scan",
        "--config",
        "/etc/rcd/config.toml",
        env={"RCD_CONFIG": "/etc/rcd/config.toml"},
    )
    assert result.returncode == 0, result.stderr
    # supercronic must NOT have been invoked.
    assert not sup_log.exists() or sup_log.read_text() == ""
    # rcd was called with the original argv.
    assert "rcd-argv=scan --config /etc/rcd/config.toml" in rcd_log.read_text()


def test_non_daemon_inspect_passes_through(tmp_path: Path) -> None:
    result, _, rcd_log = _run(
        tmp_path,
        "inspect",
        "--registry",
        "alpha",
        "--category",
        "c5",
        env={"RCD_CONFIG": "/etc/rcd/config.toml"},
    )
    assert result.returncode == 0, result.stderr
    assert "rcd-argv=inspect --registry alpha --category c5" in rcd_log.read_text()
