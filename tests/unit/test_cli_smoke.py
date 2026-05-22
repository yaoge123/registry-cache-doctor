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


@pytest.mark.parametrize("command", ["bogus", "daemon"])
def test_cli_unknown_subcommand_argparse_error(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``daemon`` is provided by the container entrypoint, not the CLI."""

    with pytest.raises(SystemExit) as excinfo:
        main([command])
    # argparse exits with status 2 for argument errors.
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "invalid choice" in captured.err


def test_cli_module_entrypoint() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "rcd", "version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == rcd.__version__
