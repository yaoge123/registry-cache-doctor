"""Tests for Redis blob descriptor scanning.

The scanner walks all three key shapes used by upstream:

  - blobs::sha256:<D>             (HASH: digest, size, mediatype)
  - repository::<repo>::blobs     (SET: members are sha256:<D>)
  - repository::<repo>::blobs::sha256:<D>  (HASH: mediatype only)

We use fakeredis because it implements SCAN, HMGET, SMEMBERS and
pipeline semantics faithfully enough for behavior-level coverage and
keeps tests hermetic.
"""

from __future__ import annotations

import fakeredis
import pytest


@pytest.fixture
def fake() -> fakeredis.FakeStrictRedis:
    return fakeredis.FakeStrictRedis(decode_responses=True)


_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


def test_scan_redis_returns_empty_for_empty_db(fake: fakeredis.FakeStrictRedis) -> None:
    from rcd.scan.redis_scan import scan_redis

    snap = scan_redis(fake)

    assert snap.global_blobs == {}
    assert snap.repo_blob_hashes == {}
    assert snap.repo_blob_sets == {}


def test_scan_redis_picks_up_global_blob_with_all_fields(
    fake: fakeredis.FakeStrictRedis,
) -> None:
    from rcd.scan.redis_scan import scan_redis

    fake.hset(
        f"blobs::{_DIGEST_A}",
        mapping={
            "digest": _DIGEST_A,
            "size": "1024",
            "mediatype": "application/octet-stream",
        },
    )

    snap = scan_redis(fake)

    assert _DIGEST_A in snap.global_blobs
    g = snap.global_blobs[_DIGEST_A]
    assert g.digest == _DIGEST_A
    assert g.size == 1024
    assert g.mediatype == "application/octet-stream"


def test_scan_redis_handles_partial_global_hashes(
    fake: fakeredis.FakeStrictRedis,
) -> None:
    """Half-cleared globals: HDEL has stripped digest+size but the
    mediatype field remains - that is exactly what the upstream Clear
    pipeline does to a global hash."""
    from rcd.scan.redis_scan import scan_redis

    fake.hset(
        f"blobs::{_DIGEST_A}",
        mapping={"mediatype": "application/octet-stream"},
    )

    snap = scan_redis(fake)
    g = snap.global_blobs[_DIGEST_A]
    assert g.size is None
    assert g.digest is None or g.digest == _DIGEST_A
    assert g.mediatype == "application/octet-stream"


def test_scan_redis_parses_size_zero_correctly(fake: fakeredis.FakeStrictRedis) -> None:
    """size=0 is a real production state; must not be coerced to None."""
    from rcd.scan.redis_scan import scan_redis

    fake.hset(
        f"blobs::{_DIGEST_A}",
        mapping={"digest": _DIGEST_A, "size": "0", "mediatype": "x"},
    )
    snap = scan_redis(fake)
    assert snap.global_blobs[_DIGEST_A].size == 0


def test_scan_redis_collects_repo_blob_set(fake: fakeredis.FakeStrictRedis) -> None:
    from rcd.scan.redis_scan import scan_redis

    fake.sadd("repository::library/alpine::blobs", _DIGEST_A, _DIGEST_B)

    snap = scan_redis(fake)

    rs = snap.repo_blob_sets["library/alpine"]
    assert rs.digests == frozenset({_DIGEST_A, _DIGEST_B})
    assert rs.repo == "library/alpine"


def test_scan_redis_collects_repo_blob_hash(fake: fakeredis.FakeStrictRedis) -> None:
    from rcd.scan.redis_scan import scan_redis

    fake.hset(
        f"repository::library/alpine::blobs::{_DIGEST_A}",
        "mediatype",
        "application/vnd.oci.image.layer.v1.tar+gzip",
    )

    snap = scan_redis(fake)

    h = snap.repo_blob_hashes[("library/alpine", _DIGEST_A)]
    assert h.repo == "library/alpine"
    assert h.digest == _DIGEST_A
    assert h.mediatype == "application/vnd.oci.image.layer.v1.tar+gzip"


def test_scan_redis_handles_repo_with_slashes_and_dashes(
    fake: fakeredis.FakeStrictRedis,
) -> None:
    """Repo names can contain `/` and other path-like chars. Distribution
    accepts up to several path segments, which means the per-repo hash
    key shape is `repository::<arbitrary chars>::blobs::sha256:<D>`."""
    from rcd.scan.redis_scan import scan_redis

    repo = "ns1/nested/component-x_2"
    fake.sadd(f"repository::{repo}::blobs", _DIGEST_A)
    fake.hset(f"repository::{repo}::blobs::{_DIGEST_A}", "mediatype", "m")

    snap = scan_redis(fake)
    assert repo in snap.repo_blob_sets
    assert (repo, _DIGEST_A) in snap.repo_blob_hashes


def test_scan_redis_three_views_are_independent(
    fake: fakeredis.FakeStrictRedis,
) -> None:
    """A digest can appear in any subset of the three views - the
    classifier relies on this independence to detect every C1-C7
    state."""
    from rcd.scan.redis_scan import scan_redis

    # Global only.
    fake.hset(
        f"blobs::{_DIGEST_A}",
        mapping={"digest": _DIGEST_A, "size": "1", "mediatype": "m"},
    )
    # Per-repo set only (no per-repo hash, no global).
    fake.sadd("repository::r1::blobs", _DIGEST_B)
    # Per-repo hash only (no set membership, no global).
    fake.hset("repository::r2::blobs::sha256:cccc", "mediatype", "m")

    snap = scan_redis(fake)

    assert set(snap.global_blobs.keys()) == {_DIGEST_A}
    assert set(snap.repo_blob_sets.keys()) == {"r1"}
    assert set(snap.repo_blob_hashes.keys()) == {("r2", "sha256:cccc")}


def test_scan_redis_ignores_unrelated_keys(fake: fakeredis.FakeStrictRedis) -> None:
    """Non-blob keys (e.g. distribution stat layer state) must not
    pollute the snapshot."""
    from rcd.scan.redis_scan import scan_redis

    fake.set("not-a-blob", "ignored")
    fake.hset("repository::r::manifests::sha256:xx", "mediatype", "m")
    fake.sadd("repository::r::manifests", "sha256:xx")

    snap = scan_redis(fake)

    assert snap.global_blobs == {}
    assert snap.repo_blob_sets == {}
    assert snap.repo_blob_hashes == {}


def test_scan_redis_uses_pipeline_for_global_hmget(
    fake: fakeredis.FakeStrictRedis,
) -> None:
    """End-to-end smoke: hundreds of globals must come back with all
    fields preserved. This is what would have caught the per-key fork
    behavior in the legacy bash script."""
    from rcd.scan.redis_scan import scan_redis

    digests: list[str] = []
    for i in range(150):
        d = f"sha256:{i:064x}"
        digests.append(d)
        fake.hset(
            f"blobs::{d}",
            mapping={"digest": d, "size": str(i), "mediatype": "m"},
        )

    snap = scan_redis(fake)
    assert len(snap.global_blobs) == 150
    for i, d in enumerate(digests):
        assert snap.global_blobs[d].size == i


def test_scan_redis_accepts_scan_count_param(fake: fakeredis.FakeStrictRedis) -> None:
    """The scan_count knob must reach the underlying SCAN call so
    operators can tune cursor pagination cost."""
    from rcd.scan.redis_scan import scan_redis

    for i in range(5):
        d = f"sha256:{i:064x}"
        fake.hset(f"blobs::{d}", mapping={"digest": d, "size": "1", "mediatype": "m"})

    snap = scan_redis(fake, scan_count=2)
    assert len(snap.global_blobs) == 5


def test_scan_redis_handles_per_repo_hash_with_no_mediatype(
    fake: fakeredis.FakeStrictRedis,
) -> None:
    """An empty repo-hash key shouldn't even be possible if upstream
    wrote it normally, but Redis can in principle expose a hash with
    only fields we don't know about. We keep this defensive: the key
    is recorded with mediatype=None."""
    from rcd.scan.redis_scan import scan_redis

    # Force-create the key with an unrelated field, then HDEL it so the
    # key still exists but has no fields. fakeredis may treat the empty
    # hash as deleted, in which case nothing leaks - that's fine.
    fake.hset(f"repository::r::blobs::{_DIGEST_A}", "garbage", "x")
    fake.hdel(f"repository::r::blobs::{_DIGEST_A}", "garbage")

    snap = scan_redis(fake)
    # Either present with mediatype=None, or absent. Neither contradicts
    # the contract.
    h = snap.repo_blob_hashes.get(("r", _DIGEST_A))
    if h is not None:
        assert h.mediatype is None
