"""Configuration data structures.

These are pure data types. The TOML parser that populates them lives in
this module too as ``load_config``, but Phase 3 only exposes the
dataclasses; the loader will be added in Phase 5.

Each ``[[registry]]`` table in the TOML file maps to one
:class:`RegistryConfig`. The top-level ``[redis]`` and ``[scan]`` tables
map to :class:`RedisOptions` and :class:`ScanOptions` respectively.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegistryConfig:
    """A single distribution registry to be checked.

    ``redis_host`` and ``redis_port`` point at the Redis backing the
    registry's blob descriptor cache. ``storage_path`` is the registry
    ``rootdirectory`` (i.e. the directory that contains
    ``docker/registry/v2/blobs``).
    """

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
