"""Tests for the C1-C7 classifier.

The matrix follows the upstream behaviour of distribution's redis cache
(see ``registry/storage/cache/redis/redis.go``):

* a = ``(repo, digest)`` is a member of ``repository::<repo>::blobs``
* b = ``repository::<repo>::blobs::sha256:<D>`` exists
* g = global ``blobs::sha256:<D>`` is *valid* (key present **and** size field present);
      this matches the upstream ``Stat`` which fails on a missing size field
* f = ``<root>/docker/registry/v2/blobs/sha256/<2>/<D>/data`` exists
"""

from __future__ import annotations

from rcd.classifier import Category, Classification, Issue, classify
from rcd.models import (
    FsBlob,
    FsSnapshot,
    GlobalBlob,
    RedisSnapshot,
    RepoBlobHash,
    RepoBlobSet,
)

D1 = "sha256:" + "11" * 32
D2 = "sha256:" + "22" * 32
D3 = "sha256:" + "33" * 32
D4 = "sha256:" + "44" * 32

R1 = "library/alpine"
R2 = "library/busybox"


def make_snap(
    *,
    global_blobs: list[GlobalBlob] | None = None,
    repo_sets: list[RepoBlobSet] | None = None,
    repo_hashes: list[RepoBlobHash] | None = None,
    fs_blobs: list[FsBlob] | None = None,
) -> tuple[RedisSnapshot, FsSnapshot]:
    redis = RedisSnapshot(
        global_blobs={b.digest: b for b in (global_blobs or [])},
        repo_blob_hashes={(h.repo, h.digest): h for h in (repo_hashes or [])},
        repo_blob_sets={s.repo: s for s in (repo_sets or [])},
    )
    fs = FsSnapshot(blobs={b.digest: b for b in (fs_blobs or [])})
    return redis, fs


# ---------------------------------------------------------------------------
# Empty / OK
# ---------------------------------------------------------------------------


def test_classify_empty_returns_zero_counts() -> None:
    redis, fs = make_snap()
    result = classify(redis, fs)
    assert isinstance(result, Classification)
    assert result.issues == ()
    assert result.counts == dict.fromkeys(Category, 0)


def test_ok_size_match_yields_no_issue_but_ok_count() -> None:
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=100, mediatype="application/octet-stream")],
        repo_sets=[RepoBlobSet(repo=R1, digests=frozenset({D1}))],
        repo_hashes=[RepoBlobHash(repo=R1, digest=D1, mediatype="application/octet-stream")],
        fs_blobs=[FsBlob(digest=D1, size=100)],
    )
    result = classify(redis, fs)
    assert result.issues == ()
    assert result.counts[Category.OK] == 1
    assert all(result.counts[c] == 0 for c in Category if c is not Category.OK)


# ---------------------------------------------------------------------------
# C1 - global hash only, no repo refs
# ---------------------------------------------------------------------------


def test_c1_global_only_no_repo_refs() -> None:
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=100, mediatype="application/octet-stream")],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C1] == 1
    issue = next(i for i in result.issues if i.category is Category.C1)
    assert issue.digest == D1
    assert issue.repo is None
    assert issue.redis_size == 100
    assert issue.fs_size is None


def test_c1_global_only_with_fs_present() -> None:
    """If the digest has no repo refs, the on-disk file does not change classification.

    Pull would not hit cache (SISMEMBER fails first); orphan-ness is what matters.
    """
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=100, mediatype="application/octet-stream")],
        fs_blobs=[FsBlob(digest=D1, size=100)],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C1] == 1
    issue = next(i for i in result.issues if i.category is Category.C1)
    assert issue.fs_size == 100


# ---------------------------------------------------------------------------
# C2 - repo set has D, global stat broken
# ---------------------------------------------------------------------------


def test_c2_repo_set_with_global_key_missing() -> None:
    redis, fs = make_snap(
        repo_sets=[RepoBlobSet(repo=R1, digests=frozenset({D1}))],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C2] == 1
    issue = next(i for i in result.issues if i.category is Category.C2)
    assert issue.repo == R1
    assert issue.digest == D1
    assert issue.redis_size is None


def test_c2_repo_set_with_global_size_field_missing() -> None:
    """Global hash key exists but size field has been HDEL'd.

    Upstream redis.go HMGET treats missing size as ErrBlobUnknown, so this
    is functionally equivalent to a missing global hash for pull purposes.
    """
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=None, mediatype="application/octet-stream")],
        repo_sets=[RepoBlobSet(repo=R1, digests=frozenset({D1}))],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C2] == 1


def test_c2_repo_set_and_repo_hash_with_global_missing() -> None:
    """repo set + repo hash both present but global stat broken -> still C2."""
    redis, fs = make_snap(
        repo_sets=[RepoBlobSet(repo=R1, digests=frozenset({D1}))],
        repo_hashes=[RepoBlobHash(repo=R1, digest=D1, mediatype="application/octet-stream")],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C2] == 1
    assert result.counts[Category.C3] == 0


# ---------------------------------------------------------------------------
# C3 - repo set + global ok, repo hash missing
# ---------------------------------------------------------------------------


def test_c3_repo_set_global_ok_repo_hash_missing() -> None:
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=100, mediatype="application/octet-stream")],
        repo_sets=[RepoBlobSet(repo=R1, digests=frozenset({D1}))],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C3] == 1
    issue = next(i for i in result.issues if i.category is Category.C3)
    assert issue.repo == R1
    assert issue.redis_size == 100


# ---------------------------------------------------------------------------
# C4 - all redis three present, fs missing (real failure)
# ---------------------------------------------------------------------------


def test_c4_all_redis_three_fs_missing() -> None:
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=100, mediatype="application/octet-stream")],
        repo_sets=[RepoBlobSet(repo=R1, digests=frozenset({D1}))],
        repo_hashes=[RepoBlobHash(repo=R1, digest=D1, mediatype="application/octet-stream")],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C4] == 1
    issue = next(i for i in result.issues if i.category is Category.C4)
    assert issue.repo == R1
    assert issue.redis_size == 100
    assert issue.fs_size is None


# ---------------------------------------------------------------------------
# C5 - all redis three, fs present, size mismatch (real failure)
# ---------------------------------------------------------------------------


def test_c5_size_mismatch() -> None:
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=7232, mediatype="application/octet-stream")],
        repo_sets=[RepoBlobSet(repo=R1, digests=frozenset({D1}))],
        repo_hashes=[RepoBlobHash(repo=R1, digest=D1, mediatype="application/octet-stream")],
        fs_blobs=[FsBlob(digest=D1, size=374)],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C5] == 1
    issue = next(i for i in result.issues if i.category is Category.C5)
    assert issue.redis_size == 7232
    assert issue.fs_size == 374


def test_c5_size_zero_in_redis_with_fs_nonzero() -> None:
    """Real production scenario: redis size=0 but fs file is non-empty."""
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=0, mediatype="application/octet-stream")],
        repo_sets=[RepoBlobSet(repo=R1, digests=frozenset({D1}))],
        repo_hashes=[RepoBlobHash(repo=R1, digest=D1, mediatype="application/octet-stream")],
        fs_blobs=[FsBlob(digest=D1, size=685)],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C5] == 1


# ---------------------------------------------------------------------------
# C6 - repo hash present, repo set does not contain it
# ---------------------------------------------------------------------------


def test_c6_repo_hash_orphan_global_present() -> None:
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=100, mediatype="application/octet-stream")],
        repo_hashes=[RepoBlobHash(repo=R1, digest=D1, mediatype="application/octet-stream")],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C6] == 1
    issue = next(i for i in result.issues if i.category is Category.C6)
    assert issue.repo == R1
    assert issue.digest == D1


def test_c6_repo_hash_orphan_global_missing() -> None:
    redis, fs = make_snap(
        repo_hashes=[RepoBlobHash(repo=R1, digest=D1, mediatype="application/octet-stream")],
    )
    result = classify(redis, fs)
    # repo hash exists but neither repo set nor global -> still C6
    # (pull would fail at SISMEMBER; the orphan repo hash key is the issue)
    assert result.counts[Category.C6] == 1


def test_c6_when_digest_referenced_by_another_repo() -> None:
    """R1 has an orphan repo hash; the digest is legitimately used by R2.

    Both records are emitted: R2/D1 is OK (fs matches), R1/D1 is C6.
    The C6 entry must not be suppressed by the cross-repo membership.
    """
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=100, mediatype="application/octet-stream")],
        repo_sets=[RepoBlobSet(repo=R2, digests=frozenset({D1}))],
        repo_hashes=[
            RepoBlobHash(repo=R1, digest=D1, mediatype="application/octet-stream"),
            RepoBlobHash(repo=R2, digest=D1, mediatype="application/octet-stream"),
        ],
        fs_blobs=[FsBlob(digest=D1, size=100)],
    )
    result = classify(redis, fs)
    assert result.counts[Category.OK] == 1
    assert result.counts[Category.C6] == 1
    assert result.counts[Category.C7] == 0
    c6 = next(i for i in result.issues if i.category is Category.C6)
    assert c6.repo == R1


# ---------------------------------------------------------------------------
# C7 - fs only, no redis refs at all
# ---------------------------------------------------------------------------


def test_c7_fs_only_no_redis_refs() -> None:
    redis, fs = make_snap(
        fs_blobs=[FsBlob(digest=D1, size=100)],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C7] == 1
    issue = next(i for i in result.issues if i.category is Category.C7)
    assert issue.repo is None
    assert issue.fs_size == 100
    assert issue.redis_size is None


def test_c7_excluded_when_global_hash_exists() -> None:
    """If global hash is present (no repo refs), it's C1 not C7."""
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=100, mediatype="application/octet-stream")],
        fs_blobs=[FsBlob(digest=D1, size=100)],
    )
    result = classify(redis, fs)
    assert result.counts[Category.C7] == 0
    assert result.counts[Category.C1] == 1


# ---------------------------------------------------------------------------
# Mixed scenarios
# ---------------------------------------------------------------------------


def test_mixed_scenario_with_multiple_categories() -> None:
    """One snapshot exercising several categories in parallel."""
    redis, fs = make_snap(
        global_blobs=[
            GlobalBlob(digest=D1, size=100, mediatype="application/octet-stream"),  # OK
            GlobalBlob(digest=D2, size=200, mediatype="application/octet-stream"),  # C5
            GlobalBlob(digest=D3, size=300, mediatype="application/octet-stream"),  # C1
        ],
        repo_sets=[
            RepoBlobSet(repo=R1, digests=frozenset({D1, D2})),
            RepoBlobSet(repo=R2, digests=frozenset({D1})),  # D1 referenced twice -> still OK
        ],
        repo_hashes=[
            RepoBlobHash(repo=R1, digest=D1, mediatype="application/octet-stream"),
            RepoBlobHash(repo=R1, digest=D2, mediatype="application/octet-stream"),
            RepoBlobHash(repo=R2, digest=D1, mediatype="application/octet-stream"),
        ],
        fs_blobs=[
            FsBlob(digest=D1, size=100),  # backs OK
            FsBlob(digest=D2, size=999),  # size mismatch -> C5
            FsBlob(digest=D4, size=400),  # C7 (no redis ref)
        ],
    )
    result = classify(redis, fs)
    # Two OK pairs (R1/D1, R2/D1)
    assert result.counts[Category.OK] == 2
    # One C5 (R1/D2)
    assert result.counts[Category.C5] == 1
    # One C1 (D3 only in global)
    assert result.counts[Category.C1] == 1
    # One C7 (D4 only on fs)
    assert result.counts[Category.C7] == 1
    # No others
    for c in (Category.C2, Category.C3, Category.C4, Category.C6):
        assert result.counts[c] == 0


def test_same_digest_same_repo_only_counted_once() -> None:
    """Idempotency on duplicate inputs (set membership is unique by definition)."""
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=100, mediatype="application/octet-stream")],
        repo_sets=[RepoBlobSet(repo=R1, digests=frozenset({D1}))],
        repo_hashes=[RepoBlobHash(repo=R1, digest=D1, mediatype="application/octet-stream")],
        fs_blobs=[FsBlob(digest=D1, size=100)],
    )
    result = classify(redis, fs)
    assert result.counts[Category.OK] == 1


def test_issues_is_immutable_tuple() -> None:
    redis, fs = make_snap(
        global_blobs=[GlobalBlob(digest=D1, size=100, mediatype="application/octet-stream")],
    )
    result = classify(redis, fs)
    assert isinstance(result.issues, tuple)


def test_issue_dataclass_is_frozen() -> None:
    issue = Issue(
        category=Category.C5,
        digest=D1,
        repo=R1,
        redis_size=7232,
        fs_size=374,
    )
    assert issue.category is Category.C5
    # Frozen dataclass: assignment must raise
    import dataclasses

    try:
        issue.redis_size = 999  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("Issue should be frozen")
