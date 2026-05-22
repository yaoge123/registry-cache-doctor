"""``rcd`` command-line entry point.

Subcommands:

* ``version`` — print the installed version.
* ``scan``    — scan all enabled registries, emit NDJSON, exit 0/2/3.
* ``clean``   — scan and (optionally) clean up inconsistent entries.
* ``inspect`` — print one NDJSON ``inspect`` event per issue in a single
                registry, optionally including the list of referencing
                repositories.

The ``daemon`` mode is provided by the container image's entrypoint
script (``entrypoint.sh``) which schedules ``rcd scan`` (and optionally
``rcd clean --apply``) via supercronic. There is no ``rcd daemon``
subcommand; run the container with ``daemon`` as its argument instead.

Exit codes (machine-readable contract):

* 0 — success, or only ``c7`` (self-healing) drift found.
* 1 — tool error (config invalid, all registries failed, etc.).
* 2 — drift detected (scan), or ``clean`` ran in dry-run with planned ops.
* 3 — ``--strict`` and ``c4`` / ``c5`` (real failures) detected.
* 4 — ``--strict`` and at least one cleanup operation failed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import traceback
from collections.abc import Sequence
from typing import IO, Any

from rcd.classifier import Category
from rcd.clean import clean_registry
from rcd.config import AppConfig, ConfigError, find_config, load_config
from rcd.orchestrator import scan_one_registry, scan_registries
from rcd.output.ndjson import (
    emit_clean_completed,
    emit_inspect,
    emit_run_summary,
    emit_scan_completed,
)
from rcd.output.stderr_log import StderrLog, should_use_color
from rcd.redis_client import make_redis_factory
from rcd.scan.redis_scan import scan_redis
from rcd.version import __version__

__all__ = ["main"]

EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_DRIFT = 2
EXIT_STRICT_REAL_FAILURE = 3
EXIT_STRICT_CLEAN_FAILED = 4

_REAL_FAILURES = (Category.C4, Category.C5)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rcd",
        description=(
            "Diagnose and repair Redis blob descriptor cache inconsistencies "
            "in distribution registries."
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

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--no-color", action="store_true", help="Disable ANSI colours on stderr.")
        p.add_argument("--quiet", action="store_true", help="Silence stderr progress.")
        p.add_argument(
            "--parallel",
            type=int,
            metavar="N",
            help="Override registries to scan concurrently.",
        )

    p_scan = sub.add_parser("scan", help="Scan registries and emit NDJSON results.")
    _add_common(p_scan)
    p_scan.add_argument("--strict", action="store_true", help="Exit 3 if c4/c5 are found.")
    p_scan.add_argument(
        "--verify-digest",
        action="store_true",
        help="Recompute sha256 of files (slow).",
    )

    p_clean = sub.add_parser("clean", help="Scan, then optionally clean inconsistent entries.")
    _add_common(p_clean)
    p_clean.add_argument(
        "--apply",
        action="store_true",
        help="Actually mutate redis. Without it, the run is dry.",
    )
    p_clean.add_argument(
        "--strict",
        action="store_true",
        help="Exit 4 if any cleanup operation failed.",
    )

    p_inspect = sub.add_parser(
        "inspect",
        help="Emit one NDJSON inspect event per issue in a single registry.",
    )
    _add_common(p_inspect)
    p_inspect.add_argument("--registry", required=True, help="Registry name (must be enabled).")
    p_inspect.add_argument(
        "--category",
        required=True,
        choices=[c.value for c in Category],
        help="Category to filter on (e.g. c5).",
    )
    p_inspect.add_argument(
        "--with-refs",
        action="store_true",
        help="Include the list of repositories referencing each digest.",
    )

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(args: argparse.Namespace, log: StderrLog) -> AppConfig | None:
    explicit = args.config
    path = find_config(explicit=explicit)
    if path is None:
        log.error("No configuration file found. Pass --config or set RCD_CONFIG.")
        return None
    try:
        return load_config(path)
    except ConfigError as exc:
        log.error(f"config: {exc}")
        return None


def _new_run_id() -> str:
    return secrets.token_hex(4)


def _stderr_log(args: argparse.Namespace) -> StderrLog:
    no_color = bool(getattr(args, "no_color", False))
    use_color = should_use_color(
        stream_isatty=sys.stderr.isatty(),
        explicit=False if no_color else None,
    )
    return StderrLog(sys.stderr, color=use_color)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def _cmd_scan(args: argparse.Namespace, *, stdout: IO[str], stderr_log: StderrLog) -> int:
    cfg = _load(args, stderr_log)
    if cfg is None:
        return EXIT_TOOL_ERROR

    factory = make_redis_factory(cfg.redis)
    parallel = args.parallel if args.parallel is not None else cfg.scan.parallel
    run_id = _new_run_id()

    try:
        reports, summary = asyncio.run(
            scan_registries(
                cfg.registries,
                factory=factory,
                parallel=parallel,
                scan_count=cfg.redis.scan_count,
            )
        )
    except Exception as exc:
        stderr_log.error(f"scan failed: {exc}")
        return EXIT_TOOL_ERROR

    for report in reports:
        emit_scan_completed(stdout, report=report, run_id=run_id)
    emit_run_summary(stdout, run_id=run_id, duration_s=summary.duration_s, totals=summary.totals)

    return _scan_exit_code(reports, summary, strict=args.strict)


def _scan_exit_code(reports: Sequence[Any], summary: Any, *, strict: bool) -> int:
    # All registries failed => tool error.
    if reports and all(r.errors for r in reports):
        return EXIT_TOOL_ERROR

    real_failures = sum(summary.totals.get(c, 0) for c in _REAL_FAILURES)
    drift_categories = (
        Category.C1, Category.C2, Category.C3, Category.C4, Category.C5, Category.C6,
    )
    drift = sum(summary.totals.get(c, 0) for c in drift_categories)

    if strict and real_failures > 0:
        return EXIT_STRICT_REAL_FAILURE
    if drift > 0:
        return EXIT_DRIFT
    return EXIT_OK


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------


def _cmd_clean(args: argparse.Namespace, *, stdout: IO[str], stderr_log: StderrLog) -> int:
    cfg = _load(args, stderr_log)
    if cfg is None:
        return EXIT_TOOL_ERROR

    factory = make_redis_factory(cfg.redis)
    parallel = args.parallel if args.parallel is not None else cfg.scan.parallel
    run_id = _new_run_id()
    dry_run = not args.apply

    try:
        reports, summary = asyncio.run(
            scan_registries(
                cfg.registries,
                factory=factory,
                parallel=parallel,
                scan_count=cfg.redis.scan_count,
            )
        )
    except Exception as exc:
        stderr_log.error(f"scan failed: {exc}")
        return EXIT_TOOL_ERROR

    for report in reports:
        emit_scan_completed(stdout, report=report, run_id=run_id)
    emit_run_summary(stdout, run_id=run_id, duration_s=summary.duration_s, totals=summary.totals)

    by_name = {c.name: c for c in cfg.registries}
    total_failed = 0
    total_planned = 0
    total_applied = 0

    for report in reports:
        if report.errors:
            stderr_log.warn(f"{report.registry}: skipping clean ({report.errors[0]})")
            continue
        registry_cfg = by_name[report.registry]
        try:
            client = factory(registry_cfg)
        except Exception as exc:
            stderr_log.error(f"{report.registry}: factory failed: {exc}")
            continue
        try:
            clean_report = clean_registry(
                registry=report.registry,
                client=client,
                issues=report.classification.issues,
                dry_run=dry_run,
                clear_internal_garbage=cfg.clean.clear_internal_garbage,
                batch=cfg.redis.pipeline_batch,
                retry=cfg.clean.retry,
            )
        except Exception as exc:
            stderr_log.error(f"{report.registry}: clean failed: {exc}")
            continue
        emit_clean_completed(
            stdout,
            run_id=run_id,
            registry=clean_report.registry,
            planned=clean_report.planned,
            applied=clean_report.applied,
            failed=clean_report.failed,
            retried=clean_report.retried,
            duration_s=clean_report.duration_s,
        )
        total_planned += clean_report.planned
        total_applied += clean_report.applied
        total_failed += clean_report.failed

    return _clean_exit_code(
        dry_run=dry_run,
        total_planned=total_planned,
        total_applied=total_applied,
        total_failed=total_failed,
        strict=args.strict,
    )


def _clean_exit_code(
    *,
    dry_run: bool,
    total_planned: int,
    total_applied: int,
    total_failed: int,
    strict: bool,
) -> int:
    if strict and total_failed > 0:
        return EXIT_STRICT_CLEAN_FAILED
    if dry_run and total_planned > 0:
        return EXIT_DRIFT
    if not dry_run and total_failed > 0:
        return EXIT_DRIFT
    return EXIT_OK


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def _cmd_inspect(args: argparse.Namespace, *, stdout: IO[str], stderr_log: StderrLog) -> int:
    cfg = _load(args, stderr_log)
    if cfg is None:
        return EXIT_TOOL_ERROR

    by_name = {c.name: c for c in cfg.registries if c.enabled}
    if args.registry not in by_name:
        stderr_log.error(f"unknown or disabled registry: {args.registry}")
        return EXIT_TOOL_ERROR
    registry_cfg = by_name[args.registry]

    factory = make_redis_factory(cfg.redis)
    target = Category(args.category)
    run_id = _new_run_id()

    try:
        report = asyncio.run(
            scan_one_registry(
                registry_cfg,
                factory=factory,
                scan_count=cfg.redis.scan_count,
            )
        )
    except Exception as exc:
        stderr_log.error(f"inspect failed: {exc}")
        return EXIT_TOOL_ERROR

    if args.with_refs:
        # Need the redis snapshot's repo sets for cross-references; rescan
        # synchronously so we don't store the raw snapshot in the report.
        try:
            client = factory(registry_cfg)
            redis_snap = scan_redis(client, scan_count=cfg.redis.scan_count)
        except Exception as exc:
            stderr_log.error(f"inspect refs failed: {exc}")
            return EXIT_TOOL_ERROR
        refs_by_digest: dict[str, list[str]] = {}
        for repo, blob_set in redis_snap.repo_blob_sets.items():
            for digest in blob_set.digests:
                refs_by_digest.setdefault(digest, []).append(repo)
        for digest in refs_by_digest:
            refs_by_digest[digest].sort()
    else:
        refs_by_digest = {}

    for issue in report.classification.issues:
        if issue.category != target:
            continue
        refs = refs_by_digest.get(issue.digest, []) if args.with_refs else None
        emit_inspect(
            stdout,
            run_id=run_id,
            registry=registry_cfg.name,
            issue=issue,
            refs=refs,
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command in (None, "version"):
        print(__version__)
        return EXIT_OK

    log = _stderr_log(args)
    try:
        if args.command == "scan":
            return _cmd_scan(args, stdout=sys.stdout, stderr_log=log)
        if args.command == "clean":
            return _cmd_clean(args, stdout=sys.stdout, stderr_log=log)
        if args.command == "inspect":
            return _cmd_inspect(args, stdout=sys.stdout, stderr_log=log)
    except KeyboardInterrupt:
        log.error("interrupted")
        return EXIT_TOOL_ERROR
    except Exception as exc:  # pragma: no cover - safety net
        log.error(f"unexpected error: {exc}")
        if os.environ.get("RCD_DEBUG"):
            traceback.print_exc()
        return EXIT_TOOL_ERROR

    parser.error(f"unknown command: {args.command}")
    return EXIT_TOOL_ERROR


if __name__ == "__main__":
    sys.exit(main())
