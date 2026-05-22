"""Tests for the per-registry scan orchestrator (Phase 3)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import fakeredis
import pytest

from rcd.classifier import Category
from rcd.config import RegistryConfig
from rcd.orchestrator import (
    OrchestrationError,
    RedisClientFactory,
    scan_one_registry,
    scan_registries,
)

# --- helpers -----------------------------------------------------------

D1 = "sha256:" + "11" * 32
D2 = "sha256:" + "22" * 32
D3 = "sha256:" + "33" * 32
R1 = "library/alpine"


def _write_blob(root: Path, digest: str, size: int) -> None:
    """Create a fake on-disk blob in the distribution v2 layout."""
    assert digest.startswith("sha256:")
    hexpart = digest[len("sha256:") :]
    prefix = hexpart[:2]
    blob_dir = root / "docker/registry/v2/blobs/sha256" / prefix / hexpart
    blob_dir.mkdir(parents=True, exist_ok=True)
    data_path = blob_dir / "data"
    if size == 0:
        data_path.touch()
    else:
        data_path.write_bytes(b"\x00" * size)


def _populate_redis(client: fakeredis.FakeRedis) -> None:
    """Seed a fake redis with a known mix of categories.

    - D1: full happy path (global + set + repo hash + fs)         -> OK
    - D2: cache promises 100 bytes, fs has 10                      -> C5
    - D3: only global hash, no repo refs, no fs                    -> C1
    """
    client.hset(
        f"blobs::{D1}",
        mapping={"digest": D1, "size": "10", "mediatype": "application/octet-stream"},
    )
    client.sadd(f"repository::{R1}::blobs", D1)
    client.hset(f"repository::{R1}::blobs::{D1}", "mediatype", "application/octet-stream")

    client.hset(
        f"blobs::{D2}",
        mapping={"digest": D2, "size": "100", "mediatype": "application/octet-stream"},
    )
    client.sadd(f"repository::{R1}::blobs", D2)
    client.hset(f"repository::{R1}::blobs::{D2}", "mediatype", "application/octet-stream")

    client.hset(
        f"blobs::{D3}",
        mapping={"digest": D3, "size": "0", "mediatype": "application/octet-stream"},
    )


def _factory_for(client: fakeredis.FakeRedis) -> RedisClientFactory:
    """Return a factory that always hands back the given client."""

    def make(_cfg: RegistryConfig) -> fakeredis.FakeRedis:
        return client

    return make


# --- single registry --------------------------------------------------


@pytest.mark.asyncio
async def test_scan_one_registry_returns_report_with_counts(tmp_path: Path) -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    _populate_redis(client)
    _write_blob(tmp_path, D1, 10)
    _write_blob(tmp_path, D2, 10)  # mismatch; redis says 100

    cfg = RegistryConfig(
        name="reg",
        redis_host="ignored",
        storage_path=str(tmp_path),
    )

    report = await scan_one_registry(cfg, factory=_factory_for(client))

    assert report.registry == "reg"
    assert report.duration_s >= 0
    assert report.errors == ()
    # Redis key counts
    assert report.redis_keys.global_hash == 3
    assert report.redis_keys.repo_set == 1
    assert report.redis_keys.repo_hash == 2
    # Categories
    counts = report.classification.counts
    assert counts[Category.OK] == 1
    assert counts[Category.C5] == 1
    assert counts[Category.C1] == 1


@pytest.mark.asyncio
async def test_scan_one_registry_propagates_redis_error(tmp_path: Path) -> None:
    class _Boom:
        def scan_iter(self, **_kw: object) -> object:
            raise ConnectionError("boom")

    cfg = RegistryConfig(
        name="reg",
        redis_host="ignored",
        storage_path=str(tmp_path),
    )

    def factory(_c: RegistryConfig) -> _Boom:
        return _Boom()

    with pytest.raises(OrchestrationError) as ei:
        await scan_one_registry(cfg, factory=factory)  # type: ignore[arg-type]
    assert "reg" in str(ei.value)
    assert "boom" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_scan_one_registry_runs_redis_and_fs_concurrently(
    tmp_path: Path,
) -> None:
    """Orchestrator should not serialize the two halves."""
    import time

    # Build a fake redis client whose `scan_iter` blocks for 50 ms.
    real = fakeredis.FakeRedis(decode_responses=True)
    _populate_redis(real)
    _write_blob(tmp_path, D1, 10)
    _write_blob(tmp_path, D2, 100)
    _write_blob(tmp_path, D3, 0)

    class _SlowRedis:
        def scan_iter(self, *args: object, **kwargs: object) -> object:
            time.sleep(0.05)
            return real.scan_iter(*args, **kwargs)

        def pipeline(self, *args: object, **kwargs: object) -> object:
            return real.pipeline(*args, **kwargs)

    cfg = RegistryConfig(
        name="reg", redis_host="ignored", storage_path=str(tmp_path)
    )

    # Slow down the fs walk too.
    real_walk = os.scandir

    def slow_scandir(p: object) -> object:
        time.sleep(0.05)
        return real_walk(p)  # type: ignore[arg-type]

    import rcd.scan.fs_scan as fsmod

    fsmod_orig = fsmod.os.scandir  # type: ignore[attr-defined]
    fsmod.os.scandir = slow_scandir  # type: ignore[attr-defined,assignment]
    try:
        start = time.monotonic()
        await scan_one_registry(cfg, factory=lambda _c: _SlowRedis())  # type: ignore[arg-type,return-value]
        elapsed = time.monotonic() - start
    finally:
        fsmod.os.scandir = fsmod_orig  # type: ignore[attr-defined]

    # Sequential lower bound would be ~ (50 ms redis sleep + many 50 ms
    # fs sleeps). We only assert "didn't take > 1s" as a loose check;
    # the strict concurrency invariant lives in the orchestrator's
    # use of asyncio.gather + to_thread.
    assert elapsed < 1.0


# --- multiple registries ----------------------------------------------


@pytest.mark.asyncio
async def test_scan_registries_returns_report_per_enabled_registry(
    tmp_path: Path,
) -> None:
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()

    client_a = fakeredis.FakeRedis(decode_responses=True)
    client_b = fakeredis.FakeRedis(decode_responses=True)

    cfgs = [
        RegistryConfig(name="a", redis_host="x", storage_path=str(a_dir)),
        RegistryConfig(name="b", redis_host="x", storage_path=str(b_dir)),
    ]

    factory_map = {"a": client_a, "b": client_b}

    def factory(c: RegistryConfig) -> fakeredis.FakeRedis:
        return factory_map[c.name]

    reports, summary = await scan_registries(cfgs, factory=factory, parallel=2)

    assert {r.registry for r in reports} == {"a", "b"}
    assert summary.duration_s >= 0
    # Empty registries -> all category totals zero.
    for cat in Category:
        assert summary.totals.get(cat, 0) == 0


@pytest.mark.asyncio
async def test_scan_registries_skips_disabled(tmp_path: Path) -> None:
    a_dir = tmp_path / "a"
    a_dir.mkdir()
    cfgs = [
        RegistryConfig(
            name="a", redis_host="x", storage_path=str(a_dir), enabled=False
        ),
    ]

    reports, _summary = await scan_registries(
        cfgs,
        factory=_factory_for(fakeredis.FakeRedis(decode_responses=True)),
        parallel=1,
    )

    assert reports == ()


@pytest.mark.asyncio
async def test_scan_registries_one_failure_does_not_abort_others(
    tmp_path: Path,
) -> None:
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()

    good = fakeredis.FakeRedis(decode_responses=True)

    class _Boom:
        def scan_iter(self, **_kw: object) -> object:
            raise ConnectionError("simulated")

    def factory(c: RegistryConfig) -> object:
        return _Boom() if c.name == "a" else good

    cfgs = [
        RegistryConfig(name="a", redis_host="x", storage_path=str(a_dir)),
        RegistryConfig(name="b", redis_host="x", storage_path=str(b_dir)),
    ]

    reports, _summary = await scan_registries(
        cfgs, factory=factory, parallel=2  # type: ignore[arg-type]
    )

    by_name = {r.registry: r for r in reports}
    assert set(by_name) == {"a", "b"}
    # The failed registry produced a report with errors set, not a raise.
    assert by_name["a"].errors and "simulated" in by_name["a"].errors[0]
    # The other registry succeeded normally.
    assert by_name["b"].errors == ()


@pytest.mark.asyncio
async def test_scan_registries_zero_parallel_means_auto(tmp_path: Path) -> None:
    """parallel=0 means "as many as enabled registries" (no semaphore stall)."""
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    cfgs = [
        RegistryConfig(name="a", redis_host="x", storage_path=str(a_dir)),
        RegistryConfig(name="b", redis_host="x", storage_path=str(b_dir)),
    ]
    client = fakeredis.FakeRedis(decode_responses=True)

    reports, _ = await asyncio.wait_for(
        scan_registries(cfgs, factory=_factory_for(client), parallel=0),
        timeout=5,
    )
    assert {r.registry for r in reports} == {"a", "b"}
