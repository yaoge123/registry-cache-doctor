"""Redis-side blob descriptor scanner.

Walks the three key shapes used by the upstream
`registry/storage/cache/redis/redis.go`:

  - `blobs::sha256:<D>`                       (HASH: digest, size, mediatype)
  - `repository::<repo>::blobs`               (SET of `sha256:<D>`)
  - `repository::<repo>::blobs::sha256:<D>`   (HASH: mediatype)

Two `SCAN` passes are issued (one per top-level prefix) and HMGET/SMEMBERS
are fanned out via a non-transactional pipeline. We do **not** EVAL or MULTI:
upstream itself uses non-atomic pipelines, and 800K-key keyspaces would
otherwise stall the single-threaded Redis main loop.

Repository names cannot contain `::` per upstream naming rules, so a
plain `key.split("::")` parse is safe.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

from rcd.models import GlobalBlob, RedisSnapshot, RepoBlobHash, RepoBlobSet


class _RedisClient(Protocol):
    def scan_iter(self, match: str, count: int) -> Any: ...
    def pipeline(self, transaction: bool = ...) -> Any: ...


_DEFAULT_SCAN_COUNT = 1000
_GLOBAL_PREFIX = "blobs::"
_REPO_PREFIX = "repository::"
_DIGEST_PREFIX = "sha256:"


def scan_redis(client: _RedisClient, scan_count: int = _DEFAULT_SCAN_COUNT) -> RedisSnapshot:
    """Take a snapshot of the three-view blob descriptor cache."""
    global_keys: list[str] = []
    repo_set_pairs: list[tuple[str, str]] = []  # (key, repo)
    repo_hash_triples: list[tuple[str, str, str]] = []  # (key, repo, digest)

    # Pass 1: global hashes.
    for raw in client.scan_iter(match=f"{_GLOBAL_PREFIX}{_DIGEST_PREFIX}*", count=scan_count):
        key = _decode(raw)
        # Defensive: SCAN MATCH is glob, so still check the shape exactly.
        parts = key.split("::")
        if len(parts) == 2 and parts[0] == "blobs" and parts[1].startswith(_DIGEST_PREFIX):
            global_keys.append(key)

    # Pass 2: per-repo set/hash. The same MATCH catches both shapes
    # plus other `repository::*` keys (manifests etc.); we filter
    # client-side by exact part shape.
    for raw in client.scan_iter(match=f"{_REPO_PREFIX}*", count=scan_count):
        key = _decode(raw)
        parts = key.split("::")
        if (
            len(parts) == 3
            and parts[0] == "repository"
            and parts[2] == "blobs"
        ):
            repo_set_pairs.append((key, parts[1]))
        elif (
            len(parts) == 4
            and parts[0] == "repository"
            and parts[2] == "blobs"
            and parts[3].startswith(_DIGEST_PREFIX)
        ):
            repo_hash_triples.append((key, parts[1], parts[3]))

    global_blobs = _fetch_globals(client, global_keys)
    repo_blob_hashes = _fetch_repo_hashes(client, repo_hash_triples)
    repo_blob_sets = _fetch_repo_sets(client, repo_set_pairs)

    return RedisSnapshot(
        global_blobs=global_blobs,
        repo_blob_hashes=repo_blob_hashes,
        repo_blob_sets=repo_blob_sets,
    )


def _fetch_globals(
    client: _RedisClient, keys: list[str]
) -> dict[str, GlobalBlob]:
    out: dict[str, GlobalBlob] = {}
    if not keys:
        return out
    pipe = client.pipeline(transaction=False)
    for key in keys:
        pipe.hmget(key, "digest", "size", "mediatype")
    results = cast(list[list[Any]], pipe.execute())
    for key, fields in zip(keys, results, strict=True):
        digest_field, size_field, mediatype_field = fields
        digest = key[len(_GLOBAL_PREFIX) :]  # canonical digest from key
        size = _to_int(size_field)
        mediatype = _to_str(mediatype_field)
        # If both `digest` and `size` came back None the HMGET-returned
        # `digest_field` matches: surface that as size=None so the
        # classifier sees a partial global. We store digest from the key
        # since it's the only invariant identifier.
        _ = digest_field  # kept for future field-level diagnostics
        out[digest] = GlobalBlob(digest=digest, size=size, mediatype=mediatype)
    return out


def _fetch_repo_hashes(
    client: _RedisClient, triples: list[tuple[str, str, str]]
) -> dict[tuple[str, str], RepoBlobHash]:
    out: dict[tuple[str, str], RepoBlobHash] = {}
    if not triples:
        return out
    pipe = client.pipeline(transaction=False)
    for key, _repo, _digest in triples:
        pipe.hget(key, "mediatype")
    results = cast(list[Any], pipe.execute())
    for (_, repo, digest), value in zip(triples, results, strict=True):
        # An HGET on a missing field returns None; we keep these because
        # the key still exists and that's a real cache state worth
        # classifying (HDEL "mediatype" without DEL).
        out[(repo, digest)] = RepoBlobHash(
            repo=repo, digest=digest, mediatype=_to_str(value)
        )
    return out


def _fetch_repo_sets(
    client: _RedisClient, pairs: list[tuple[str, str]]
) -> dict[str, RepoBlobSet]:
    out: dict[str, RepoBlobSet] = {}
    if not pairs:
        return out
    pipe = client.pipeline(transaction=False)
    for key, _repo in pairs:
        pipe.smembers(key)
    results = cast(list[Any], pipe.execute())
    for (_key, repo), members in zip(pairs, results, strict=True):
        decoded = {_decode(m) for m in members or ()}
        out[repo] = RepoBlobSet(repo=repo, digests=decoded)
    return out


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return cast(str, value)


def _to_str(value: Any) -> str | None:
    if value is None:
        return None
    return _decode(value)


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return int(_decode(value))
