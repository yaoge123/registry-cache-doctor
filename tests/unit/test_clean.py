"""Tests for the clean module.

The clean stage takes an iterable of :class:`rcd.classifier.Issue` and:

1. *Plans* the Redis command sequence for each issue, deriving operations
   that mirror the distribution ``Clear`` pipeline semantics.
2. *Executes* the plan in batched non-transactional pipelines, with one
   automatic retry on RedisError and per-op failure accounting.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import fakeredis
import pytest

from rcd.classifier import Category, Issue
from rcd.clean import (
    CleanFailure,
    CleanOp,
    CleanReport,
    clean_registry,
    execute_clean,
    plan_clean,
)

D1 = "sha256:" + "11" * 32
D2 = "sha256:" + "22" * 32
D3 = "sha256:" + "33" * 32
D4 = "sha256:" + "44" * 32
D5 = "sha256:" + "55" * 32
D6 = "sha256:" + "66" * 32
R1 = "library/alpine"
R2 = "library/busybox"


def _categories(ops: Iterable[CleanOp]) -> list[Category]:
    return [op.category for op in ops]


# ---------------------------------------------------------------------------
# plan_clean
# ---------------------------------------------------------------------------


def test_plan_returns_empty_for_empty_issues() -> None:
    assert plan_clean(()) == ()


def test_plan_skips_ok_and_c7() -> None:
    issues = (
        Issue(category=Category.C7, digest=D1, repo=None, redis_size=None, fs_size=10),
    )
    assert plan_clean(issues) == ()


def test_plan_for_c4_yields_repo_scoped_op() -> None:
    issues = (
        Issue(category=Category.C4, digest=D1, repo=R1, redis_size=100, fs_size=None),
    )
    ops = plan_clean(issues)
    assert _categories(ops) == [Category.C4]
    assert ops[0].digest == D1
    assert ops[0].repo == R1


def test_plan_for_c5_yields_repo_scoped_op() -> None:
    issues = (
        Issue(category=Category.C5, digest=D1, repo=R1, redis_size=200, fs_size=100),
    )
    ops = plan_clean(issues)
    assert _categories(ops) == [Category.C5]


def test_plan_for_c1_op_when_internal_garbage_enabled() -> None:
    issues = (
        Issue(category=Category.C1, digest=D1, repo=None, redis_size=10, fs_size=10),
    )
    assert _categories(plan_clean(issues)) == [Category.C1]


def test_plan_skips_internal_categories_when_disabled() -> None:
    issues = (
        Issue(category=Category.C1, digest=D1, repo=None, redis_size=10, fs_size=10),
        Issue(category=Category.C2, digest=D2, repo=R1, redis_size=None, fs_size=10),
        Issue(category=Category.C3, digest=D3, repo=R1, redis_size=10, fs_size=10),
        Issue(category=Category.C4, digest=D4, repo=R1, redis_size=10, fs_size=None),
        Issue(category=Category.C5, digest=D5, repo=R1, redis_size=20, fs_size=10),
        Issue(category=Category.C6, digest=D6, repo=R1, redis_size=10, fs_size=10),
    )
    ops = plan_clean(issues, clear_internal_garbage=False)
    assert _categories(ops) == [Category.C4, Category.C5]


def test_plan_preserves_issue_order() -> None:
    issues = (
        Issue(category=Category.C5, digest=D1, repo=R1, redis_size=20, fs_size=10),
        Issue(category=Category.C4, digest=D2, repo=R2, redis_size=30, fs_size=None),
        Issue(category=Category.C1, digest=D3, repo=None, redis_size=40, fs_size=None),
    )
    ops = plan_clean(issues)
    assert [op.digest for op in ops] == [D1, D2, D3]


def test_plan_op_command_count_matches_category() -> None:
    issues = (
        Issue(category=Category.C1, digest=D1, repo=None, redis_size=10, fs_size=None),
        Issue(category=Category.C5, digest=D2, repo=R1, redis_size=10, fs_size=20),
        Issue(category=Category.C6, digest=D3, repo=R1, redis_size=10, fs_size=10),
    )
    ops = plan_clean(issues)
    counts = [op.command_count for op in ops]
    assert counts == [1, 3, 2]


# ---------------------------------------------------------------------------
# execute_clean
# ---------------------------------------------------------------------------


def _populate_full(client: Any, repo: str, digest: str, size: int = 100) -> None:
    """Lay down the upstream-shaped trio of keys for a (repo, digest)."""
    client.sadd(f"repository::{repo}::blobs", digest)
    client.hset(f"repository::{repo}::blobs::{digest}", "mediatype", "application/octet-stream")
    client.hset(
        f"blobs::{digest}",
        mapping={"digest": digest, "size": str(size), "mediatype": "application/octet-stream"},
    )


def test_execute_c4_removes_all_three_redis_artifacts() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    _populate_full(client, R1, D1)
    issue = Issue(category=Category.C4, digest=D1, repo=R1, redis_size=100, fs_size=None)
    report = execute_clean(client, plan_clean((issue,)))
    assert report.applied == 1
    assert report.failed == 0
    assert client.smembers(f"repository::{R1}::blobs") == set()
    assert not client.exists(f"repository::{R1}::blobs::{D1}")
    # HDEL leaves the global key empty -> redis returns 0 for exists, but
    # if we deleted all three fields the key disappears in standard Redis.
    assert client.hgetall(f"blobs::{D1}") == {}


def test_execute_c5_size_mismatch_clears_three_keys() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    _populate_full(client, R1, D1, size=999)
    issue = Issue(category=Category.C5, digest=D1, repo=R1, redis_size=999, fs_size=100)
    report = execute_clean(client, plan_clean((issue,)))
    assert report.applied == 1
    assert client.smembers(f"repository::{R1}::blobs") == set()


def test_execute_c1_hdels_global_fields_only() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    client.hset(
        f"blobs::{D1}",
        mapping={"digest": D1, "size": "100", "mediatype": "application/octet-stream"},
    )
    issue = Issue(category=Category.C1, digest=D1, repo=None, redis_size=100, fs_size=None)
    report = execute_clean(client, plan_clean((issue,)))
    assert report.applied == 1
    assert client.hgetall(f"blobs::{D1}") == {}


def test_execute_c6_dels_repo_hash_and_global_no_srem() -> None:
    """C6 = repo hash present, repo set does NOT contain the digest.

    The plan must NOT issue a no-op SREM (we trust upstream's pipeline
    semantics, but we don't need to do work that has no effect)."""
    client = fakeredis.FakeRedis(decode_responses=True)
    # repo set membership is empty intentionally
    client.sadd(f"repository::{R1}::blobs", D2)  # unrelated digest still there
    client.hset(
        f"repository::{R1}::blobs::{D1}", "mediatype", "application/octet-stream"
    )
    client.hset(
        f"blobs::{D1}",
        mapping={"digest": D1, "size": "100", "mediatype": "application/octet-stream"},
    )
    issue = Issue(category=Category.C6, digest=D1, repo=R1, redis_size=100, fs_size=100)
    report = execute_clean(client, plan_clean((issue,)))
    assert report.applied == 1
    # The unrelated set member must remain.
    assert client.smembers(f"repository::{R1}::blobs") == {D2}
    assert not client.exists(f"repository::{R1}::blobs::{D1}")
    assert client.hgetall(f"blobs::{D1}") == {}


def test_execute_idempotent_when_keys_already_gone() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    # Don't populate anything; every command becomes a no-op.
    issue = Issue(category=Category.C4, digest=D1, repo=R1, redis_size=100, fs_size=None)
    report = execute_clean(client, plan_clean((issue,)))
    # No errors; applied counts the planned op (it is "applied" in the sense
    # that the pipeline ran without error).
    assert report.failed == 0
    assert report.applied == 1


def test_execute_respects_batch_size_smaller_than_n() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    issues = []
    for i in range(5):
        digest = f"sha256:{i:064x}"
        _populate_full(client, R1, digest)
        issues.append(Issue(category=Category.C4, digest=digest, repo=R1, redis_size=100, fs_size=None))
    report = execute_clean(client, plan_clean(tuple(issues)), batch=2)
    assert report.applied == 5
    assert report.failed == 0


def test_execute_returns_zero_for_empty_ops() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    report = execute_clean(client, ())
    assert report.applied == 0
    assert report.planned == 0
    assert report.failed == 0


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class _FlakyPipeline:
    """A pipeline stub whose ``execute`` raises N times then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    def sadd(self, *args: Any, **_: Any) -> _FlakyPipeline:
        self.commands.append(("sadd", args))
        return self

    def srem(self, *args: Any, **_: Any) -> _FlakyPipeline:
        self.commands.append(("srem", args))
        return self

    def delete(self, *args: Any, **_: Any) -> _FlakyPipeline:
        self.commands.append(("delete", args))
        return self

    def hdel(self, *args: Any, **_: Any) -> _FlakyPipeline:
        self.commands.append(("hdel", args))
        return self

    def execute(self) -> list[Any]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("simulated redis connection drop")
        return [1] * len(self.commands)


class _FlakyClient:
    def __init__(self, fail_times: int) -> None:
        self._pipe = _FlakyPipeline(fail_times)

    def pipeline(self, transaction: bool = True) -> _FlakyPipeline:
        # Reset state per pipeline like real redis-py.
        self._pipe.commands = []
        return self._pipe


def test_execute_retries_once_on_failure_then_succeeds() -> None:
    client = _FlakyClient(fail_times=1)
    issue = Issue(category=Category.C4, digest=D1, repo=R1, redis_size=100, fs_size=None)
    report = execute_clean(client, plan_clean((issue,)), batch=10, retry=1)
    assert report.applied == 1
    assert report.retried == 1
    assert report.failed == 0
    assert report.failures == ()


def test_execute_records_failure_after_retry_exhausted() -> None:
    client = _FlakyClient(fail_times=99)
    issue = Issue(category=Category.C5, digest=D1, repo=R1, redis_size=100, fs_size=50)
    report = execute_clean(client, plan_clean((issue,)), batch=10, retry=1)
    assert report.applied == 0
    assert report.failed == 1
    assert len(report.failures) == 1
    failure = report.failures[0]
    assert isinstance(failure, CleanFailure)
    assert failure.digest == D1
    assert failure.repo == R1
    assert failure.category == Category.C5


# ---------------------------------------------------------------------------
# clean_registry (high-level)
# ---------------------------------------------------------------------------


def test_clean_registry_dry_run_returns_plan_without_apply() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    _populate_full(client, R1, D1)
    issues = (
        Issue(category=Category.C4, digest=D1, repo=R1, redis_size=100, fs_size=None),
    )
    report = clean_registry(
        registry="reg", client=client, issues=issues, dry_run=True
    )
    assert isinstance(report, CleanReport)
    assert report.registry == "reg"
    assert report.planned == 1
    assert report.applied == 0
    assert report.failed == 0
    # Nothing must have changed.
    assert client.smembers(f"repository::{R1}::blobs") == {D1}


def test_clean_registry_apply_executes_plan() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    _populate_full(client, R1, D1)
    issues = (
        Issue(category=Category.C4, digest=D1, repo=R1, redis_size=100, fs_size=None),
    )
    report = clean_registry(
        registry="reg", client=client, issues=issues, dry_run=False
    )
    assert report.planned == 1
    assert report.applied == 1
    assert client.smembers(f"repository::{R1}::blobs") == set()


def test_clean_registry_with_no_actionable_issues_is_zero() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    issues = (
        Issue(category=Category.C7, digest=D1, repo=None, redis_size=None, fs_size=10),
    )
    report = clean_registry(
        registry="reg", client=client, issues=issues, dry_run=False
    )
    assert report.planned == 0
    assert report.applied == 0


def test_clean_registry_internal_garbage_disabled_skips_c1_c6() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    issues = (
        Issue(category=Category.C1, digest=D1, repo=None, redis_size=10, fs_size=None),
        Issue(category=Category.C4, digest=D2, repo=R1, redis_size=10, fs_size=None),
        Issue(category=Category.C6, digest=D3, repo=R1, redis_size=10, fs_size=10),
    )
    report = clean_registry(
        registry="reg",
        client=client,
        issues=issues,
        dry_run=True,
        clear_internal_garbage=False,
    )
    assert report.planned == 1  # only C4


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_clean_op_is_frozen() -> None:
    op = CleanOp(category=Category.C4, digest=D1, repo=R1)
    with pytest.raises((AttributeError, Exception)):  # frozen dataclass
        op.digest = D2  # type: ignore[misc]


def test_clean_report_is_frozen() -> None:
    rep = CleanReport(
        registry="r",
        duration_s=0.0,
        planned=0,
        applied=0,
        failed=0,
        retried=0,
        failures=(),
    )
    with pytest.raises((AttributeError, Exception)):
        rep.applied = 9  # type: ignore[misc]
