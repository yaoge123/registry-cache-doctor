"""Tests for Phase 1 data models.

These are immutable, structural value types. They carry no behavior.
The point of testing them is to lock the field surface so downstream
classifier/cleaner code can rely on a stable schema.
"""

from __future__ import annotations

import dataclasses

import pytest


def test_global_blob_fields() -> None:
    from rcd.models import GlobalBlob

    blob = GlobalBlob(
        digest="sha256:" + "a" * 64,
        size=1024,
        mediatype="application/octet-stream",
    )
    assert blob.digest == "sha256:" + "a" * 64
    assert blob.size == 1024
    assert blob.mediatype == "application/octet-stream"
    # All fields nullable except digest: digest/size/mediatype each missing
    # corresponds to a real distribution failure mode.
    GlobalBlob(digest="sha256:b" * 65, size=None, mediatype=None)


def test_global_blob_is_frozen() -> None:
    from rcd.models import GlobalBlob

    blob = GlobalBlob(digest="sha256:" + "a" * 64, size=1, mediatype="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        blob.size = 2  # type: ignore[misc]


def test_repo_blob_hash_fields() -> None:
    from rcd.models import RepoBlobHash

    rh = RepoBlobHash(
        repo="library/alpine",
        digest="sha256:" + "c" * 64,
        mediatype="application/vnd.oci.image.layer.v1.tar+gzip",
    )
    assert rh.repo == "library/alpine"
    assert rh.digest == "sha256:" + "c" * 64
    assert rh.mediatype is not None
    # mediatype field can be missing - upstream sets it via HSetNX so the
    # field may be absent if a parallel writer already populated it.
    RepoBlobHash(repo="x", digest="sha256:d" * 65, mediatype=None)


def test_repo_blob_set_fields() -> None:
    from rcd.models import RepoBlobSet

    s = RepoBlobSet(repo="library/alpine", digests={"sha256:e" * 65, "sha256:f" * 65})
    assert s.repo == "library/alpine"
    assert isinstance(s.digests, frozenset)
    assert len(s.digests) == 2


def test_repo_blob_set_freezes_input() -> None:
    """Mutability of input set must not leak into stored state."""
    from rcd.models import RepoBlobSet

    src: set[str] = {"sha256:" + "a" * 64}
    s = RepoBlobSet(repo="r", digests=src)
    src.add("sha256:" + "b" * 64)
    assert "sha256:" + "b" * 64 not in s.digests


def test_redis_snapshot_collects_three_views() -> None:
    from rcd.models import GlobalBlob, RedisSnapshot, RepoBlobHash, RepoBlobSet

    snap = RedisSnapshot(
        global_blobs={
            "sha256:" + "a" * 64: GlobalBlob(
                digest="sha256:" + "a" * 64, size=10, mediatype="m"
            ),
        },
        repo_blob_hashes={
            ("r1", "sha256:" + "a" * 64): RepoBlobHash(
                repo="r1", digest="sha256:" + "a" * 64, mediatype="m"
            ),
        },
        repo_blob_sets={
            "r1": RepoBlobSet(repo="r1", digests={"sha256:" + "a" * 64}),
        },
    )
    assert snap.global_blobs["sha256:" + "a" * 64].size == 10
    assert snap.repo_blob_hashes[("r1", "sha256:" + "a" * 64)].mediatype == "m"
    assert "sha256:" + "a" * 64 in snap.repo_blob_sets["r1"].digests


def test_fs_blob_fields() -> None:
    from rcd.models import FsBlob

    f = FsBlob(digest="sha256:" + "1" * 64, size=4096)
    assert f.digest == "sha256:" + "1" * 64
    assert f.size == 4096


def test_fs_snapshot_indexes_by_digest() -> None:
    from rcd.models import FsBlob, FsSnapshot

    digest = "sha256:" + "2" * 64
    snap = FsSnapshot(blobs={digest: FsBlob(digest=digest, size=42)})
    assert snap.blobs[digest].size == 42
