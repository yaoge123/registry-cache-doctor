"""Configuration data structures and TOML loader.

Each ``[[registry]]`` table maps to one :class:`RegistryConfig`. The
top-level option tables (``[redis]``, ``[scan]``, ``[clean]``,
``[daemon]``, ``[output]``) map to their respective dataclasses, and
:func:`load_config` parses a TOML file into an :class:`AppConfig`.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AppConfig",
    "CleanOptions",
    "ConfigError",
    "DaemonOptions",
    "OutputOptions",
    "RedisOptions",
    "RegistryConfig",
    "ScanOptions",
    "find_config",
    "load_config",
]


# ---------- data classes ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryConfig:
    """A single distribution registry to be checked."""

    name: str
    redis_host: str
    storage_path: str
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class RedisOptions:
    """Tunables for Redis access shared across registries."""

    socket_timeout: float = 10.0
    socket_connect_timeout: float = 5.0
    scan_count: int = 1000
    pipeline_batch: int = 200


@dataclass(frozen=True, slots=True)
class ScanOptions:
    """Tunables for scanning behaviour.

    ``parallel == 0`` means "auto", which the orchestrator interprets as
    "as many enabled registries as configured".
    """

    parallel: int = 0
    verify_digest: bool = False


@dataclass(frozen=True, slots=True)
class CleanOptions:
    """Tunables for the ``clean`` subcommand."""

    strict: bool = False
    retry: int = 1
    clear_internal_garbage: bool = True


@dataclass(frozen=True, slots=True)
class DaemonOptions:
    """Daemon-mode tunables that belong in the config file.

    Schedule and auto-clean are *not* configured here: they are deployment-
    level concerns set via container environment variables (``RCD_SCHEDULE``
    and ``RCD_DAEMON_AUTO_CLEAN``) consumed by ``entrypoint.sh``. Only
    behavioural switches that should travel with the configuration belong
    in this section.
    """

    strict: bool = False


@dataclass(frozen=True, slots=True)
class OutputOptions:
    """Tunables for stdout/stderr output."""

    quiet: bool = False
    no_color: bool = False
    include_digest_list: bool = False


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Fully resolved configuration loaded from disk."""

    schema_version: int
    redis: RedisOptions
    scan: ScanOptions
    clean: CleanOptions
    daemon: DaemonOptions
    output: OutputOptions
    registries: tuple[RegistryConfig, ...]


# ---------- errors ---------------------------------------------------------


class ConfigError(ValueError):
    """Raised when the TOML configuration is missing or invalid."""


# ---------- known-key sets (typo guard) ------------------------------------


_KNOWN_TOP_KEYS = frozenset(
    {
        "schema_version",
        "redis",
        "scan",
        "clean",
        "daemon",
        "output",
        "registry",
    }
)
_KNOWN_REGISTRY_KEYS = frozenset(
    {
        "name",
        "redis_host",
        "redis_port",
        "redis_db",
        "redis_password",
        "storage_path",
        "enabled",
    }
)
_KNOWN_REDIS_KEYS = frozenset(
    {
        "socket_timeout",
        "socket_connect_timeout",
        "scan_count",
        "pipeline_batch",
    }
)
_KNOWN_SCAN_KEYS = frozenset({"parallel", "verify_digest"})
_KNOWN_CLEAN_KEYS = frozenset({"strict", "retry", "clear_internal_garbage"})
_KNOWN_DAEMON_KEYS = frozenset({"strict"})
_KNOWN_OUTPUT_KEYS = frozenset({"quiet", "no_color", "include_digest_list"})


_SUPPORTED_SCHEMA_VERSION = 1


# ---------- public API -----------------------------------------------------


def load_config(path: str | Path) -> AppConfig:
    """Load *path* and return an :class:`AppConfig`.

    Raises :class:`ConfigError` for any kind of validation failure
    (missing file, bad TOML, unknown keys, missing required fields,
    invalid values, etc.).
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"configuration file not found: {p}")

    try:
        with p.open("rb") as fh:
            raw: Mapping[str, Any] = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"failed to parse {p}: {exc}") from exc

    _reject_unknown(raw, _KNOWN_TOP_KEYS, where="top level")

    schema_version = raw.get("schema_version")
    if schema_version is None:
        raise ConfigError("missing required key 'schema_version'")
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise ConfigError(
            f"unsupported schema_version={schema_version!r} "
            f"(expected {_SUPPORTED_SCHEMA_VERSION})"
        )

    redis = _redis_options(raw.get("redis", {}))
    scan = _scan_options(raw.get("scan", {}))
    clean = _clean_options(raw.get("clean", {}))
    daemon = _daemon_options(raw.get("daemon", {}))
    output = _output_options(raw.get("output", {}))

    registries_raw = raw.get("registry") or []
    if not registries_raw:
        raise ConfigError("at least one [[registry]] table is required")

    registries = tuple(_registry_config(item, idx) for idx, item in enumerate(registries_raw))
    _check_unique_names(registries)

    return AppConfig(
        schema_version=int(schema_version),
        redis=redis,
        scan=scan,
        clean=clean,
        daemon=daemon,
        output=output,
        registries=registries,
    )


def find_config(*, explicit: str | os.PathLike[str] | None) -> Path | None:
    """Locate a configuration file using the documented search order.

    Order: explicit argument > ``$RCD_CONFIG`` > ``./registry-cache-doctor.toml``
    > ``$XDG_CONFIG_HOME/registry-cache-doctor/config.toml`` > ``~/.config/...``.
    Returns ``None`` if nothing exists; callers may then either use
    defaults or raise.
    """
    if explicit is not None:
        return Path(explicit)

    env = os.environ.get("RCD_CONFIG")
    if env:
        return Path(env)

    cwd_candidate = Path.cwd() / "registry-cache-doctor.toml"
    if cwd_candidate.exists():
        return cwd_candidate

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    user_candidate = base / "registry-cache-doctor" / "config.toml"
    if user_candidate.exists():
        return user_candidate

    return None


# ---------- section parsers ------------------------------------------------


def _redis_options(section: Mapping[str, Any]) -> RedisOptions:
    _reject_unknown(section, _KNOWN_REDIS_KEYS, where="[redis]")
    scan_count = int(section.get("scan_count", 1000))
    if scan_count <= 0:
        raise ConfigError("[redis].scan_count must be > 0")
    pipeline_batch = int(section.get("pipeline_batch", 200))
    if pipeline_batch <= 0:
        raise ConfigError("[redis].pipeline_batch must be > 0")
    return RedisOptions(
        socket_timeout=float(section.get("socket_timeout", 10.0)),
        socket_connect_timeout=float(section.get("socket_connect_timeout", 5.0)),
        scan_count=scan_count,
        pipeline_batch=pipeline_batch,
    )


def _scan_options(section: Mapping[str, Any]) -> ScanOptions:
    _reject_unknown(section, _KNOWN_SCAN_KEYS, where="[scan]")
    parallel = int(section.get("parallel", 0))
    if parallel < 0:
        raise ConfigError("[scan].parallel must be >= 0")
    return ScanOptions(parallel=parallel, verify_digest=bool(section.get("verify_digest", False)))


def _clean_options(section: Mapping[str, Any]) -> CleanOptions:
    _reject_unknown(section, _KNOWN_CLEAN_KEYS, where="[clean]")
    retry = int(section.get("retry", 1))
    if retry < 0:
        raise ConfigError("[clean].retry must be >= 0")
    return CleanOptions(
        strict=bool(section.get("strict", False)),
        retry=retry,
        clear_internal_garbage=bool(section.get("clear_internal_garbage", True)),
    )


def _daemon_options(section: Mapping[str, Any]) -> DaemonOptions:
    _reject_unknown(section, _KNOWN_DAEMON_KEYS, where="[daemon]")
    return DaemonOptions(
        strict=bool(section.get("strict", False)),
    )


def _output_options(section: Mapping[str, Any]) -> OutputOptions:
    _reject_unknown(section, _KNOWN_OUTPUT_KEYS, where="[output]")
    return OutputOptions(
        quiet=bool(section.get("quiet", False)),
        no_color=bool(section.get("no_color", False)),
        include_digest_list=bool(section.get("include_digest_list", False)),
    )


def _registry_config(item: Any, idx: int) -> RegistryConfig:
    if not isinstance(item, Mapping):
        raise ConfigError(f"[[registry]] #{idx} must be a table")
    _reject_unknown(item, _KNOWN_REGISTRY_KEYS, where=f"[[registry]] #{idx}")

    for required in ("name", "redis_host", "storage_path"):
        if required not in item:
            raise ConfigError(
                f"[[registry]] #{idx} missing required key {required!r}"
            )

    return RegistryConfig(
        name=str(item["name"]),
        redis_host=str(item["redis_host"]),
        storage_path=str(item["storage_path"]),
        redis_port=int(item.get("redis_port", 6379)),
        redis_db=int(item.get("redis_db", 0)),
        redis_password=(
            str(item["redis_password"]) if item.get("redis_password") is not None else None
        ),
        enabled=bool(item.get("enabled", True)),
    )


# ---------- helpers --------------------------------------------------------


def _reject_unknown(
    section: Mapping[str, Any], known: frozenset[str], *, where: str
) -> None:
    unknown = sorted(k for k in section if k not in known)
    if unknown:
        joined = ", ".join(unknown)
        raise ConfigError(f"unknown key(s) at {where}: {joined}")


def _check_unique_names(registries: tuple[RegistryConfig, ...]) -> None:
    seen: set[str] = set()
    for reg in registries:
        if reg.name in seen:
            raise ConfigError(f"duplicate registry name {reg.name!r}")
        seen.add(reg.name)
