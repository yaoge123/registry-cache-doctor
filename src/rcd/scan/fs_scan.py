"""Filesystem-side blob scanner.

Walks the on-disk layout used by `distribution/distribution`:

    <root>/docker/registry/v2/blobs/sha256/<2-hex-prefix>/<full-digest>/data

For each well-formed entry, captures the size of the `data` regular
file. Anything that doesn't match the layout is silently skipped - the
parent tree is shared with upload temp dirs and we don't want partial
uploads to leak into the snapshot. Permission errors are propagated:
half-truths are worse than failure for a diagnostic tool.

The returned snapshot is keyed by `sha256:<digest>` to match the way
Redis represents digests, so the classifier can join the two views by
key directly.
"""

from __future__ import annotations

import os
import string
from pathlib import Path

from rcd.models import FsBlob, FsSnapshot

_HEX = set(string.hexdigits.lower())
_DIGEST_LEN = 64
_BLOBS_SUBPATH = ("docker", "registry", "v2", "blobs", "sha256")


def _is_lower_hex(s: str, length: int) -> bool:
    return len(s) == length and all(c in _HEX for c in s)


def scan_fs(root: str | os.PathLike[str]) -> FsSnapshot:
    """Scan a registry storage root for present blob `data` files.

    Missing tree returns an empty snapshot (not an error): freshly
    deployed registries have no blobs yet, and the classifier still
    needs a snapshot to compare against the Redis side.
    """
    blobs_root = Path(root, *_BLOBS_SUBPATH)
    blobs: dict[str, FsBlob] = {}
    if not blobs_root.is_dir():
        return FsSnapshot(blobs=blobs)

    # We use os.scandir for the prefix and digest layers because it
    # returns DirEntry objects that carry stat info on Linux without an
    # extra syscall, and matches the size of the production trees
    # (~50K-700K dirs per registry).
    with os.scandir(blobs_root) as prefix_iter:
        for prefix_entry in prefix_iter:
            if not prefix_entry.is_dir(follow_symlinks=False):
                continue
            prefix = prefix_entry.name
            if not _is_lower_hex(prefix, 2):
                continue
            with os.scandir(prefix_entry.path) as digest_iter:
                for digest_entry in digest_iter:
                    if not digest_entry.is_dir(follow_symlinks=False):
                        continue
                    digest_hex = digest_entry.name
                    if not _is_lower_hex(digest_hex, _DIGEST_LEN):
                        continue
                    if not digest_hex.startswith(prefix):
                        # Misplaced: the layout convention is broken.
                        continue
                    data_path = Path(digest_entry.path) / "data"
                    try:
                        st = data_path.stat()
                    except FileNotFoundError:
                        # No data file => not really a blob on disk.
                        continue
                    if not _is_regular_file(st.st_mode):
                        continue
                    digest = f"sha256:{digest_hex}"
                    blobs[digest] = FsBlob(digest=digest, size=st.st_size)

    return FsSnapshot(blobs=blobs)


def _is_regular_file(mode: int) -> bool:
    # 0o170000 mask, 0o100000 == S_IFREG, avoids importing stat module.
    return (mode & 0o170000) == 0o100000
