"""Per-registry and run-level result types emitted by the orchestrator.

These are the in-memory shape that gets serialised to NDJSON in
:mod:`rcd.output.ndjson`. Keeping them separate from the wire format
lets the orchestrator be tested without touching JSON.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from rcd.classifier import Category, Classification


@dataclass(frozen=True, slots=True)
class RedisKeyCounts:
    """How many keys of each shape were observed in Redis.

    These are raw key counts, not blob counts: ``repo_set`` is the
    number of *sets* (one per repository), not the total number of
    digest references inside them.
    """

    global_hash: int
    repo_set: int
    repo_hash: int


@dataclass(frozen=True, slots=True)
class RegistryReport:
    """Result of scanning a single registry."""

    registry: str
    duration_s: float
    redis_keys: RedisKeyCounts
    classification: Classification
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Aggregate result across every scanned registry."""

    duration_s: float
    totals: Mapping[Category, int] = field(default_factory=dict)
