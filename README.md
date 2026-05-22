# registry-cache-doctor

Diagnose and repair Redis blob descriptor cache inconsistencies in
[`distribution/distribution`](https://github.com/distribution/distribution)
registries (including pull-through cache deployments).

> **Status:** functionality complete; release candidate for `v0.1.0`.
> CI matrix runs on Python 3.11 / 3.12 / 3.13. See
> [`CHANGELOG.md`](./CHANGELOG.md).

[简体中文文档](./README.zh-CN.md)

---

## What it does

When a `distribution` registry is configured with
`REGISTRY_STORAGE_CACHE_BLOBDESCRIPTOR=redis`, blob descriptors are written
to Redis and the actual blob bytes are written to the storage backend by
independent code paths. The upstream code keeps no consistency invariant
between the two. When they drift, `docker pull` can fail with:

- `unexpected EOF`
- `digest verification failed`
- `transfer closed with N bytes remaining to read`

These symptoms are tracked upstream in
[#2367](https://github.com/distribution/distribution/issues/2367),
[#3722](https://github.com/distribution/distribution/issues/3722) and
[#4752](https://github.com/distribution/distribution/issues/4752) — all
still open at the time of writing.

`registry-cache-doctor` (`rcd`) scans both sides, classifies any drift
into seven categories `C1`–`C7`, and can clear the bad Redis entries
using the same pipeline semantics that `distribution` itself uses
internally.

## Design summary

- Reads three Redis key shapes used by `distribution`:
  - `blobs::sha256:<D>` — global hash (`digest`, `size`, `mediatype`)
  - `repository::<repo>::blobs` — per-repository set of digests
  - `repository::<repo>::blobs::sha256:<D>` — per-repository hash
    (`mediatype`)
- Walks `<rootdirectory>/docker/registry/v2/blobs/sha256/` once and
  `stat()`s each blob.
- Scans Redis with `SCAN` and a non-transactional pipeline; storage with
  `os.scandir`. Both run concurrently per registry; registries run in
  parallel.
- Compares both views, classifies the result, and emits NDJSON to
  stdout.
- `clean` issues `SREM` + `DEL` + `HDEL` in a Redis pipeline matching
  the upstream `Clear` implementation. Failures are retried once and
  recorded.
- Default mode is read-only. Mutations require an explicit `--apply`.
- Ships as a single OCI image: one-shot subcommands, plus a `daemon`
  entrypoint that runs on a cron schedule via
  [`supercronic`](https://github.com/aptible/supercronic).

## Fault matrix

| Category | Redis state | Storage | Symptom | Real failure? |
|---|---|---|---|---|
| `C1` | only the global hash exists | any | not hit by pulls | no — internal garbage |
| `C2` | repo set lists `D`, global key/size missing | any | falls back to backend | no — internal garbage |
| `C3` | repo set + global ok, repo hash missing | any | falls back to backend | no — internal garbage |
| `C4` | all three Redis keys present | blob file missing | `unexpected EOF` | **yes** |
| `C5` | all three Redis keys present | size disagrees with cache | `unexpected EOF` / `digest verification failed` | **yes** |
| `C6` | repo hash present, repo set does not list `D` | any | falls back to backend | no — internal garbage |
| `C7` | no Redis entries reference `D` | blob file present | self-heals on next pull | no — self-healing |

`clean` clears `C4` and `C5` (real failures) plus `C1`/`C2`/`C3`/`C6`
(internal garbage) by default. `C7` is left untouched.

## Quick start

> All commands below assume the image has been built locally as
> `registry-cache-doctor:local`.

```sh
docker build -t registry-cache-doctor:local .

# Read-only scan
docker run --rm \
  --network <your-registry-network> \
  -v <your-storage-root>:<your-storage-root>:ro \
  -v "$PWD/registry-cache-doctor.toml:/etc/rcd/config.toml:ro" \
  registry-cache-doctor:local scan

# Long-running scheduled mode (cron)
docker compose -f examples/docker-compose.yml up -d
```

Configuration is a TOML file. A fully commented template lives at
[`examples/registry-cache-doctor.toml`](./examples/registry-cache-doctor.toml).

## Subcommands

| Command | Purpose |
|---|---|
| `scan`   | Read-only scan of every configured registry; emit NDJSON summary |
| `clean`  | Apply Redis cleanup for the categories selected (requires `--apply`) |
| `inspect`| Per-digest detail view, optionally with referencing repositories |
| `version`| Print the installed version |

`daemon` is **not** a CLI subcommand. Pass `daemon` as the container
command instead — see [Daemon mode](#daemon-mode-cron) below.

### CLI options

| Flag | Where | Effect |
|---|---|---|
| `--config PATH` | top-level | Path to the TOML config (overrides `RCD_CONFIG` and the search path) |
| `--parallel N` | `scan` / `clean` / `inspect` | Cap concurrent registries; `0` = number of enabled registries (default) |
| `--no-color` | `scan` / `clean` / `inspect` | Disable ANSI colour on stderr (also honoured: `NO_COLOR=1`) |
| `--quiet` | `scan` / `clean` / `inspect` | Suppress stderr progress / log lines |
| `--strict` | `scan` / `clean` | Promote real failures to non-zero exit codes (see below) |
| `--apply` | `clean` | Actually issue Redis writes; without it `clean` is a dry-run |
| `--verify-digest` | `scan` | Re-hash blob bytes when comparing sizes (slow) |
| `--with-refs` | `inspect` | Include the list of repositories that reference each digest |
| `--registry NAME` | `inspect` | Required: which registry to inspect |
| `--category Cn` | `inspect` | Required: which category to list |

### Configuration file

The CLI looks for a config file in this order:

1. `--config PATH`
2. `$RCD_CONFIG`
3. `./registry-cache-doctor.toml`
4. `$XDG_CONFIG_HOME/registry-cache-doctor/config.toml`
   (or `~/.config/...` if unset)

Top-level layout (only the `[[registry]]` array is required; everything
else has defaults):

```toml
schema_version = 1
network = "my-registry-net"          # consumed by docker-compose; ignored by rcd

[redis]
db = 0
socket_timeout = 10
socket_connect_timeout = 5
scan_count = 1000
pipeline_batch = 200

[scan]
parallel = 0                         # 0 = number of enabled registries
verify_digest = false

[clean]
strict = false
retry = 1
clear_internal_garbage = true        # also clean C1/C2/C3/C6, not just C4/C5

[daemon]
schedule = "0 3 * * *"
auto_clean = false
strict = false

[output]
quiet = false
no_color = false
include_digest_list = false

[[registry]]
name = "my-registry"
redis_host = "my-registry-redis"
redis_port = 6379
storage_path = "/var/lib/registry"
# redis_password = "..."
# redis_db = 0
# enabled = true
```

### NDJSON output

`scan` and `clean` write one JSON object per line to stdout. All events
share `schema_version`, `tool`, `version`, `ts`, `run_id` and `event`.

| Event | Additional fields |
|---|---|
| `scan_completed` | `registry`, `duration_s`, `redis_keys.{global,repo_set,repo_hash}`, `categories.{ok,c1,…,c7}`, `errors[]` |
| `run_summary` | `duration_s`, `totals.{ok,c1,…,c7}` |
| `clean_completed` | `registry`, `planned`, `applied`, `failed`, `retried`, `duration_s` |
| `inspect` | `registry`, `category`, `digest`, `repo`, `redis_size`, `fs_size`, optional `refs[]` |

## Daemon mode (cron)

Pass `daemon` as the container's command. The entrypoint generates a
crontab from environment variables and execs `supercronic`:

```sh
docker run -d \
  --name rcd \
  --network <your-registry-network> \
  -v <your-storage-root>:<your-storage-root>:ro \
  -v "$PWD/registry-cache-doctor.toml:/etc/rcd/config.toml:ro" \
  -e RCD_SCHEDULE='0 3 * * *' \
  -e RCD_DAEMON_AUTO_CLEAN=false \
  registry-cache-doctor:local daemon
```

Environment variables consumed by the entrypoint:

| Variable | Default | Effect |
|---|---|---|
| `RCD_CONFIG` | `/etc/rcd/config.toml` | Path of the mounted config |
| `RCD_SCHEDULE` | `0 3 * * *` | Cron expression for `rcd scan` |
| `RCD_DAEMON_AUTO_CLEAN` | `false` | When `true`, also schedule `rcd clean --apply` on the same cron |

All output goes to the container's stdout/stderr; pick it up with
`docker logs` or your preferred log shipper.

## Exit codes

| Code | Default | `--strict` |
|---|---|---|
| 0 | success, or only `C7` (self-healing) found | same |
| 1 | tool error (config, connection, etc.) | same |
| 2 | dry-run found drift, or `clean` had recoverable failures | same |
| 3 | — | `C4` or `C5` (real failure) found |
| 4 | — | one or more `clean` operations failed permanently |

## Building from source

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
mypy --strict src/rcd
```

Supported Python versions: **3.11, 3.12, 3.13**.

## License

[MIT](./LICENSE)
