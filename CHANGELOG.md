# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-05-24

First functional release.

### Added

- **Scanners.** Read the three Redis key classes used by upstream
  `distribution` (`blobs::sha256:<D>`, `repository::<repo>::blobs`,
  `repository::<repo>::blobs::sha256:<D>`) with two non-transactional
  pipeline-driven `SCAN` passes. Walk the on-disk blob tree
  (`<root>/docker/registry/v2/blobs/sha256/<2hex>/<64hex>/data`) with
  `os.scandir`.
- **Classifier.** Reduce the joined Redis + filesystem snapshot into the
  C1–C7 fault matrix. C4 (cache present, file missing) and C5 (cache
  size disagrees with file) are flagged as the real "unexpected EOF"
  failures; C1/C2/C3/C6 are internal garbage; C7 is self-healing.
- **Cleaner.** `plan_clean` / `execute_clean` / `clean_registry` mirror
  upstream `Clear` semantics: pipelined `SREM` + `DEL` + `HDEL digest
  size mediatype`, configurable batch size and retry, idempotent under
  concurrent registry writes.
- **Orchestrator.** `asyncio.gather` registries in parallel with
  `asyncio.to_thread` for blocking Redis/FS work; per-registry errors
  are captured into the report rather than aborting the run.
- **CLI.** `rcd scan` / `rcd clean` / `rcd inspect` / `rcd version`,
  with `--strict` / `--no-strict` (BooleanOptionalAction) overriding
  TOML defaults, plus `--apply` / `--parallel` / `--no-color` /
  `--with-refs` / `--config`. Exit codes 0/1/2/3/4 per spec.
- **NDJSON output.** `scan_completed` / `run_summary` /
  `clean_completed` / `inspect` events on stdout (one JSON per line),
  human-readable progress on stderr, `NO_COLOR` and `--no-color`
  honoured.
- **Configuration.** TOML schema (`schema_version = 1`) with
  `[redis]`, `[scan]`, `[clean]`, `[[registry]]` sections; unknown keys
  rejected. Search order: `--config` → `$RCD_CONFIG` → `$PWD` →
  `$XDG_CONFIG_HOME` → `~/.config`.
- **Container image.** Multi-stage `python:3.13-alpine` build, `tini`
  + `supercronic` v0.2.30, `tzdata` so `TZ` works without bind-mounting
  the host zoneinfo tree, non-root `rcd` user.
- **Daemon mode.** Container entrypoint; `acme.sh`-style same-image
  two-entrypoint pattern. Environment variables consumed by
  `entrypoint.sh`: `RCD_CONFIG`, `RCD_SCHEDULE` (default
  `0 3 * * *`), `RCD_DAEMON_AUTO_CLEAN` (default `false`),
  `RCD_DAEMON_STRICT` (default `false`). A small POSIX wrapper
  (`rcd-cron-exec`) rewrites the drift exit code 2 to 0 so cron does
  not log false alarms.
- **Tests.** Unit coverage for scanners, classifier, cleaner,
  orchestrator, CLI exit codes, NDJSON shape, progress, stderr log,
  config loader (schema validation, search-order, error paths),
  `entrypoint.sh` shell behaviour, and the cron-exec wrapper.
- **CI/CD.** GitHub Actions: `ruff` + `mypy --strict`, `pytest` matrix
  on Python 3.11 / 3.12 / 3.13, multi-arch (`linux/amd64` +
  `linux/arm64`) image build. `main` branch pushes publish rolling
  `main` / `edge` / `sha-<short>` tags to GHCR; release tags
  (`v*`) publish versioned tags (`<version>`, `<major>.<minor>`,
  `latest`) plus a GitHub Release with sdist/wheel artefacts.

### Verified in production

- Read-only scan and `--apply` clean exercised across seven
  upstream-distribution pull-through caches: ~1.5 M Redis keys
  classified and removed end-to-end, no failures, no retries,
  pipeline rate ~30 k/s.

## [0.0.0] — 2026-05-21

Initial scaffold tag. Not functional yet.
