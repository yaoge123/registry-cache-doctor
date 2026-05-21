"""Smoke tests for the Phase 0 scaffold.

These guard the bits that ship in v0.0.0 so that later phases cannot
regress the scaffold contract:

* ``rcd`` is importable
* ``rcd --version`` and ``rcd version`` agree
* unknown subcommands fail cleanly
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import rcd
from rcd.cli import main


def test_package_importable() -> None:
    assert rcd.__version__ == "0.0.0"


def test_cli_no_args_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == rcd.__version__


def test_cli_version_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == rcd.__version__


@pytest.mark.parametrize("command", ["scan", "clean", "inspect", "daemon"])
def test_cli_pending_subcommands_exit_nonzero(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([command]) == 1
    captured = capsys.readouterr()
    assert "not implemented" in captured.err


def test_cli_module_entrypoint() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "rcd", "version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == rcd.__version__
