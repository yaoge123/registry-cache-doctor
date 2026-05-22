"""Tests for the NDJSON serializer (Phase 3)."""

from __future__ import annotations

import io
import json

import pytest

from rcd.classifier import Category, Classification, Issue
from rcd.output.ndjson import (
    emit_clean_completed,
    emit_inspect,
    emit_run_summary,
    emit_scan_completed,
)
from rcd.report import RedisKeyCounts, RegistryReport


def _decode_one(buf: io.StringIO) -> dict[str, object]:
    text = buf.getvalue()
    assert text.endswith("\n"), "every NDJSON record must end with a newline"
    assert text.count("\n") == 1, "single event must produce exactly one line"
    return json.loads(text.rstrip("\n"))  # type: ignore[no-any-return]


def _make_report(
    *,
    registry: str = "test-registry",
    duration_s: float = 1.5,
    counts: dict[Category, int] | None = None,
    issues: tuple[Issue, ...] = (),
    redis_keys: RedisKeyCounts | None = None,
    errors: tuple[str, ...] = (),
) -> RegistryReport:
    base = dict.fromkeys(Category, 0)
    if counts:
        base.update(counts)
    return RegistryReport(
        registry=registry,
        duration_s=duration_s,
        redis_keys=redis_keys or RedisKeyCounts(global_hash=0, repo_set=0, repo_hash=0),
        classification=Classification(issues=issues, counts=base),
        errors=errors,
    )


def test_scan_completed_emits_required_fields() -> None:
    buf = io.StringIO()
    report = _make_report(
        registry="myreg",
        duration_s=42.7,
        counts={Category.OK: 1000, Category.C5: 7},
        redis_keys=RedisKeyCounts(global_hash=100, repo_set=20, repo_hash=500),
    )

    emit_scan_completed(buf, report=report, run_id="abc12345")

    obj = _decode_one(buf)
    assert obj["schema_version"] == 1
    assert obj["tool"] == "rcd"
    assert obj["event"] == "scan_completed"
    assert obj["registry"] == "myreg"
    assert obj["duration_s"] == 42.7
    assert obj["run_id"] == "abc12345"
    assert obj["redis_keys"] == {
        "global": 100,
        "repo_set": 20,
        "repo_hash": 500,
    }
    assert obj["categories"]["c5"] == 7
    assert obj["categories"]["ok"] == 1000
    # Every Category key must appear, even at zero, so jq filters work.
    for cat in Category:
        assert cat.value in obj["categories"]
    # Always include errors list (possibly empty).
    assert obj["errors"] == []
    # Timestamp must be a UTC ISO-8601 string ending in 'Z' or '+00:00'.
    ts = obj["ts"]
    assert isinstance(ts, str)
    assert ts.endswith("Z") or ts.endswith("+00:00")


def test_scan_completed_includes_errors_when_present() -> None:
    buf = io.StringIO()
    report = _make_report(errors=("fs scandir oserror: permission denied",))

    emit_scan_completed(buf, report=report, run_id="r")

    assert _decode_one(buf)["errors"] == ["fs scandir oserror: permission denied"]


def test_scan_completed_does_not_include_digest_list_by_default() -> None:
    buf = io.StringIO()
    report = _make_report(
        issues=(
            Issue(
                category=Category.C5,
                digest="sha256:" + "ab" * 32,
                repo="library/alpine",
                redis_size=7232,
                fs_size=374,
            ),
        ),
        counts={Category.C5: 1},
    )

    emit_scan_completed(buf, report=report, run_id="r")

    obj = _decode_one(buf)
    assert "issues" not in obj
    assert "digests" not in obj


def test_run_summary_aggregates_totals() -> None:
    buf = io.StringIO()
    totals = dict.fromkeys(Category, 0)
    totals[Category.C4] = 2
    totals[Category.C5] = 5
    totals[Category.OK] = 1000

    emit_run_summary(buf, run_id="abc", duration_s=120.5, totals=totals)

    obj = _decode_one(buf)
    assert obj["event"] == "run_summary"
    assert obj["schema_version"] == 1
    assert obj["run_id"] == "abc"
    assert obj["duration_s"] == 120.5
    assert obj["totals"]["c4"] == 2
    assert obj["totals"]["c5"] == 5
    assert obj["totals"]["ok"] == 1000


def test_clean_completed_event_shape() -> None:
    buf = io.StringIO()

    emit_clean_completed(
        buf,
        run_id="r",
        registry="myreg",
        planned=10,
        applied=8,
        failed=2,
        retried=1,
        duration_s=3.2,
    )

    obj = _decode_one(buf)
    assert obj["event"] == "clean_completed"
    assert obj["registry"] == "myreg"
    assert obj["planned"] == 10
    assert obj["applied"] == 8
    assert obj["failed"] == 2
    assert obj["retried"] == 1
    assert obj["duration_s"] == 3.2


def test_each_event_writes_a_single_line() -> None:
    buf = io.StringIO()
    report = _make_report()

    emit_scan_completed(buf, report=report, run_id="r")
    emit_run_summary(buf, run_id="r", duration_s=1.0, totals=dict.fromkeys(Category, 0))

    lines = buf.getvalue().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # well-formed JSON per line


def test_records_are_compact_no_pretty_printing() -> None:
    buf = io.StringIO()

    emit_run_summary(buf, run_id="r", duration_s=1.0, totals=dict.fromkeys(Category, 0))

    text = buf.getvalue()
    assert "\n" in text
    # Compact JSON should not have spaces after separators.
    assert ": " not in text
    assert ", " not in text


@pytest.mark.parametrize("bad_run_id", ["", "abc-with-newline\n", "two\nlines"])
def test_run_id_must_be_a_single_safe_token(bad_run_id: str) -> None:
    buf = io.StringIO()
    report = _make_report()

    with pytest.raises(ValueError):
        emit_scan_completed(buf, report=report, run_id=bad_run_id)


def test_inspect_event_shape() -> None:
    buf = io.StringIO()
    issue = Issue(
        category=Category.C5,
        digest="sha256:" + "ab" * 32,
        repo="library/alpine",
        redis_size=7232,
        fs_size=374,
    )

    emit_inspect(buf, run_id="r", registry="myreg", issue=issue)

    obj = _decode_one(buf)
    assert obj["schema_version"] == 1
    assert obj["tool"] == "rcd"
    assert obj["event"] == "inspect"
    assert obj["registry"] == "myreg"
    assert obj["category"] == "c5"
    assert obj["digest"] == "sha256:" + "ab" * 32
    assert obj["repo"] == "library/alpine"
    assert obj["redis_size"] == 7232
    assert obj["fs_size"] == 374
    assert "refs" not in obj


def test_inspect_event_includes_refs_when_provided() -> None:
    buf = io.StringIO()
    issue = Issue(
        category=Category.C1,
        digest="sha256:" + "cd" * 32,
        repo=None,
        redis_size=1024,
        fs_size=None,
    )

    emit_inspect(
        buf,
        run_id="r",
        registry="myreg",
        issue=issue,
        refs=("library/alpine", "library/busybox"),
    )

    obj = _decode_one(buf)
    assert obj["repo"] is None
    assert obj["fs_size"] is None
    assert obj["refs"] == ["library/alpine", "library/busybox"]


def test_inspect_event_with_empty_refs_still_emits_field() -> None:
    buf = io.StringIO()
    issue = Issue(
        category=Category.C1,
        digest="sha256:" + "ef" * 32,
        repo=None,
        redis_size=512,
        fs_size=None,
    )

    emit_inspect(buf, run_id="r", registry="myreg", issue=issue, refs=())

    obj = _decode_one(buf)
    assert obj["refs"] == []
