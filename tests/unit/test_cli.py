"""Phase 5 CLI tests for ``rcd scan / clean / inspect``.

These tests inject fakes for the redis client factory and the storage
filesystem so the CLI can be exercised without contacting a real
registry. The exit-code contract is the user-facing API and is the
primary thing we lock down here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import fakeredis
import pytest

from rcd.cli import main
from rcd.config import RegistryConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, registries: Iterable[dict[str, Any]]) -> Path:
    cfg = tmp_path / "rcd.toml"
    lines = ["schema_version = 1", ""]
    for r in registries:
        lines.append("[[registry]]")
        for key, value in r.items():
            if isinstance(value, str):
                lines.append(f'{key} = "{value}"')
            elif isinstance(value, bool):
                lines.append(f"{key} = {str(value).lower()}")
            else:
                lines.append(f"{key} = {value}")
        lines.append("")
    cfg.write_text("\n".join(lines))
    return cfg


def _make_blob_file(storage_path: Path, digest: str, payload: bytes) -> None:
    digest_hex = digest.removeprefix("sha256:")
    blob_dir = (
        storage_path
        / "docker"
        / "registry"
        / "v2"
        / "blobs"
        / "sha256"
        / digest_hex[:2]
        / digest_hex
    )
    blob_dir.mkdir(parents=True, exist_ok=True)
    (blob_dir / "data").write_bytes(payload)


def _seed_redis_global(client: Any, digest: str, *, size: int, mediatype: str) -> None:
    client.hset(f"blobs::{digest}", mapping={
        "digest": digest,
        "size": str(size),
        "mediatype": mediatype,
    })


def _seed_redis_repo(client: Any, repo: str, digest: str, mediatype: str = "") -> None:
    client.sadd(f"repository::{repo}::blobs", digest)
    if mediatype:
        client.hset(f"repository::{repo}::blobs::{digest}", "mediatype", mediatype)
    else:
        # Per-repo hash always exists in a healthy state, even with just the
        # mediatype bucket; classifier expects the key to exist for non-C3.
        client.hset(f"repository::{repo}::blobs::{digest}", "mediatype", "")


@pytest.fixture
def fake_factory() -> tuple[Any, dict[str, Any]]:
    """Return a (factory, clients-by-name) tuple sharing a fakeredis server.

    Each registry name maps to its own database via ``DB_BY_NAME``.
    """
    server = fakeredis.FakeServer()
    clients: dict[str, Any] = {}

    def _factory(cfg: RegistryConfig) -> Any:
        client = fakeredis.FakeStrictRedis(server=server, db=cfg.redis_db)
        clients[cfg.name] = client
        return client

    return _factory, clients


# ---------------------------------------------------------------------------
# version / unknown
# ---------------------------------------------------------------------------


def test_version_subcommand_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip()


def test_daemon_is_not_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    """``daemon`` is provided by the container entrypoint, not the CLI."""
    with pytest.raises(SystemExit) as excinfo:
        main(["daemon"])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def test_scan_returns_zero_when_only_ok(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fake_factory: tuple[Any, dict[str, Any]],
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    digest = "sha256:" + "11" * 32
    payload = b"hello world!"
    _make_blob_file(storage, digest, payload)

    factory, _clients = fake_factory
    cfg = _write_config(
        tmp_path,
        [{"name": "a", "redis_host": "fake", "storage_path": str(storage)}],
    )
    # Pre-seed redis to fully match fs (OK).
    seed_client = factory(RegistryConfig(name="a", redis_host="fake", storage_path=str(storage)))
    _seed_redis_global(seed_client, digest, size=len(payload), mediatype="application/octet-stream")
    _seed_redis_repo(seed_client, "library/alpine", digest, mediatype="application/octet-stream")

    with patch("rcd.cli.make_redis_factory", return_value=factory):
        rc = main(["--config", str(cfg), "scan"])

    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    # One scan_completed + one run_summary line.
    assert len(out) == 2
    events = [json.loads(line) for line in out]
    assert events[0]["event"] == "scan_completed"
    assert events[0]["registry"] == "a"
    assert events[1]["event"] == "run_summary"


def test_scan_returns_two_when_drift_detected(
    tmp_path: Path,
    fake_factory: tuple[Any, dict[str, Any]],
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    digest = "sha256:" + "22" * 32
    _make_blob_file(storage, digest, b"hi")

    factory, _ = fake_factory
    seed_client = factory(RegistryConfig(name="a", redis_host="fake", storage_path=str(storage)))
    # C5: redis says 1024 but fs is 2 bytes.
    _seed_redis_global(seed_client, digest, size=1024, mediatype="application/octet-stream")
    _seed_redis_repo(seed_client, "library/x", digest, mediatype="application/octet-stream")

    cfg = _write_config(
        tmp_path,
        [{"name": "a", "redis_host": "fake", "storage_path": str(storage)}],
    )

    with patch("rcd.cli.make_redis_factory", return_value=factory):
        rc = main(["--config", str(cfg), "scan"])

    # Drift but no --strict: exit 2.
    assert rc == 2


def test_scan_strict_returns_three_for_real_failures(
    tmp_path: Path,
    fake_factory: tuple[Any, dict[str, Any]],
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    digest = "sha256:" + "33" * 32
    _make_blob_file(storage, digest, b"hi")

    factory, _ = fake_factory
    seed_client = factory(RegistryConfig(name="a", redis_host="fake", storage_path=str(storage)))
    _seed_redis_global(seed_client, digest, size=1024, mediatype="application/octet-stream")
    _seed_redis_repo(seed_client, "library/x", digest, mediatype="application/octet-stream")

    cfg = _write_config(
        tmp_path,
        [{"name": "a", "redis_host": "fake", "storage_path": str(storage)}],
    )

    with patch("rcd.cli.make_redis_factory", return_value=factory):
        rc = main(["--config", str(cfg), "scan", "--strict"])

    # --strict + C4/C5 present: exit 3.
    assert rc == 3


def test_scan_strict_zero_when_only_internal_garbage(
    tmp_path: Path,
    fake_factory: tuple[Any, dict[str, Any]],
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    digest = "sha256:" + "44" * 32

    factory, _ = fake_factory
    seed_client = factory(RegistryConfig(name="a", redis_host="fake", storage_path=str(storage)))
    # C1 only: global hash without any repo refs.
    _seed_redis_global(seed_client, digest, size=10, mediatype="application/octet-stream")

    cfg = _write_config(
        tmp_path,
        [{"name": "a", "redis_host": "fake", "storage_path": str(storage)}],
    )

    with patch("rcd.cli.make_redis_factory", return_value=factory):
        rc = main(["--config", str(cfg), "scan", "--strict"])

    # --strict only escalates real failures (C4/C5), C1 still drift -> 2.
    assert rc == 2


def test_scan_returns_one_when_config_missing(tmp_path: Path) -> None:
    rc = main(["--config", str(tmp_path / "nope.toml"), "scan"])
    assert rc == 1


# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------


def test_clean_dry_run_does_not_touch_redis(
    tmp_path: Path,
    fake_factory: tuple[Any, dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    digest = "sha256:" + "55" * 32
    _make_blob_file(storage, digest, b"hi")

    factory, _ = fake_factory
    seed_client = factory(RegistryConfig(name="a", redis_host="fake", storage_path=str(storage)))
    _seed_redis_global(seed_client, digest, size=1024, mediatype="application/octet-stream")
    _seed_redis_repo(seed_client, "library/x", digest, mediatype="application/octet-stream")

    cfg = _write_config(
        tmp_path,
        [{"name": "a", "redis_host": "fake", "storage_path": str(storage)}],
    )

    with patch("rcd.cli.make_redis_factory", return_value=factory):
        rc = main(["--config", str(cfg), "clean"])

    # Dry-run + drift: exit 2.
    assert rc == 2
    # Redis still has the entries.
    assert seed_client.exists(f"blobs::{digest}")
    out = capsys.readouterr().out.strip().splitlines()
    events = [json.loads(line) for line in out]
    assert any(e["event"] == "clean_completed" for e in events)
    clean_event = next(e for e in events if e["event"] == "clean_completed")
    assert clean_event["applied"] == 0


def test_clean_apply_removes_inconsistent_entries(
    tmp_path: Path,
    fake_factory: tuple[Any, dict[str, Any]],
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    digest = "sha256:" + "66" * 32
    _make_blob_file(storage, digest, b"hi")

    factory, _ = fake_factory
    seed_client = factory(RegistryConfig(name="a", redis_host="fake", storage_path=str(storage)))
    _seed_redis_global(seed_client, digest, size=1024, mediatype="application/octet-stream")
    _seed_redis_repo(seed_client, "library/x", digest, mediatype="application/octet-stream")

    cfg = _write_config(
        tmp_path,
        [{"name": "a", "redis_host": "fake", "storage_path": str(storage)}],
    )

    with patch("rcd.cli.make_redis_factory", return_value=factory):
        rc = main(["--config", str(cfg), "clean", "--apply"])

    # Apply path: returns 0 even when drift was found, since cleanup ran.
    assert rc == 0
    # Repository set should no longer contain the digest.
    assert not seed_client.sismember("repository::library/x::blobs", digest)


def test_clean_apply_strict_zero_when_no_failures(
    tmp_path: Path,
    fake_factory: tuple[Any, dict[str, Any]],
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()

    factory, _ = fake_factory
    factory(RegistryConfig(name="a", redis_host="fake", storage_path=str(storage)))

    cfg = _write_config(
        tmp_path,
        [{"name": "a", "redis_host": "fake", "storage_path": str(storage)}],
    )

    with patch("rcd.cli.make_redis_factory", return_value=factory):
        rc = main(["--config", str(cfg), "clean", "--apply", "--strict"])

    assert rc == 0


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def test_inspect_emits_one_line_per_issue(
    tmp_path: Path,
    fake_factory: tuple[Any, dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    digest = "sha256:" + "77" * 32
    _make_blob_file(storage, digest, b"hi")

    factory, _ = fake_factory
    seed_client = factory(RegistryConfig(name="a", redis_host="fake", storage_path=str(storage)))
    _seed_redis_global(seed_client, digest, size=1024, mediatype="application/octet-stream")
    _seed_redis_repo(seed_client, "library/x", digest, mediatype="application/octet-stream")

    cfg = _write_config(
        tmp_path,
        [{"name": "a", "redis_host": "fake", "storage_path": str(storage)}],
    )

    with patch("rcd.cli.make_redis_factory", return_value=factory):
        rc = main(
            [
                "--config",
                str(cfg),
                "inspect",
                "--registry",
                "a",
                "--category",
                "c5",
            ]
        )

    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    events = [json.loads(line) for line in out if line.strip()]
    inspect_events = [e for e in events if e["event"] == "inspect"]
    assert len(inspect_events) == 1
    assert inspect_events[0]["category"] == "c5"
    assert inspect_events[0]["digest"] == digest
    # No --with-refs: no refs key.
    assert "refs" not in inspect_events[0]


def test_inspect_with_refs_lists_referencing_repos(
    tmp_path: Path,
    fake_factory: tuple[Any, dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    digest = "sha256:" + "88" * 32

    factory, _ = fake_factory
    seed_client = factory(RegistryConfig(name="a", redis_host="fake", storage_path=str(storage)))
    # C1: only global hash, no repo refs.
    _seed_redis_global(seed_client, digest, size=10, mediatype="application/octet-stream")

    cfg = _write_config(
        tmp_path,
        [{"name": "a", "redis_host": "fake", "storage_path": str(storage)}],
    )

    with patch("rcd.cli.make_redis_factory", return_value=factory):
        rc = main(
            [
                "--config",
                str(cfg),
                "inspect",
                "--registry",
                "a",
                "--category",
                "c1",
                "--with-refs",
            ]
        )

    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    inspects = [json.loads(line) for line in out if json.loads(line)["event"] == "inspect"]
    assert len(inspects) == 1
    assert inspects[0]["refs"] == []
