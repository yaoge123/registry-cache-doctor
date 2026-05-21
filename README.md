# registry-cache-doctor

Diagnose and repair Redis blob descriptor cache inconsistencies in
[`distribution/distribution`](https://github.com/distribution/distribution)
registries (including pull-through cache deployments).

> **Status:** v0.0.0 — scaffold only. Functionality lands in `v0.1.0`.
> See [`CHANGELOG.md`](./CHANGELOG.md) and the milestones on GitHub.

[简体中文文档](./README.zh-CN.md)

---

## What it does

`distribution` registries can be configured to keep blob descriptors in a
Redis cache (`REGISTRY_STORAGE_CACHE_BLOBDESCRIPTOR=redis`). The cache and
the on-disk blob storage are written from independent code paths, and the
upstream code keeps no consistency invariant between the two. When they
drift, `docker pull` can fail with:

- `unexpected EOF`
- `digest verification failed`
- `transfer closed with N bytes remaining to read`

These symptoms are tracked upstream in
[distribution/distribution#2367](https://github.com/distribution/distribution/issues/2367),
[#3722](https://github.com/distribution/distribution/issues/3722) and
[#4752](https://github.com/distribution/distribution/issues/4752).

`registry-cache-doctor` (`rcd`) scans both sides, classifies any drift into
seven categories `C1`–`C7`, and can clear the bad Redis entries with the
same pipeline semantics that `distribution` itself uses internally.

## Design summary

- Reads three Redis key shapes used by `distribution`:
  - `blobs::sha256:<D>` — global hash (`digest`, `size`, `mediatype`)
  - `repository::<repo>::blobs` — per-repository set of digests
  - `repository::<repo>::blobs::sha256:<D>` — per-repository hash (`mediatype`)
- Walks the `<rootdirectory>/docker/registry/v2/blobs/sha256/` tree once and
  `stat()`s each blob.
- Compares both views, classifies the result, and emits NDJSON to stdout.
- `clean` issues `SREM` + `DEL` + `HDEL` in a Redis pipeline matching
  the upstream `Clear` implementation.
- Default mode is read-only. Mutations require an explicit `--apply`.
- Ships as a single OCI image: one-shot subcommands, plus a `daemon`
  entrypoint that runs on a cron schedule.

Full design notes live in `REQUIREMENTS.md` (kept out of the published
repository — drop a question on the issue tracker if you need details).

## Quick start

> All commands below assume the image has been built locally as
> `registry-cache-doctor:local`.

```sh
docker build -t registry-cache-doctor:local .

# One-shot scan
docker run --rm \
  --network <your-registry-network> \
  -v <your-storage-root>:<your-storage-root>:ro \
  -v "$PWD/registry-cache-doctor.toml:/etc/rcd/config.toml:ro" \
  registry-cache-doctor:local scan

# Long-running scheduled mode
docker compose -f examples/docker-compose.yml up -d
```

Configuration is a TOML file. A fully commented template lives at
[`examples/registry-cache-doctor.toml`](./examples/registry-cache-doctor.toml).

## Subcommands (planned for v0.1.0)

| Command | Purpose |
|---|---|
| `scan`   | Read-only scan of every configured registry; emit NDJSON summary |
| `clean`  | Apply Redis cleanup for the categories selected (requires `--apply`) |
| `inspect`| Per-digest detail view, optionally with referencing repositories |
| `daemon` | Run `scan` (and optionally `clean`) on a cron schedule |
| `version`| Print the installed version |

## Exit codes

| Code | Default | `--strict` |
|---|---|---|
| 0 | success or only `C7` (self-healing) found | same |
| 1 | tool error (config, connection, etc.) | same |
| 2 | dry-run found drift, or `clean` had recoverable failures | same |
| 3 | — | `C4` or `C5` (real failure) found |
| 4 | — | one or more `clean` operations failed permanently |

## Building from source

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e .[dev]
pytest
```

Supported Python versions: **3.11, 3.12, 3.13**.

## License

[MIT](./LICENSE)
