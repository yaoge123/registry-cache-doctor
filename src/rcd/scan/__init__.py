"""Scanning subpackage.

* :mod:`rcd.scan.redis_scan` -- SCAN-driven walk of the three Redis key shapes.
* :mod:`rcd.scan.fs_scan`    -- Filesystem walk of ``<root>/docker/registry/v2/blobs``.
"""

from rcd.scan.fs_scan import scan_fs
from rcd.scan.redis_scan import scan_redis

__all__ = ["scan_fs", "scan_redis"]
