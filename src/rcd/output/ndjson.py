"""NDJSON event emitter for stdout.

The NDJSON schema is the public contract for ``rcd``'s machine-readable
output. Every event written by this module:

- Uses ``schema_version: 1``.
- Sets ``tool: "rcd"`` and the running version in the version-bearing
  events (``scan_completed``, ``run_summary``, ``clean_completed``).
- Includes a UTC ISO-8601 timestamp (``ts``) ending in ``Z``.
- Carries a ``run_id`` that ties together every event from a single
  invocation.

Records are compact (no whitespace between separators) and are followed
by exactly one ``\\n``, so consumers can split on newlines safely.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Mapping
from typing import IO, Any

from rcd.classifier import Category
from rcd.report import RegistryReport
from rcd.version import __version__

__all__ = [
    "emit_clean_completed",
    "emit_run_summary",
    "emit_scan_completed",
]

_SCHEMA_VERSION = 1
_TOOL = "rcd"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _now_iso_z() -> str:
    """Return the current UTC time as an ISO-8601 string ending in 'Z'."""
    return (
        _dt.datetime.now(_dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _validate_run_id(run_id: str) -> None:
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            f"run_id must match {_RUN_ID_RE.pattern!r}, got {run_id!r}"
        )


def _categories_dict(counts: Mapping[Category, int]) -> dict[str, int]:
    """Render every Category, even at zero, so jq filters always succeed."""
    return {cat.value: int(counts.get(cat, 0)) for cat in Category}


def _write(stream: IO[str], obj: dict[str, Any]) -> None:
    stream.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    stream.write("\n")


def emit_scan_completed(
    stream: IO[str],
    *,
    report: RegistryReport,
    run_id: str,
) -> None:
    """Emit a per-registry ``scan_completed`` event."""
    _validate_run_id(run_id)
    obj: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "ts": _now_iso_z(),
        "run_id": run_id,
        "tool": _TOOL,
        "version": __version__,
        "event": "scan_completed",
        "registry": report.registry,
        "duration_s": round(report.duration_s, 3),
        "redis_keys": {
            "global": report.redis_keys.global_hash,
            "repo_set": report.redis_keys.repo_set,
            "repo_hash": report.redis_keys.repo_hash,
        },
        "categories": _categories_dict(report.classification.counts),
        "errors": list(report.errors),
    }
    _write(stream, obj)


def emit_run_summary(
    stream: IO[str],
    *,
    run_id: str,
    duration_s: float,
    totals: Mapping[Category, int],
) -> None:
    """Emit the run-level ``run_summary`` event."""
    _validate_run_id(run_id)
    obj: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "ts": _now_iso_z(),
        "run_id": run_id,
        "tool": _TOOL,
        "version": __version__,
        "event": "run_summary",
        "duration_s": round(duration_s, 3),
        "totals": _categories_dict(totals),
    }
    _write(stream, obj)


def emit_clean_completed(
    stream: IO[str],
    *,
    run_id: str,
    registry: str,
    planned: int,
    applied: int,
    failed: int,
    retried: int,
    duration_s: float,
) -> None:
    """Emit a per-registry ``clean_completed`` event."""
    _validate_run_id(run_id)
    obj: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "ts": _now_iso_z(),
        "run_id": run_id,
        "tool": _TOOL,
        "version": __version__,
        "event": "clean_completed",
        "registry": registry,
        "planned": planned,
        "applied": applied,
        "failed": failed,
        "retried": retried,
        "duration_s": round(duration_s, 3),
    }
    _write(stream, obj)
