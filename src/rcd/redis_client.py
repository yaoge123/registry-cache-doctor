"""Build redis-py client factories from configuration objects."""

from __future__ import annotations

from typing import Any

import redis

from rcd.config import RedisOptions, RegistryConfig
from rcd.orchestrator import RedisClientFactory

__all__ = ["make_redis_factory"]


def make_redis_factory(opts: RedisOptions) -> RedisClientFactory:
    """Return a callable that constructs a ``redis.Redis`` per registry.

    The cache layer expects raw ``bytes`` responses; ``decode_responses`` is
    pinned to ``False`` so digest values (which can include arbitrary byte
    sequences in mediatype) round-trip unchanged.
    """

    def _factory(cfg: RegistryConfig) -> Any:
        kwargs: dict[str, Any] = {
            "host": cfg.redis_host,
            "port": cfg.redis_port,
            "db": cfg.redis_db,
            "decode_responses": False,
            "socket_timeout": opts.socket_timeout,
            "socket_connect_timeout": opts.socket_connect_timeout,
        }
        if cfg.redis_password is not None:
            kwargs["password"] = cfg.redis_password
        return redis.Redis(**kwargs)

    return _factory
