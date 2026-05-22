"""Tests for filesystem blob scanning."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _make_blob(root: Path, digest_hex: str, payload: bytes = b"hi") -> Path:
    """Create a distribution-shaped blob path under `root`.

    Layout: <root>/docker/registry/v2/blobs/sha256/<prefix>/<digest>/data
    """
    prefix = digest_hex[:2]
    blob_dir = root / "docker" / "registry" / "v2" / "blobs" / "sha256" / prefix / digest_hex
    blob_dir.mkdir(parents=True, exist_ok=True)
    data = blob_dir / "data"
    data.write_bytes(payload)
    return data


def test_scan_fs_returns_empty_for_missing_blob_root(tmp_path: Path) -> None:
    from rcd.scan.fs_scan import scan_fs

    snap = scan_fs(tmp_path)
    assert snap.blobs == {}


def test_scan_fs_finds_one_blob(tmp_path: Path) -> None:
    from rcd.scan.fs_scan import scan_fs

    digest_hex = "a" * 64
    _make_blob(tmp_path, digest_hex, payload=b"x" * 17)

    snap = scan_fs(tmp_path)

    assert list(snap.blobs.keys()) == [f"sha256:{digest_hex}"]
    assert snap.blobs[f"sha256:{digest_hex}"].size == 17


def test_scan_fs_indexes_many_prefixes(tmp_path: Path) -> None:
    from rcd.scan.fs_scan import scan_fs

    digests = [
        ("a" * 64, b"a"),
        ("b" * 64, b"bb"),
        ("c" * 64, b"ccc"),
    ]
    for d, p in digests:
        _make_blob(tmp_path, d, p)

    snap = scan_fs(tmp_path)

    assert len(snap.blobs) == 3
    for d, p in digests:
        assert snap.blobs[f"sha256:{d}"].size == len(p)


def test_scan_fs_records_size_zero_files(tmp_path: Path) -> None:
    """Zero-byte data files are an actual production failure mode (C5
    extreme), and the scanner must report them so the classifier can
    flag them."""
    from rcd.scan.fs_scan import scan_fs

    digest_hex = "d" * 64
    _make_blob(tmp_path, digest_hex, payload=b"")

    snap = scan_fs(tmp_path)
    assert snap.blobs[f"sha256:{digest_hex}"].size == 0


def test_scan_fs_skips_blob_dir_without_data_file(tmp_path: Path) -> None:
    """A dangling directory with no `data` file must not appear in the
    snapshot - distribution treats that as 'blob not on disk'."""
    from rcd.scan.fs_scan import scan_fs

    digest_hex = "e" * 64
    blob_dir = (
        tmp_path / "docker" / "registry" / "v2" / "blobs" / "sha256" / digest_hex[:2] / digest_hex
    )
    blob_dir.mkdir(parents=True)
    # Note: no `data` file written.

    snap = scan_fs(tmp_path)
    assert snap.blobs == {}


def test_scan_fs_skips_invalid_digest_dir_names(tmp_path: Path) -> None:
    """Anything not matching the 64-hex digest layout must be ignored.
    distribution writes its uploads/temp data under the same parent so
    the scanner must defend against accidental matches."""
    from rcd.scan.fs_scan import scan_fs

    bogus_root = tmp_path / "docker" / "registry" / "v2" / "blobs" / "sha256" / "ab"
    (bogus_root / "not-hex").mkdir(parents=True)
    (bogus_root / "not-hex" / "data").write_bytes(b"")
    short = bogus_root / "ab123"
    short.mkdir(parents=True)
    (short / "data").write_bytes(b"")

    snap = scan_fs(tmp_path)
    assert snap.blobs == {}


def test_scan_fs_skips_mismatched_prefix(tmp_path: Path) -> None:
    """The first two hex chars of the digest must match the parent
    prefix dir; if they don't, this is corrupted layout and we ignore."""
    from rcd.scan.fs_scan import scan_fs

    digest_hex = "f" * 64
    wrong_prefix = "00"
    blob_dir = (
        tmp_path / "docker" / "registry" / "v2" / "blobs" / "sha256" / wrong_prefix / digest_hex
    )
    blob_dir.mkdir(parents=True)
    (blob_dir / "data").write_bytes(b"oops")

    snap = scan_fs(tmp_path)
    assert snap.blobs == {}


def test_scan_fs_passes_non_path_arguments_as_path(tmp_path: Path) -> None:
    """Accept str roots too, just to keep the call site ergonomic."""
    from rcd.scan.fs_scan import scan_fs

    digest_hex = "9" * 64
    _make_blob(tmp_path, digest_hex)
    snap = scan_fs(str(tmp_path))
    assert f"sha256:{digest_hex}" in snap.blobs


def test_scan_fs_uses_data_file_size_not_directory_size(tmp_path: Path) -> None:
    """Size must be `stat()` of the `data` regular file."""
    from rcd.scan.fs_scan import scan_fs

    digest_hex = "1" * 64
    _make_blob(tmp_path, digest_hex, payload=b"\x00" * 4096)

    snap = scan_fs(tmp_path)
    blob_path = (
        tmp_path
        / "docker"
        / "registry"
        / "v2"
        / "blobs"
        / "sha256"
        / digest_hex[:2]
        / digest_hex
        / "data"
    )
    assert snap.blobs[f"sha256:{digest_hex}"].size == os.stat(blob_path).st_size


def test_scan_fs_unreadable_prefix_raises(tmp_path: Path) -> None:
    """If a prefix directory exists but is unreadable, surface the error
    rather than silently skipping; in production we'd rather a hard
    fail than a half-truth."""
    pytest.importorskip("os")  # always present, but documents the platform expectation

    if os.geteuid() == 0:
        pytest.skip("root bypasses permission bits")

    from rcd.scan.fs_scan import scan_fs

    prefix = tmp_path / "docker" / "registry" / "v2" / "blobs" / "sha256" / "ab"
    prefix.mkdir(parents=True)
    digest_hex = "ab" + "0" * 62
    blob_dir = prefix / digest_hex
    blob_dir.mkdir()
    (blob_dir / "data").write_bytes(b"x")
    os.chmod(prefix, 0o000)
    try:
        with pytest.raises(PermissionError):
            scan_fs(tmp_path)
    finally:
        os.chmod(prefix, 0o755)
