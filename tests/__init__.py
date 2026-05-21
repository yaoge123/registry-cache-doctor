"""Test suite root.

Tests are split into:

* ``tests/unit``         — fast, no Docker, no Redis, no network.
* ``tests/integration``  — Docker-based, opt-in via the
  ``INTEGRATION=1`` environment variable in CI.
"""
