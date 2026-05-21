# registry-cache-doctor

诊断并修复 [`distribution/distribution`](https://github.com/distribution/distribution)
registry 在使用 Redis 作为 blob descriptor cache 时与本地 blob 文件系统的不一致
（含 pull-through cache 部署形态）。

> **当前状态：** v0.0.0 — 仅骨架。功能将在 `v0.1.0` 落地。

[English](./README.md)

---

## 解决什么问题

distribution registry 在配置 `REGISTRY_STORAGE_CACHE_BLOBDESCRIPTOR=redis`
后，会把 blob descriptor 写入 Redis 缓存。Redis cache 与本地 blob 文件
分别由不同代码路径写入，上游源码不维护两者间一致性约束。当两者发生漂移，
`docker pull` 表现为：

- `unexpected EOF`
- `digest verification failed`
- `transfer closed with N bytes remaining to read`

上游已知但仍然 Open 的相关 issue：
[distribution/distribution#2367](https://github.com/distribution/distribution/issues/2367)、
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
- 比对两侧、分类、按 NDJSON 写出 stdout
- `clean` 使用与上游 `Clear` 同语义的 `SREM` + `DEL` + `HDEL` pipeline
- 默认只读，写操作必须显式 `--apply`
- 单 OCI 镜像分发：既支持一次性子命令，也支持 cron 调度的 `daemon` 入口

详细设计见 `REQUIREMENTS.md`（不发布到 GitHub，需要可在 issue 提问）。

## 快速开始

> 下述命令假设镜像已本地构建为 `registry-cache-doctor:local`。

```sh
docker build -t registry-cache-doctor:local .

# 一次性扫描
docker run --rm \
  --network <你的 registry 容器网络> \
  -v <你的存储根目录>:<你的存储根目录>:ro \
  -v "$PWD/registry-cache-doctor.toml:/etc/rcd/config.toml:ro" \
  registry-cache-doctor:local scan

# 常驻调度模式
docker compose -f examples/docker-compose.yml up -d
```

配置文件是 TOML 格式，完整带注释的模板见
[`examples/registry-cache-doctor.toml`](./examples/registry-cache-doctor.toml)。

## 子命令（v0.1.0 计划）

| 命令 | 作用 |
|---|---|
| `scan`   | 只读扫描所有已配置 registry；输出 NDJSON |
| `clean`  | 按所选分类清理 Redis（需 `--apply`） |
| `inspect`| 按 digest 查看详情，可选附引用仓库列表 |
| `daemon` | 以 cron 调度运行 `scan`（可选 `clean`） |
| `version`| 打印版本号 |

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
pip install -e .[dev]
pytest
```

支持的 Python 版本：**3.11、3.12、3.13**。

## 许可证

[MIT](./LICENSE)
