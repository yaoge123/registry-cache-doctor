"""Tests for ``rcd.config.load_config`` (TOML loader)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rcd.config import (
    AppConfig,
    CleanOptions,
    ConfigError,
    DaemonOptions,
    OutputOptions,
    RedisOptions,
    RegistryConfig,
    ScanOptions,
    find_config,
    load_config,
)

# ---------- helpers --------------------------------------------------------


_MINIMAL = """\
schema_version = 1

[[registry]]
name = "alpha"
redis_host = "alpha-redis"
storage_path = "/var/lib/registry"
"""


_FULL = """\
schema_version = 1
network = "rcd-net"

[redis]
db = 2
socket_timeout = 7
socket_connect_timeout = 3
scan_count = 1500
pipeline_batch = 250

[scan]
parallel = 4
verify_digest = true

[clean]
strict = true
retry = 2
clear_internal_garbage = false

[daemon]
strict = true

[output]
quiet = true
no_color = true
include_digest_list = true

[[registry]]
name = "alpha"
redis_host = "alpha-redis"
redis_port = 6380
redis_db = 1
redis_password = "shh"
storage_path = "/srv/alpha"
enabled = false
network = "alpha-net"

[[registry]]
name = "beta"
redis_host = "beta-redis"
storage_path = "/srv/beta"
"""


def _write(tmp: Path, body: str, *, name: str = "config.toml") -> Path:
    path = tmp / name
    path.write_text(body, encoding="utf-8")
    return path


# ---------- load_config: happy paths ---------------------------------------


def test_load_minimal_config_yields_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _MINIMAL))

    assert isinstance(cfg, AppConfig)
    assert cfg.schema_version == 1
    assert cfg.redis == RedisOptions()
    assert cfg.scan == ScanOptions()
    assert cfg.clean == CleanOptions()
    assert cfg.daemon == DaemonOptions()
    assert cfg.output == OutputOptions()
    assert cfg.registries == (
        RegistryConfig(name="alpha", redis_host="alpha-redis", storage_path="/var/lib/registry"),
    )


def test_load_full_config_populates_every_field(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _FULL))

    assert cfg.redis == RedisOptions(
        socket_timeout=7.0,
        socket_connect_timeout=3.0,
        scan_count=1500,
        pipeline_batch=250,
    )
    assert cfg.scan == ScanOptions(parallel=4, verify_digest=True)
    assert cfg.clean == CleanOptions(strict=True, retry=2, clear_internal_garbage=False)
    assert cfg.daemon == DaemonOptions(strict=True)
    assert cfg.output == OutputOptions(
        quiet=True, no_color=True, include_digest_list=True
    )
    assert cfg.registries == (
        RegistryConfig(
            name="alpha",
            redis_host="alpha-redis",
            storage_path="/srv/alpha",
            redis_port=6380,
            redis_db=1,
            redis_password="shh",
            enabled=False,
        ),
        RegistryConfig(name="beta", redis_host="beta-redis", storage_path="/srv/beta"),
    )


def test_top_level_network_is_ignored(tmp_path: Path) -> None:
    body = _MINIMAL + '\nnetwork = "ignored-by-rcd"\n'
    cfg = load_config(_write(tmp_path, body))
    assert cfg.registries[0].name == "alpha"


def test_registry_level_network_is_ignored(tmp_path: Path) -> None:
    body = _MINIMAL.rstrip() + '\nnetwork = "alpha-net"\n'
    cfg = load_config(_write(tmp_path, body))
    # No exception; the network field is silently dropped.
    assert cfg.registries[0].name == "alpha"


def test_load_accepts_string_path(tmp_path: Path) -> None:
    path = _write(tmp_path, _MINIMAL)
    cfg = load_config(str(path))
    assert cfg.registries[0].name == "alpha"


# ---------- load_config: validation errors ---------------------------------


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "schema_version = 1\nthis is = invalid = toml")
    with pytest.raises(ConfigError, match="parse"):
        load_config(path)


def test_missing_schema_version_raises(tmp_path: Path) -> None:
    body = _MINIMAL.replace("schema_version = 1\n", "")
    with pytest.raises(ConfigError, match="schema_version"):
        load_config(_write(tmp_path, body))


def test_unsupported_schema_version_raises(tmp_path: Path) -> None:
    body = _MINIMAL.replace("schema_version = 1", "schema_version = 99")
    with pytest.raises(ConfigError, match="schema_version"):
        load_config(_write(tmp_path, body))


def test_no_registries_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="registry"):
        load_config(_write(tmp_path, "schema_version = 1\n"))


def test_duplicate_registry_name_raises(tmp_path: Path) -> None:
    body = (
        "schema_version = 1\n"
        '[[registry]]\nname = "alpha"\nredis_host = "h1"\nstorage_path = "/p1"\n'
        '[[registry]]\nname = "alpha"\nredis_host = "h2"\nstorage_path = "/p2"\n'
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(_write(tmp_path, body))


@pytest.mark.parametrize("missing", ["name", "redis_host", "storage_path"])
def test_registry_missing_required_field_raises(tmp_path: Path, missing: str) -> None:
    body = _MINIMAL
    body = "\n".join(line for line in body.splitlines() if not line.startswith(missing + " "))
    with pytest.raises(ConfigError, match=missing):
        load_config(_write(tmp_path, body))


def test_unknown_top_level_key_raises(tmp_path: Path) -> None:
    body = _MINIMAL + '\nbogus = "x"\n'
    with pytest.raises(ConfigError, match="bogus"):
        load_config(_write(tmp_path, body))


def test_unknown_registry_key_raises(tmp_path: Path) -> None:
    body = _MINIMAL.rstrip() + '\nbogus = "x"\n'
    with pytest.raises(ConfigError, match="bogus"):
        load_config(_write(tmp_path, body))


def test_unknown_redis_key_raises(tmp_path: Path) -> None:
    body = _MINIMAL.replace(
        "schema_version = 1",
        'schema_version = 1\n[redis]\nbogus = 1',
    )
    with pytest.raises(ConfigError, match="bogus"):
        load_config(_write(tmp_path, body))


def test_daemon_schedule_no_longer_accepted(tmp_path: Path) -> None:
    # ``schedule`` is now controlled by RCD_SCHEDULE; reject it in TOML so the
    # user is not surprised when an in-file value silently has no effect.
    body = _MINIMAL.replace(
        "schema_version = 1",
        'schema_version = 1\n[daemon]\nschedule = "0 4 * * *"',
    )
    with pytest.raises(ConfigError, match="schedule"):
        load_config(_write(tmp_path, body))


def test_daemon_auto_clean_no_longer_accepted(tmp_path: Path) -> None:
    # ``auto_clean`` is now controlled by RCD_DAEMON_AUTO_CLEAN.
    body = _MINIMAL.replace(
        "schema_version = 1",
        "schema_version = 1\n[daemon]\nauto_clean = true",
    )
    with pytest.raises(ConfigError, match="auto_clean"):
        load_config(_write(tmp_path, body))


def test_negative_scan_parallel_raises(tmp_path: Path) -> None:
    body = _MINIMAL.replace(
        "schema_version = 1",
        "schema_version = 1\n[scan]\nparallel = -1",
    )
    with pytest.raises(ConfigError, match="parallel"):
        load_config(_write(tmp_path, body))


def test_zero_scan_count_raises(tmp_path: Path) -> None:
    body = _MINIMAL.replace(
        "schema_version = 1",
        "schema_version = 1\n[redis]\nscan_count = 0",
    )
    with pytest.raises(ConfigError, match="scan_count"):
        load_config(_write(tmp_path, body))


def test_negative_clean_retry_raises(tmp_path: Path) -> None:
    body = _MINIMAL.replace(
        "schema_version = 1",
        "schema_version = 1\n[clean]\nretry = -1",
    )
    with pytest.raises(ConfigError, match="retry"):
        load_config(_write(tmp_path, body))


# ---------- find_config ----------------------------------------------------


def test_find_config_explicit_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = _write(tmp_path, _MINIMAL, name="explicit.toml")
    decoy = _write(tmp_path, _MINIMAL, name="env.toml")
    monkeypatch.setenv("RCD_CONFIG", str(decoy))
    monkeypatch.chdir(tmp_path)

    assert find_config(explicit=str(explicit)) == explicit


def test_find_config_env_when_no_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    via_env = _write(tmp_path, _MINIMAL, name="env.toml")
    monkeypatch.setenv("RCD_CONFIG", str(via_env))
    monkeypatch.chdir(tmp_path)

    assert find_config(explicit=None) == via_env


def test_find_config_cwd_when_no_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd_cfg = _write(tmp_path, _MINIMAL, name="registry-cache-doctor.toml")
    monkeypatch.delenv("RCD_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    assert find_config(explicit=None) == cwd_cfg


def test_find_config_returns_none_when_nothing_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RCD_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "no-such-home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert find_config(explicit=None) is None
