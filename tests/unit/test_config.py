"""Tests for ``rcd.config.load_config`` (TOML loader)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rcd.config import (
    AppConfig,
    CleanOptions,
    ConfigError,
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

[redis]
socket_timeout = 7
socket_connect_timeout = 3
scan_count = 1500
pipeline_batch = 250

[scan]
parallel = 4
strict = true

[clean]
strict = true
retry = 2
clear_internal_garbage = false

[[registry]]
name = "alpha"
redis_host = "alpha-redis"
redis_port = 6380
redis_db = 1
redis_password = "shh"
storage_path = "/srv/alpha"
enabled = false

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
    assert cfg.scan == ScanOptions(parallel=4, strict=True)
    assert cfg.clean == CleanOptions(strict=True, retry=2, clear_internal_garbage=False)
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


def test_top_level_network_now_rejected(tmp_path: Path) -> None:
    # ``network`` was a docker-compose hint that rcd never read; it is now an
    # error so users do not assume it has any effect on rcd itself.
    body = _MINIMAL + '\nnetwork = "ignored-by-rcd"\n'
    with pytest.raises(ConfigError, match="network"):
        load_config(_write(tmp_path, body))


def test_registry_level_network_now_rejected(tmp_path: Path) -> None:
    body = _MINIMAL.rstrip() + '\nnetwork = "alpha-net"\n'
    with pytest.raises(ConfigError, match="network"):
        load_config(_write(tmp_path, body))


def test_redis_db_no_longer_accepted(tmp_path: Path) -> None:
    # Redis db is selected per-registry via ``[[registry]].redis_db``. A
    # global ``[redis].db`` was always silently dropped; reject it now.
    body = _MINIMAL.replace(
        "schema_version = 1",
        "schema_version = 1\n[redis]\ndb = 5",
    )
    with pytest.raises(ConfigError, match="db"):
        load_config(_write(tmp_path, body))


def test_load_accepts_string_path(tmp_path: Path) -> None:
    path = _write(tmp_path, _MINIMAL)
    cfg = load_config(str(path))
    assert cfg.registries[0].name == "alpha"


def test_scan_strict_default_is_false(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _MINIMAL))
    assert cfg.scan.strict is False


def test_scan_strict_can_be_true(tmp_path: Path) -> None:
    body = _MINIMAL.replace(
        "schema_version = 1",
        "schema_version = 1\n[scan]\nstrict = true",
    )
    cfg = load_config(_write(tmp_path, body))
    assert cfg.scan.strict is True


def test_clean_strict_default_is_false(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _MINIMAL))
    assert cfg.clean.strict is False


def test_clean_strict_can_be_true(tmp_path: Path) -> None:
    body = _MINIMAL.replace(
        "schema_version = 1",
        "schema_version = 1\n[clean]\nstrict = true",
    )
    cfg = load_config(_write(tmp_path, body))
    assert cfg.clean.strict is True


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


def test_unknown_scan_key_raises(tmp_path: Path) -> None:
    # ``verify_digest`` was a placeholder for a feature that never shipped; it
    # is now rejected so users do not silently expect it to work.
    body = _MINIMAL.replace(
        "schema_version = 1",
        "schema_version = 1\n[scan]\nverify_digest = true",
    )
    with pytest.raises(ConfigError, match="verify_digest"):
        load_config(_write(tmp_path, body))


def test_daemon_section_now_rejected(tmp_path: Path) -> None:
    # The whole [daemon] section moved to environment variables
    # (RCD_SCHEDULE, RCD_DAEMON_AUTO_CLEAN, RCD_DAEMON_STRICT). Reject it
    # in TOML so the user is not surprised when an in-file value silently
    # has no effect.
    body = _MINIMAL.replace(
        "schema_version = 1",
        "schema_version = 1\n[daemon]\nstrict = true",
    )
    with pytest.raises(ConfigError, match="daemon"):
        load_config(_write(tmp_path, body))


def test_output_section_now_rejected(tmp_path: Path) -> None:
    # The [output] section was never read by the CLI. Reject it so the
    # config does not advertise behaviour that does not exist.
    body = _MINIMAL.replace(
        "schema_version = 1",
        "schema_version = 1\n[output]\nquiet = true",
    )
    with pytest.raises(ConfigError, match="output"):
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


def test_find_config_xdg_when_no_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When no explicit, no env, and no cwd-local file, fall back to
    # $XDG_CONFIG_HOME/registry-cache-doctor/config.toml.
    empty_cwd = tmp_path / "cwd"
    empty_cwd.mkdir()
    xdg = tmp_path / "xdg"
    rcd_dir = xdg / "registry-cache-doctor"
    rcd_dir.mkdir(parents=True)
    cfg = _write(rcd_dir, _MINIMAL, name="config.toml")

    monkeypatch.delenv("RCD_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.chdir(empty_cwd)

    assert find_config(explicit=None) == cfg


def test_find_config_user_home_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When XDG_CONFIG_HOME is unset, fall back to ~/.config/...
    empty_cwd = tmp_path / "cwd"
    empty_cwd.mkdir()
    home = tmp_path / "home"
    rcd_dir = home / ".config" / "registry-cache-doctor"
    rcd_dir.mkdir(parents=True)
    cfg = _write(rcd_dir, _MINIMAL, name="config.toml")

    monkeypatch.delenv("RCD_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(empty_cwd)

    assert find_config(explicit=None) == cfg


def test_find_config_returns_none_when_nothing_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RCD_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "no-such-home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    assert find_config(explicit=None) is None
