"""Command-line dispatcher.

Phase 0 only wires up `version`; every other subcommand is reserved
and returns a clear "not yet implemented" error so the contract
shows up in `--help` from day one.

Real subcommand implementations land in later phases:

* Phase 1-3: `scan`, `inspect`
* Phase 4  : `clean`
* Phase 6  : `daemon`
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from rcd.version import __version__

_NOT_IMPLEMENTED_EXIT = 1
_SUBCOMMANDS_PENDING = ("scan", "clean", "inspect", "daemon")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rcd",
        description=(
            "Diagnose and repair Redis blob descriptor cache "
            "inconsistencies in distribution registries."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="Path to the TOML configuration file.",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("version", help="Print the installed version.")

    for name in _SUBCOMMANDS_PENDING:
        sub.add_parser(name, help=f"(not yet implemented in v{__version__})")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command in (None, "version"):
        print(__version__)
        return 0

    if args.command in _SUBCOMMANDS_PENDING:
        print(
            f"rcd: subcommand '{args.command}' is not implemented in "
            f"v{__version__}.",
            file=sys.stderr,
        )
        return _NOT_IMPLEMENTED_EXIT

    parser.error(f"unknown command: {args.command}")
    return 2  # argparse.error never returns; satisfy type-checkers


if __name__ == "__main__":
    sys.exit(main())
