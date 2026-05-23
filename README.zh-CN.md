# registry-cache-doctor

诊断并修复 [`distribution/distribution`](https://github.com/distribution/distribution)
registry 在使用 Redis 作为 blob descriptor cache 时与本地 blob 文件系统的不一致
（含 pull-through cache 部署形态）。

> **当前状态：** 功能开发完成，`v0.1.0` 候选发布版本。
> CI 在 Python 3.11 / 3.12 / 3.13 上跑全套测试。
> 见 [`CHANGELOG.md`](./CHANGELOG.md)。

[English](./README.md)

---

## 解决什么问题

distribution registry 在配置 `REGISTRY_STORAGE_CACHE_BLOBDESCRIPTOR=redis`
后，blob descriptor 写到 Redis、blob 字节写到 storage backend，两者由不同
代码路径写入，上游不维护两者间一致性约束。当两者发生漂移，`docker pull`
表现为：

- `unexpected EOF`
- `digest verification failed`
- `transfer closed with N bytes remaining to read`

上游已知但仍然 Open 的相关 issue：
[#2367](https://github.com/distribution/distribution/issues/2367)、
[#3722](https://github.com/distribution/distribution/issues/3722)、
[#4752](https://github.com/distribution/distribution/issues/4752)。

`registry-cache-doctor`（CLI 名 `rcd`）扫描两侧、按 `C1`–`C7` 七类对漂移
进行分类、并能以与 `distribution` 自身相同的 pipeline 语义清理坏的 Redis 项。

## 设计要点

- 读取 distribution 使用的三类 Redis key：
  - `blobs::sha256:<D>` — 全局 hash（`digest`、`size`、`mediatype`）
  - `repository::<repo>::blobs` — 每仓库的 digest 集合
  - `repository::<repo>::blobs::sha256:<D>` — 每仓库 hash（`mediatype`）
- 遍历 `<rootdirectory>/docker/registry/v2/blobs/sha256/` 一次，每个 blob 仅 `stat` 一次
- Redis 用 `SCAN` + 非事务 pipeline；存储用 `os.scandir`。两侧每个 registry 内并发，多 registry 间也并行
- 比对两侧、分类、按 NDJSON 写出 stdout
- `clean` 使用与上游 `Clear` 同语义的 `SREM` + `DEL` + `HDEL` pipeline，失败重试一次后记录
- 默认只读，写操作必须显式 `--apply`
- 单 OCI 镜像分发：既支持一次性子命令，也支持基于
  [`supercronic`](https://github.com/aptible/supercronic) 的 cron 调度 `daemon` 入口

## 故障矩阵

| 分类 | Redis 状态 | 存储 | 表现 | 真故障？ |
|---|---|---|---|---|
| `C1` | 仅全局 hash 存在 | 任意 | 不会被 pull 命中 | 否 — 内部垃圾 |
| `C2` | repo set 含 `D`，全局 key/size 缺 | 任意 | fallback 到 backend | 否 — 内部垃圾 |
| `C3` | repo set + 全局 ok，repo hash 缺 | 任意 | fallback 到 backend | 否 — 内部垃圾 |
| `C4` | 三类 Redis key 全在 | blob 文件丢失 | `unexpected EOF` | **是** |
| `C5` | 三类 Redis key 全在 | size 与 cache 不一致 | `unexpected EOF` / `digest verification failed` | **是** |
| `C6` | repo hash 在，repo set 不含 `D` | 任意 | fallback 到 backend | 否 — 内部垃圾 |
| `C7` | Redis 完全不引用 `D` | blob 文件存在 | 下次 pull 自愈 | 否 — 自愈中 |

`clean` 默认清理 `C4`/`C5`（真故障）以及 `C1`/`C2`/`C3`/`C6`（内部垃圾），
`C7` 不动。

## 快速开始

> 下述命令假设镜像已本地构建为 `registry-cache-doctor:local`。

```sh
docker build -t registry-cache-doctor:local .

# 只读扫描
docker run --rm \
  --network <你的 registry 容器网络> \
  -v <你的存储根目录>:<你的存储根目录>:ro \
  -v "$PWD/registry-cache-doctor.toml:/etc/rcd/config.toml:ro" \
  registry-cache-doctor:local scan

# 常驻 cron 调度
docker compose -f examples/docker-compose.yml up -d
```

配置文件是 TOML 格式，完整带注释的模板见
[`examples/registry-cache-doctor.toml`](./examples/registry-cache-doctor.toml)。

## 子命令

| 命令 | 作用 |
|---|---|
| `scan`   | 只读扫描所有已配置 registry，输出 NDJSON |
| `clean`  | 按所选分类清理 Redis（需 `--apply`，否则 dry-run） |
| `inspect`| 按 digest 查看详情，可选附引用仓库列表 |
| `version`| 打印版本号 |

`daemon` **不是** CLI 子命令，而是把 `daemon` 作为容器命令传给镜像 ——
见下文 [Daemon 模式](#daemon-模式cron)。

### CLI 参数

| 参数 | 适用 | 作用 |
|---|---|---|
| `--config PATH` | 顶层 | TOML 配置路径（覆盖 `RCD_CONFIG` 和搜索顺序） |
| `--parallel N` | `scan` / `clean` / `inspect` | 限制并发 registry 数；`0` = 启用 registry 个数（默认） |
| `--no-color` | `scan` / `clean` / `inspect` | 关闭 stderr ANSI 颜色（`NO_COLOR=1` 同效） |
| `--quiet` | `scan` / `clean` / `inspect` | 抑制 stderr 进度/日志 |
| `--strict` | `scan` / `clean` | 真故障升级为非 0 退出码（见下文） |
| `--apply` | `clean` | 实际写 Redis；不带则 dry-run |
| `--verify-digest` | `scan` | 重算 blob sha256 而非仅比对 size（慢） |
| `--with-refs` | `inspect` | 输出每个 digest 的引用仓库列表 |
| `--registry NAME` | `inspect` | 必填：要查看的 registry |
| `--category Cn` | `inspect` | 必填：要列出的分类 |

### 配置文件查找顺序

1. `--config PATH`
2. `$RCD_CONFIG`
3. `./registry-cache-doctor.toml`
4. `$XDG_CONFIG_HOME/registry-cache-doctor/config.toml`
   （未设则 `~/.config/...`）

顶层结构（只有 `[[registry]]` 必填，其他都有默认值）：

```toml
schema_version = 1
network = "my-registry-net"          # 给 docker-compose 用，rcd 忽略

[redis]
db = 0
socket_timeout = 10
socket_connect_timeout = 5
scan_count = 1000
pipeline_batch = 200

[scan]
parallel = 0                         # 0 = 启用 registry 个数
verify_digest = false

[clean]
strict = false
retry = 1
clear_internal_garbage = true        # 一并清 C1/C2/C3/C6，不仅是 C4/C5

[daemon]
# schedule 与 auto_clean 不在 TOML，由容器环境变量驱动（见 Daemon 模式章节）。
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

### NDJSON 输出

`scan` 和 `clean` 每个事件一行 JSON，所有事件共有
`schema_version`、`tool`、`version`、`ts`、`run_id`、`event`。

| 事件 | 额外字段 |
|---|---|
| `scan_completed` | `registry`、`duration_s`、`redis_keys.{global,repo_set,repo_hash}`、`categories.{ok,c1,…,c7}`、`errors[]` |
| `run_summary` | `duration_s`、`totals.{ok,c1,…,c7}` |
| `clean_completed` | `registry`、`planned`、`applied`、`failed`、`retried`、`duration_s` |
| `inspect` | `registry`、`category`、`digest`、`repo`、`redis_size`、`fs_size`、可选 `refs[]` |

## Daemon 模式（cron）

把 `daemon` 作为容器命令传给镜像，entrypoint 会从环境变量生成 crontab
并 exec `supercronic`：

```sh
docker run -d \
  --name rcd \
  --network <你的 registry 容器网络> \
  -v <你的存储根目录>:<你的存储根目录>:ro \
  -v "$PWD/registry-cache-doctor.toml:/etc/rcd/config.toml:ro" \
  -e RCD_SCHEDULE='0 3 * * *' \
  -e RCD_DAEMON_AUTO_CLEAN=false \
  registry-cache-doctor:local daemon
```

entrypoint 识别的环境变量：

| 变量 | 默认 | 作用 |
|---|---|---|
| `RCD_CONFIG` | `/etc/rcd/config.toml` | 挂载的配置路径 |
| `RCD_SCHEDULE` | `0 3 * * *` | `rcd scan` 的 cron 表达式 |
| `RCD_DAEMON_AUTO_CLEAN` | `false` | 设为 `true` 时同时调度 `rcd clean --apply` |

所有输出走容器 stdout/stderr，用 `docker logs` 或你的日志收集器读取。

## 退出码

| 码 | 默认 | `--strict` |
|---|---|---|
| 0 | 成功或仅发现 `C7`（自愈中） | 同左 |
| 1 | 工具错误（配置 / 连接等） | 同左 |
| 2 | dry-run 发现漂移或 `clean` 出现可恢复失败 | 同左 |
| 3 | — | 发现 `C4` 或 `C5`（真故障） |
| 4 | — | 至少一次 `clean` 永久失败 |

## 从源码构建

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
mypy --strict src/rcd
```

支持的 Python 版本：**3.11、3.12、3.13**。

## 许可证

[MIT](./LICENSE)
