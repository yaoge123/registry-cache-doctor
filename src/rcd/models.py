"""Phase 1 data models.

These dataclasses are pure value types describing what was observed in
Redis and on disk for a single registry. They are intentionally separate
from the classifier (`rcd.classifier`) so the snapshot layer can be reused
for `inspect`/`scan`/`clean` without any classification logic dependency.

All snapshot-level types are frozen.
Field semantics follow the upstream `distribution/distribution` Redis
blob descriptor cache (`registry/storage/cache/redis/redis.go`):

  - Global hash:   blobs::sha256:<D>             {digest, size, mediatype}
  - Per-repo set:  repository::<repo>::blobs     SET<sha256:<D>>
  - Per-repo hash: repository::<repo>::blobs::sha256:<D>  {mediatype}
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class GlobalBlob:
    """Snapshot of a single `blobs::sha256:<D>` Redis hash.

    `size` and `mediatype` are nullable: the upstream code can write any
    subset of the three fields independently (in particular `mediatype`
    is set with HSetNX, `digest`+`size` are set with HMSET, and the
    cleaner uses HDEL on individual fields rather than DEL of the whole
    hash). Either of `digest` or `size` being missing makes the global
    `Stat()` call return ErrBlobUnknown.
    """

    digest: str
    size: int | None
    mediatype: str | None


@dataclass(frozen=True, slots=True)
class RepoBlobHash:
    """Snapshot of a single `repository::<repo>::blobs::sha256:<D>` hash.

    Distribution writes only `mediatype` here, as a per-repository
    override. The hash can also exist with no fields if the per-repo
    side was partially cleared (HDEL but no DEL).
    """

    repo: str
    digest: str
    mediatype: str | None


@dataclass(frozen=True, slots=True)
class RepoBlobSet:
    """Snapshot of a single `repository::<repo>::blobs` Redis set."""

    repo: str
    digests: frozenset[str]

    def __init__(self, repo: str, digests: Iterable[str]) -> None:
        # Coerce any iterable (including mutable sets) into a frozenset
        # so callers can't mutate snapshot state via aliasing.
        object.__setattr__(self, "repo", repo)
        object.__setattr__(self, "digests", frozenset(digests))


@dataclass(frozen=True, slots=True)
class RedisSnapshot:
    """All Redis state observed for a single registry."""

    global_blobs: Mapping[str, GlobalBlob] = field(default_factory=dict)
    repo_blob_hashes: Mapping[tuple[str, str], RepoBlobHash] = field(default_factory=dict)
    repo_blob_sets: Mapping[str, RepoBlobSet] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FsBlob:
    """A single on-disk blob descriptor.

    Only `size` is captured from a `stat()` call. Distribution stores
    the bytes at `<root>/docker/registry/v2/blobs/sha256/<prefix>/<full>/data`,
    so a missing `data` file means the digest is functionally absent
    from storage even if the parent directory exists.
    """

    digest: str
    size: int


@dataclass(frozen=True, slots=True)
class FsSnapshot:
    """All on-disk blob state observed for a single registry."""

    blobs: Mapping[str, FsBlob] = field(default_factory=dict)
