"""Per-registry and multi-registry scan orchestration.

Two public entry points:

- :func:`scan_one_registry` — run a redis scan and a filesystem scan
  concurrently for a single registry, classify, and return a
  :class:`~rcd.report.RegistryReport`. Raises
  :class:`OrchestrationError` on Redis or filesystem failure.
- :func:`scan_registries` — fan out across many registries in parallel,
  bounded by ``parallel``. Per-registry failures are captured into the
  returned :class:`~rcd.report.RegistryReport` (``errors`` field) and do
  not abort the rest.

A ``factory`` callable is injected so tests can hand in ``fakeredis``
clients without any monkey-patching.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from rcd.classifier import Category, Classification, classify
from rcd.config import RegistryConfig
from rcd.report import RedisKeyCounts, RegistryReport, RunSummary
from rcd.scan.fs_scan import scan_fs
from rcd.scan.redis_scan import scan_redis

__all__ = [
    "OrchestrationError",
    "RedisClientFactory",
    "scan_one_registry",
    "scan_registries",
]


class _RedisLike(Protocol):
    def scan_iter(self, *args: Any, **kwargs: Any) -> Any: ...
    def pipeline(self, *args: Any, **kwargs: Any) -> Any: ...


class RedisClientFactory(Protocol):
    """A callable that builds a Redis client for a given registry."""

    def __call__(self, cfg: RegistryConfig) -> _RedisLike: ...


class OrchestrationError(RuntimeError):
    """Raised when a single-registry scan fails irrecoverably."""


async def scan_one_registry(
    cfg: RegistryConfig,
    *,
    factory: RedisClientFactory,
    scan_count: int = 1000,
) -> RegistryReport:
    """Scan one registry. Redis and FS halves run concurrently."""
    start = time.monotonic()

    try:
        client = factory(cfg)
    except Exception as exc:  # pragma: no cover - factory failure is rare
        raise OrchestrationError(f"{cfg.name}: factory failed: {exc}") from exc

    redis_task = asyncio.to_thread(scan_redis, client, scan_count)
    fs_task = asyncio.to_thread(scan_fs, cfg.storage_path)

    try:
        redis_snap, fs_snap = await asyncio.gather(redis_task, fs_task)
    except Exception as exc:
        raise OrchestrationError(f"{cfg.name}: scan failed: {exc}") from exc

    classification = classify(redis_snap, fs_snap)
    duration = time.monotonic() - start

    return RegistryReport(
        registry=cfg.name,
        duration_s=duration,
        redis_keys=RedisKeyCounts(
            global_hash=len(redis_snap.global_blobs),
            repo_set=len(redis_snap.repo_blob_sets),
            repo_hash=len(redis_snap.repo_blob_hashes),
        ),
        classification=classification,
        errors=(),
    )


async def scan_registries(
    configs: Iterable[RegistryConfig],
    *,
    factory: RedisClientFactory,
    parallel: int = 0,
    scan_count: int = 1000,
) -> tuple[Sequence[RegistryReport], RunSummary]:
    """Scan many registries concurrently with a parallelism cap.

    ``parallel == 0`` means "as many enabled registries as configured".
    Per-registry failures are captured into a degraded
    :class:`RegistryReport` (with ``errors`` populated) rather than
    aborting the run.
    """
    enabled = [c for c in configs if c.enabled]
    if not enabled:
        return ((), RunSummary(duration_s=0.0))

    bound = parallel if parallel > 0 else len(enabled)
    sem = asyncio.Semaphore(bound)

    run_start = time.monotonic()

    async def _one(cfg: RegistryConfig) -> RegistryReport:
        async with sem:
            try:
                return await scan_one_registry(
                    cfg, factory=factory, scan_count=scan_count
                )
            except Exception as exc:
                # Capture the failure as a report so the run can continue.
                detail = _short_error(exc)
                return RegistryReport(
                    registry=cfg.name,
                    duration_s=0.0,
                    redis_keys=RedisKeyCounts(
                        global_hash=0, repo_set=0, repo_hash=0
                    ),
                    classification=_empty_classification(),
                    errors=(detail,),
                )

    reports = await asyncio.gather(*(_one(c) for c in enabled))
    duration = time.monotonic() - run_start

    totals: dict[Category, int] = dict.fromkeys(Category, 0)
    for r in reports:
        for cat, n in r.classification.counts.items():
            totals[cat] = totals.get(cat, 0) + n

    return tuple(reports), RunSummary(duration_s=duration, totals=totals)


def _empty_classification() -> Classification:
    return Classification(
        issues=(),
        counts=dict.fromkeys(Category, 0),
    )


def _short_error(exc: BaseException) -> str:
    """One-line summary suitable for the report's ``errors`` field."""
    msg = str(exc) or exc.__class__.__name__
    # Strip any embedded newlines so NDJSON stays one event per line.
    return msg.replace("\n", " ").strip() or _fallback_traceback(exc)


def _fallback_traceback(exc: BaseException) -> str:  # pragma: no cover
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()
