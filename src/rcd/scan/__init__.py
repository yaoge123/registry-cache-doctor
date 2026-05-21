"""Scanning subpackage.

Concrete implementations land in Phase 1:

* :mod:`rcd.scan.redis_scan` — SCAN-driven walk of the three Redis key shapes.
* :mod:`rcd.scan.fs_scan`    — Filesystem walk of ``<root>/docker/registry/v2/blobs``.
"""
