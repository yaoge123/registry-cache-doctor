"""Classify (RedisSnapshot, FsSnapshot) into the C1-C7 fault matrix.

The matrix mirrors the upstream consistency contract enforced by
``distribution`` itself in
``registry/storage/cache/redis/redis.go`` and
``registry/storage/blobserver.go``:

* The repository-scoped ``Stat`` walks three independent stores
  (set membership, global hash, repo hash). A miss in any one
  yields ``ErrBlobUnknown`` and the request falls through to the
  backend, which auto-heals.
* When all three caches agree, ``ServeBlob`` writes
  ``Content-Length: desc.Size`` and streams the file. If the file
  is missing or its size disagrees with ``desc.Size``, the client
  observes ``unexpected EOF`` or a digest mismatch.

That gives us seven mutually exclusive categories per ``(repo, digest)``
or per orphan ``digest``:

================  =========================================================
Category          Meaning
================  =========================================================
:data:`Category.OK`   Cache and on-disk blob agree (incl. size).
:data:`Category.C1`   Global hash exists, no repository references it.
:data:`Category.C2`   Repo set has the digest, global hash is invalid.
:data:`Category.C3`   Repo set + valid global hash, repo hash missing.
:data:`Category.C4`   All three Redis keys present, file missing on disk.
:data:`Category.C5`   All three Redis keys present, on-disk size mismatch.
:data:`Category.C6`   Repo hash present without matching repo set membership.
:data:`Category.C7`   File on disk, no Redis reference at all.
================  =========================================================

C4 and C5 are the real failure modes (they cause client-visible errors
when the corresponding repository tag is pulled). The others are
internally inconsistent but recoverable: pull falls through to the
backend and either succeeds (C1, C2, C3, C6, C7) or remains a no-op
until the cache is rewritten.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from rcd.models import FsSnapshot, RedisSnapshot

__all__ = ["Category", "Classification", "Issue", "classify"]


class Category(StrEnum):
    """C1-C7 plus a synthetic OK bucket used for counting only."""

    OK = "ok"
    C1 = "c1"
    C2 = "c2"
    C3 = "c3"
    C4 = "c4"
    C5 = "c5"
    C6 = "c6"
    C7 = "c7"


@dataclass(frozen=True, slots=True)
class Issue:
    """A single classified anomaly.

    Attributes
    ----------
    category:
        The matrix bucket. Only categories ``C1``..``C7`` are emitted as
        :class:`Issue` records; ``OK`` exists only in the
        :attr:`Classification.counts` tally.
    digest:
        Canonical ``sha256:<hex>`` form.
    repo:
        Owning repository for repo-scoped issues. ``None`` for ``C1`` and
        ``C7`` which are global to the digest.
    redis_size:
        Size recorded in the global ``blobs::sha256:<D>`` hash, or
        ``None`` when the global hash is missing or has no ``size``
        field.
    fs_size:
        Size of the on-disk ``data`` file, or ``None`` when absent.
    """

    category: Category
    digest: str
    repo: str | None
    redis_size: int | None
    fs_size: int | None


@dataclass(frozen=True, slots=True)
class Classification:
    """Result of classifying a (RedisSnapshot, FsSnapshot) pair."""

    issues: tuple[Issue, ...]
    counts: Mapping[Category, int]


def _global_valid(redis: RedisSnapshot, digest: str) -> bool:
    """Mirror upstream HMGET semantics.

    distribution treats ``redis.Nil`` on either ``digest`` or ``size`` as
    ``ErrBlobUnknown``. We only check ``size`` because:

    * ``digest`` is reconstructible from the key itself.
    * If ``size`` was HDEL'd, the upstream :class:`Stat` returns
      ``ErrBlobUnknown`` regardless.
    """
    blob = redis.global_blobs.get(digest)
    return blob is not None and blob.size is not None


def classify(redis: RedisSnapshot, fs: FsSnapshot) -> Classification:
    """Compute the classification for a pair of snapshots.

    Parameters
    ----------
    redis:
        A snapshot of the registry's Redis cache as produced by
        :func:`rcd.scan.redis_scan.scan_redis`.
    fs:
        A snapshot of the storage tree as produced by
        :func:`rcd.scan.fs_scan.scan_fs`.

    Returns
    -------
    Classification
        ``issues`` enumerates every (digest, optional repo) pair whose
        category is not :data:`Category.OK`. ``counts`` is a complete
        tally of every :class:`Category` value (zero buckets included).
    """
    issues: list[Issue] = []
    counts: dict[Category, int] = dict.fromkeys(Category, 0)

    # Index of which repos reference each digest via their blob set.
    refs_by_digest: dict[str, set[str]] = {}
    for repo, blob_set in redis.repo_blob_sets.items():
        for digest in blob_set.digests:
            refs_by_digest.setdefault(digest, set()).add(repo)

    # Track every digest we touch so we can find C7 / C1 leftovers.
    seen_digests: set[str] = set()

    # ---- Repo-scoped pass ---------------------------------------------------
    # Walk every (repo, digest) pair that appears in either the repo set or
    # the repo hash. The union catches C2/C3/C4/C5 (set-driven) and C6
    # (hash-driven without set membership).
    pairs: set[tuple[str, str]] = set()
    for repo, blob_set in redis.repo_blob_sets.items():
        for digest in blob_set.digests:
            pairs.add((repo, digest))
    pairs.update(redis.repo_blob_hashes.keys())

    for repo, digest in pairs:
        seen_digests.add(digest)
        in_set = repo in redis.repo_blob_sets and digest in redis.repo_blob_sets[repo].digests
        in_repo_hash = (repo, digest) in redis.repo_blob_hashes
        global_ok = _global_valid(redis, digest)
        fs_blob = fs.blobs.get(digest)
        global_blob = redis.global_blobs.get(digest)

        redis_size = global_blob.size if global_blob is not None else None
        fs_size = fs_blob.size if fs_blob is not None else None

        if not in_set and in_repo_hash:
            # C6: repo hash present but the repo set does not (or no longer)
            # contains the digest. Pull would fail at SISMEMBER first and
            # auto-heal via the backend.
            cat = Category.C6
        elif in_set and not global_ok:
            # C2: repo set says the digest belongs to this repo, but the
            # global hash is missing or has no size.
            cat = Category.C2
        elif in_set and global_ok and not in_repo_hash:
            # C3: set + global hash valid, repo hash missing.
            cat = Category.C3
        elif in_set and global_ok and in_repo_hash and fs_blob is None:
            # C4: cache fully consistent but the on-disk file is absent.
            # Real failure mode: ServeBlob streams a 0-byte body with the
            # cached Content-Length -> client observes unexpected EOF.
            cat = Category.C4
        elif (
            in_set
            and global_ok
            and in_repo_hash
            and fs_blob is not None
            and redis_size != fs_size
        ):
            # C5: cache fully consistent but the on-disk size differs.
            # http.ServeContent honours Content-Length from the cache,
            # producing either truncation or unexpected EOF.
            cat = Category.C5
        else:
            # Everything aligns -> OK; not emitted as an Issue.
            counts[Category.OK] += 1
            continue

        counts[cat] += 1
        issues.append(
            Issue(
                category=cat,
                digest=digest,
                repo=repo,
                redis_size=redis_size,
                fs_size=fs_size,
            )
        )

    # ---- Global pass --------------------------------------------------------
    # Any global ``blobs::sha256:<D>`` whose digest is not referenced by any
    # repository -> C1 (orphan in cache; pull never hits this entry).
    for digest, global_blob in redis.global_blobs.items():
        if digest in refs_by_digest:
            continue
        seen_digests.add(digest)
        fs_blob = fs.blobs.get(digest)
        counts[Category.C1] += 1
        issues.append(
            Issue(
                category=Category.C1,
                digest=digest,
                repo=None,
                redis_size=global_blob.size,
                fs_size=fs_blob.size if fs_blob is not None else None,
            )
        )

    # ---- Filesystem leftovers ----------------------------------------------
    # Files on disk with no Redis reference at all -> C7. Pull will go to the
    # backend and the file becomes referenced again on success (self-healing).
    for digest, fs_blob in fs.blobs.items():
        if digest in seen_digests:
            continue
        if digest in redis.global_blobs:
            # Already covered by the C1 branch above.
            continue
        counts[Category.C7] += 1
        issues.append(
            Issue(
                category=Category.C7,
                digest=digest,
                repo=None,
                redis_size=None,
                fs_size=fs_blob.size,
            )
        )

    return Classification(issues=tuple(issues), counts=counts)
