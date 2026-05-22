"""Redis blob descriptor cache clean stage.

The clean stage is the only mutation surface of ``rcd``. It accepts a sequence
of :class:`rcd.classifier.Issue` records and projects them onto a sequence of
Redis pipeline commands that mirror the ``Clear`` operation in
``registry/storage/cache/redis/redis.go``::

    pipeline.SREM repository::<repo>::blobs <digest>
    pipeline.DEL  repository::<repo>::blobs::<digest>
    pipeline.HDEL blobs::<digest> digest size mediatype

We deliberately do **not** wrap each batch in MULTI/EXEC. Upstream itself
uses a non-transactional pipeline for ``Clear`` -- mirroring it keeps the
tool's failure surface symmetric with the rest of distribution. Half-applied
batches degrade into the same internal-garbage states (C1/C2/C3/C6) that the
classifier already understands; subsequent runs converge.

The translation per category is:

==========  ========================================================
Category    Commands issued
==========  ========================================================
``C1``      ``HDEL blobs::<D> digest size mediatype``
``C2``      full trio (SREM + DEL + HDEL)
``C3``      full trio
``C4``      full trio
``C5``      full trio
``C6``      ``DEL repo_hash`` + ``HDEL blobs::<D> ...``
``C7``      *not* cleaned (file is the source of truth, self-heals)
``OK``      *not* cleaned
==========  ========================================================

Failure handling is per-batch with one automatic retry on
:class:`redis.exceptions.RedisError` (any subclass) or :class:`OSError`.
A second failure is recorded against every issue in that batch and surfaced
through :class:`CleanReport.failures`. This matches the design decision in
``Q4 = (a) pipeline + retry-once + batch=200``.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from rcd.classifier import Category, Issue

try:  # pragma: no cover - import shape only
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover
    RedisError = OSError  # type: ignore[assignment,misc]

__all__ = [
    "CleanFailure",
    "CleanOp",
    "CleanReport",
    "clean_registry",
    "execute_clean",
    "plan_clean",
]

_DEFAULT_BATCH = 200
_DEFAULT_RETRY = 1
_GLOBAL_FIELDS = ("digest", "size", "mediatype")

# Issues from these categories represent real failures or recoverable
# inconsistencies that the tool wants to fix.
_INTERNAL_GARBAGE: frozenset[Category] = frozenset(
    {Category.C1, Category.C2, Category.C3, Category.C6}
)
_REAL_FAILURES: frozenset[Category] = frozenset({Category.C4, Category.C5})
_REPO_SCOPED: frozenset[Category] = frozenset(
    {Category.C2, Category.C3, Category.C4, Category.C5, Category.C6}
)
# C6 specifically does not have set membership, so SREM is omitted.
_NEEDS_SREM: frozenset[Category] = frozenset(
    {Category.C2, Category.C3, Category.C4, Category.C5}
)


@dataclass(frozen=True, slots=True)
class CleanOp:
    """A single planned cleanup, translated from one :class:`Issue`."""

    category: Category
    digest: str
    repo: str | None  # None for C1 (global-only)

    @property
    def command_count(self) -> int:
        """Number of Redis commands this op contributes to a pipeline."""
        if self.category == Category.C1:
            return 1
        if self.category == Category.C6:
            return 2
        return 3


@dataclass(frozen=True, slots=True)
class CleanFailure:
    """A planned op whose pipeline batch ultimately failed."""

    category: Category
    digest: str
    repo: str | None
    error: str


@dataclass(frozen=True, slots=True)
class CleanReport:
    """Outcome of cleaning a single registry."""

    registry: str
    duration_s: float
    planned: int
    applied: int
    failed: int
    retried: int
    failures: tuple[CleanFailure, ...]


class _RedisPipelineLike(Protocol):
    def srem(self, key: str, *values: str) -> Any: ...
    def delete(self, *keys: str) -> Any: ...
    def hdel(self, key: str, *fields: str) -> Any: ...
    def execute(self) -> Any: ...


class _RedisLike(Protocol):
    def pipeline(self, transaction: bool = ...) -> _RedisPipelineLike: ...


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def plan_clean(
    issues: Iterable[Issue], *, clear_internal_garbage: bool = True
) -> tuple[CleanOp, ...]:
    """Translate classifier issues into a sequence of cleanup ops.

    Parameters
    ----------
    issues:
        The :attr:`rcd.classifier.Classification.issues` sequence (or any
        compatible iterable).
    clear_internal_garbage:
        When ``True`` (default) the plan covers every category in
        :data:`_INTERNAL_GARBAGE` plus :data:`_REAL_FAILURES`. When ``False``
        only ``C4`` and ``C5`` (the user-visible failure modes) are planned.

    Returns
    -------
    tuple[CleanOp, ...]
        Ordered ops, mirroring the input order.
    """
    eligible: frozenset[Category] = (
        _INTERNAL_GARBAGE | _REAL_FAILURES if clear_internal_garbage else _REAL_FAILURES
    )

    out: list[CleanOp] = []
    for issue in issues:
        if issue.category not in eligible:
            continue
        # Repo-scoped categories require a repo; defensively treat a
        # missing repo as a malformed issue and skip rather than fabricate
        # commands that would corrupt unrelated data.
        if issue.category in _REPO_SCOPED and issue.repo is None:
            continue
        out.append(
            CleanOp(category=issue.category, digest=issue.digest, repo=issue.repo)
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


def _queue_op(pipe: _RedisPipelineLike, op: CleanOp) -> None:
    """Stage an op's commands onto an already-open pipeline."""
    if op.category == Category.C1:
        pipe.hdel(f"blobs::{op.digest}", *_GLOBAL_FIELDS)
        return
    # Repo-scoped from here onward: repo is guaranteed non-None by plan.
    assert op.repo is not None
    repo_set = f"repository::{op.repo}::blobs"
    repo_hash = f"repository::{op.repo}::blobs::{op.digest}"
    global_hash = f"blobs::{op.digest}"
    if op.category in _NEEDS_SREM:
        pipe.srem(repo_set, op.digest)
    pipe.delete(repo_hash)
    pipe.hdel(global_hash, *_GLOBAL_FIELDS)


def execute_clean(
    client: _RedisLike,
    ops: Sequence[CleanOp],
    *,
    batch: int = _DEFAULT_BATCH,
    retry: int = _DEFAULT_RETRY,
) -> _ExecutionReport:
    """Run ``ops`` in batched non-transactional pipelines.

    Returns an internal :class:`_ExecutionReport`. ``clean_registry`` wraps
    this with registry-level metadata.
    """
    if not ops:
        return _ExecutionReport(planned=0, applied=0, failed=0, retried=0, failures=())

    failures: list[CleanFailure] = []
    applied = 0
    retried = 0

    for start in range(0, len(ops), batch):
        chunk = ops[start : start + batch]
        attempts = 0
        max_attempts = retry + 1
        last_error: BaseException | None = None
        while attempts < max_attempts:
            attempts += 1
            pipe = client.pipeline(transaction=False)
            for op in chunk:
                _queue_op(pipe, op)
            try:
                pipe.execute()
                last_error = None
                break
            except (RedisError, OSError) as exc:
                last_error = exc
                if attempts < max_attempts:
                    retried += 1
        if last_error is None:
            applied += len(chunk)
        else:
            err = _short_error(last_error)
            for op in chunk:
                failures.append(
                    CleanFailure(
                        category=op.category,
                        digest=op.digest,
                        repo=op.repo,
                        error=err,
                    )
                )
    return _ExecutionReport(
        planned=len(ops),
        applied=applied,
        failed=len(failures),
        retried=retried,
        failures=tuple(failures),
    )


@dataclass(frozen=True, slots=True)
class _ExecutionReport:
    planned: int
    applied: int
    failed: int
    retried: int
    failures: tuple[CleanFailure, ...]


# ---------------------------------------------------------------------------
# clean_registry
# ---------------------------------------------------------------------------


def clean_registry(
    *,
    registry: str,
    client: _RedisLike,
    issues: Iterable[Issue],
    dry_run: bool,
    clear_internal_garbage: bool = True,
    batch: int = _DEFAULT_BATCH,
    retry: int = _DEFAULT_RETRY,
) -> CleanReport:
    """Plan and (optionally) apply cleanup for one registry.

    When ``dry_run=True`` the plan is computed and counted, but no commands
    are issued.
    """
    start = time.monotonic()
    ops = plan_clean(issues, clear_internal_garbage=clear_internal_garbage)
    if dry_run or not ops:
        duration = time.monotonic() - start
        return CleanReport(
            registry=registry,
            duration_s=duration,
            planned=len(ops),
            applied=0,
            failed=0,
            retried=0,
            failures=(),
        )
    exec_report = execute_clean(client, ops, batch=batch, retry=retry)
    duration = time.monotonic() - start
    return CleanReport(
        registry=registry,
        duration_s=duration,
        planned=exec_report.planned,
        applied=exec_report.applied,
        failed=exec_report.failed,
        retried=exec_report.retried,
        failures=exec_report.failures,
    )


def _short_error(exc: BaseException) -> str:
    msg = str(exc) or exc.__class__.__name__
    return msg.replace("\n", " ").strip()
