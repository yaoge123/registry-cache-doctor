"""Tests for ``scripts/rcd-cron-exec.sh``.

The wrapper is invoked from the container's crontab entries to translate
``rcd``'s "drift detected" exit code (2) into a successful exit so that
supercronic does not log a misleading ``level=error`` line every run.

Behaviour spec:

* exit 0  -> 0  (no drift, pristine)
* exit 1  -> 1  (tool/connection error)
* exit 2  -> 0  (drift detected; informational, not a failure)
* exit 3  -> 3  (--strict + real failure)
* exit 4  -> 4  (--strict + clean failed)
* exit 42 -> 42 (any other code is passed through verbatim)

The wrapper itself must:

* require at least one positional argument (the command to run)
* exec the command with all remaining arguments preserved
* not swallow stdout/stderr from the wrapped command
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WRAPPER = _REPO_ROOT / "scripts" / "rcd-cron-exec.sh"


def _run_wrapper(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(_WRAPPER), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_wrapper_exists_and_is_executable() -> None:
    assert _WRAPPER.is_file(), f"missing wrapper: {_WRAPPER}"
    mode = _WRAPPER.stat().st_mode
    assert mode & stat.S_IXUSR, "wrapper must be executable"


def test_wrapper_requires_at_least_one_argument() -> None:
    result = _run_wrapper([])
    assert result.returncode != 0
    assert "usage" in result.stderr.lower() or result.returncode == 64


@pytest.mark.parametrize(
    ("input_code", "expected_code"),
    [
        (0, 0),
        (1, 1),
        (2, 0),  # the whole point of the wrapper
        (3, 3),
        (4, 4),
        (42, 42),
    ],
)
def test_wrapper_translates_exit_codes(input_code: int, expected_code: int) -> None:
    sh = shutil.which("sh")
    assert sh is not None
    result = _run_wrapper([sh, "-c", f"exit {input_code}"])
    assert result.returncode == expected_code, (
        f"expected exit {expected_code} for input {input_code}, "
        f"got {result.returncode}; stderr={result.stderr!r}"
    )


def test_wrapper_preserves_stdout_and_stderr() -> None:
    sh = shutil.which("sh")
    assert sh is not None
    result = _run_wrapper([sh, "-c", "printf hello; printf bye 1>&2; exit 2"])
    assert result.returncode == 0
    assert result.stdout == "hello"
    assert result.stderr == "bye"


def test_wrapper_passes_arguments_to_command() -> None:
    # Use printf to echo back arguments so we can assert they were preserved.
    sh = shutil.which("sh")
    assert sh is not None
    result = _run_wrapper([sh, "-c", "echo $#:$1:$2", "_", "alpha", "beta"])
    assert result.returncode == 0
    assert result.stdout.strip() == "2:alpha:beta"


def test_wrapper_handles_command_not_found() -> None:
    # POSIX shells return 127 when the command cannot be located; we must not
    # rewrite that code.
    result = _run_wrapper(["this-command-definitely-does-not-exist-zzz"])
    assert result.returncode == 127
