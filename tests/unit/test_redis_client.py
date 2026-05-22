"""Tests for the redis-py client factory."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from rcd.config import RedisOptions, RegistryConfig
from rcd.redis_client import make_redis_factory


class _FakeRedis:
    last_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs


def test_factory_returns_callable() -> None:
    factory = make_redis_factory(RedisOptions())
    assert callable(factory)


def test_factory_passes_registry_connection_params() -> None:
    cfg = RegistryConfig(
        name="reg",
        redis_host="rhost",
        storage_path="/tmp/storage",
        redis_port=6380,
        redis_db=2,
        redis_password="secret",
    )
    opts = RedisOptions(socket_timeout=12.0, socket_connect_timeout=4.0)

    with patch("rcd.redis_client.redis.Redis", _FakeRedis):
        factory = make_redis_factory(opts)
        client = factory(cfg)

    assert isinstance(client, _FakeRedis)
    kwargs = _FakeRedis.last_kwargs
    assert kwargs is not None
    assert kwargs["host"] == "rhost"
    assert kwargs["port"] == 6380
    assert kwargs["db"] == 2
    assert kwargs["password"] == "secret"
    assert kwargs["socket_timeout"] == pytest.approx(12.0)
    assert kwargs["socket_connect_timeout"] == pytest.approx(4.0)
    # Cache code expects raw bytes; redis-py default is False but we pin it.
    assert kwargs["decode_responses"] is False


def test_factory_omits_password_when_none() -> None:
    cfg = RegistryConfig(
        name="reg",
        redis_host="rhost",
        storage_path="/tmp/storage",
    )
    with patch("rcd.redis_client.redis.Redis", _FakeRedis):
        factory = make_redis_factory(RedisOptions())
        factory(cfg)

    kwargs = _FakeRedis.last_kwargs
    assert kwargs is not None
    assert "password" not in kwargs or kwargs["password"] is None
