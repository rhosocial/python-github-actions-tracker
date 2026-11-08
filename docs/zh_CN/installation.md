# 安装

## 安装分两种：全新与遗留

这里的“全新”和“遗留”是**相对数据库而言的**，不是相对软件包而言。

当前数据库模式尚未定型，数据表存在被整体推翻并重建的可能。因此，遗留安装适合能够自行管理 schema 的部署；
如果数据库中的数据可以丢弃，建议按全新安装处理，以便后续重建时不受旧表结构限制。

| 类型 | 数据库状态 | 安装顺序 |
|---|---|---|
| **全新安装** | 本服务第一次使用这个数据库 | 第一步先初始化数据库，然后再启动服务 |
| **遗留安装** | 数据库已存在，schema 和凭据由自己管理 | 不要执行初始化，直接启动服务 |

区分方式很简单：如果 `ghat_*` 表还不存在，就是全新安装；如果已经存在，可能是全新安装也可能是升级，
此时要保留数据，就按遗留安装处理。

---

## 全新安装

### 第 1 步：初始化数据库

全新安装必须先有数据库，再启动服务。初始化会创建 `ghat_*` 表，但不清空已有数据。

原生方式：

```bash
# 有建库权限时会同时创建数据库；托管服务请加 --no-create-database
rhosocial-github-actions-tracker-db init
```

容器方式：

```bash
docker compose --env-file docker/stack.env up -d      # init 先跑，成功后 web / mcp 才启动
# 或用一次性 init 容器
docker compose run --rm -e GHAT_INITDB=auto init
```

单容器方式需要把 `init.d` 挂进去，否则首次启动会明确报错：

```bash
docker run -d --name tracker-web --restart unless-stopped \
  -v "$PWD/docker/init.d:/docker-entrypoint-init.d:ro" \
  -v /path/to/certs:/certs:ro \
  -e GHAT_DB_HOST=<db-host> -e GHAT_DB_PORT=5432 \
  -e GHAT_DB_USER=<user> -e GHAT_DB_PASSWORD=<password> \
  -e GHAT_DB_NAME=github_actions_tracker \
  -e GHAT_API_KEY=<bearer> \
  -e GHAT_BOOTSTRAP_PAT=<fine-grained-pat> \
  -e GHAT_WEBHOOK_SSL_CERTFILE=/certs/fullchain.pem \
  -e GHAT_WEBHOOK_SSL_KEYFILE=/certs/privkey.pem \
  registry.cn-shanghai.aliyuncs.com/vistart_public/github-actions-tracker:latest   # 国内
  # docker.io/vistart/github-actions-tracker:latest                               # 国外，二选一
```

`GHAT_INITDB` 三个取值：`auto` 是表不存在时执行，`force` 是总是执行，`skip` 是不执行。
`GHAT_LEGACY=1` 等价于 `GHAT_INITDB=skip`。

### 第 2 步：登记凭据

原生安装需要显式执行一次，等价于容器中的 `20-pat.sh`：

```bash
rhosocial-github-actions-tracker credential pat ensure \
  --name work --token-file /path/to/github-actions-tracker-pat --probe-repo owner/repo
```

容器安装把 PAT 放进 `docker/stack.env`，`20-pat.sh` 会在初始化时读：

```dotenv
GHAT_BOOTSTRAP_PAT=github_pat_xxx
GHAT_BOOTSTRAP_PAT_NAME=work
GHAT_BOOTSTRAP_PROBE_REPO=owner/repo
```

若不想让令牌出现在 `docker inspect` 中，请把令牌放进一个目录，并把该目录挂进 `init` 容器：

```yaml
volumes:
  - ./docker/secrets:/run/secrets:ro
```

```dotenv
GHAT_BOOTSTRAP_PAT_FILE=/run/secrets/github-actions-tracker-pat
```

### 第 3 步：启动服务

**原生方式：**

```bash
pip install rhosocial-github-actions-tracker                # 若还没有安装包
rhosocial-github-actions-tracker serve all                  # REST :8000 + webhook :18080
rhosocial-github-actions-tracker serve mcp                  # MCP :8001
```

`pip install rhosocial-github-actions-tracker` 提供两个命令行入口。镜像和 CI 都运行
**Python 3.15**；`requires-python` 保留 3.10 以满足一般安装场景。

compose 方式在初始化完成后直接启动两个常驻容器：

```bash
cp docker/stack.env.example docker/stack.env   # 填数据库、API key、webhook secret、PAT
mkdir -p docker/certs && cp <fullchain.pem> <privkey.pem> docker/certs/
docker compose --env-file docker/stack.env up -d
curl -k https://127.0.0.1:18080/readyz        # {"status":"ready",...}
```

#### 原生环境准备

Docker 不是必需项。原生安装得到的是同一套服务，行为相同，差别只在首次初始化由谁触发、
常驻进程由谁守护。

所有 compose `environment:` 配置项在原生部署里都来自 `GHAT_*` 环境变量，两个命令都会读取：
数据库坐标、API key、webhook 回调地址、TLS 路径与 PAT 名。不需要配置文件、init 目录或数据卷。
`docker/stack.env.example` 仍然可以直接作为逐项检查清单。

| 容器中 | 原生部署 |
|---|---|
| `init` 先运行一次，成功后才启动 `web` 与 `mcp` | 先运行 `db init`，再启动两个进程 |
| `restart: unless-stopped` 自动重启容器 | 自行守护两个 `serve` 进程 |

开始前检查环境：

```bash
python .claude/skills/ops-deploy-image/scripts/check_native_env.py
```

退出码 `0` 表示就绪，`2` 表示可用但存在问题，`1` 表示无法运行。脚本会报告解释器版本 3.10+、
两个命令是否安装、psycopg 3 能否导入，以及 Windows 事件循环注意事项。

**Windows**：psycopg 的异步模式需要 `asyncio.SelectorEventLoop`，CLI 会为自己的进程装上；
用别的方式驱动控制器（自写的 runner、IDE 里的测试）就得自己装：

```python
import asyncio, selectors
asyncio.set_event_loop(asyncio.SelectorEventLoop(selectors.SelectSelector()))
```

#### 作为服务运行

两个进程互相独立，可以分别守护，彼此都不依赖对方：

```ini
# /etc/systemd/system/tracker-web.service
[Unit]
Description=github-actions-tracker — REST 与 webhook
After=network-online.target

[Service]
EnvironmentFile=/etc/github-actions-tracker/env
ExecStart=/opt/github-actions-tracker/venv/bin/rhosocial-github-actions-tracker serve all
Restart=unless-stopped

[Install]
WantedBy=multi-user.target
```

MCP 监听沿用同一份 unit，将 `ExecStart` 改为 `serve mcp`。`EnvironmentFile` 是 `GHAT_*`
变量的存放位置，相当于原生部署中的 `docker/stack.env`，同样不应提交到仓库。

---

## 遗留安装

已经自行管理数据库 schema 与凭据的部署，例如旧库、外部迁移工具或手工维护的 PAT，不要执行初始化：

```dotenv
GHAT_INITDB=skip
```

`init` 不执行任何脚本，`web` 与 `mcp` 也不建表、不碰凭据。

遗留安装需要自行保证的步骤：

```bash
# 只建表；操作幂等，数据库不存在时会先创建
docker compose run --rm web rhosocial-github-actions-tracker-db init

# 录入凭据
docker compose exec web rhosocial-github-actions-tracker credential pat ensure \
  --name work --token <PAT> --probe-repo owner/repo
```

初始化和下面三个删除操作是两回事：`db init` 只做 `CREATE DATABASE`（库不存在时）和
`CREATE TABLE IF NOT EXISTS`；它们会删除数据，与“首次初始化”无关。

| 命令 | 影响 |
|---|---|
| `rhosocial-github-actions-tracker-db reset --yes` | 删除并重建全部 `ghat_*` 表，凭据行一起清空，库本身保留 |
| `rhosocial-github-actions-tracker-db drop --yes` | 删除整个数据库（拒绝删 `--maintenance-db`） |
| `docker compose ... down -v` | 一体化形态下删除数据卷，等于删库 |

---

## 镜像

```text
registry.cn-shanghai.aliyuncs.com/vistart_public/github-actions-tracker:latest        # 国内
docker.io/vistart/github-actions-tracker:latest                                       # 国外
```

镜像只提供一个标签 `latest`，指向 main 最近一次成功发布。若要确认“当前拉取的是哪一版”，
请查看镜像内部的 `build.revision`，`/readyz` 与 `ci_status` 都会报告该值。需要固定版本时，
把 `GHAT_IMAGE` 指向某个 registry 地址，再用 `build.revision` 核对。

`workflow_dispatch` 在 `main` 上是正常发布；在某个提交上运行是**刻意的重发**——这就是回滚的
做法（`gh workflow run images.yml --ref <旧提交>`）。在其它分支或 tag 上运行只构建不发布，
feature 代码不会成为生产要拉的东西。

- 多架构：`linux/amd64`、`linux/arm64`。
- 镜像里只有 Python 运行时、服务代码和入口脚本；**不含数据库、证书、任何代理配置、pip 镜像或索引覆盖**。
- 代理是**运行时**选择：全球用户不需要；中国大陆用户在自己的 compose / 环境里注入即可，见
  [运行时代理](operations.md#运行时代理仅中国大陆)。
- 自建：`docker buildx bake --load`（amd64 + arm64，本机只加载当前架构）；全平台用
  `docker buildx bake github-actions-tracker-all --push`。

### ARM64 与 ARM NAS

镜像是原生的 `linux/arm64` 构建，不是模拟出来的：同一个 manifest 里含两个架构，`docker pull`
会按宿主机自动挑，运行时没有转译开销。所以带容器管理器的 64 位 ARM NAS 完全可以当宿主机用——
NAS 本来就一直开着，而“CI 现在是什么状态”这类问题正适合由 NAS 来回答。

部署前先确认这台机器：

```bash
uname -m                                    # aarch64 → 用 arm64 镜像
docker version --format '{{.Server.Arch}}'
docker pull docker.io/vistart/github-actions-tracker:latest         # 自动取宿主架构
docker image inspect docker.io/vistart/github-actions-tracker:latest --format '{{.Architecture}}'
```

两家都有支持容器管理的 64 位 ARM 机型，**以下只是举例，不是兼容性清单**：Synology 在
DSM 7.2 上的 Container Manager（DS223、DS423 等 `aarch64` 机型；低端 `j` 系列通常不带容器
支持），QNAP 在 QTS 5 上的 Container Station（TS-233、TS-433 等 Cortex-A55 机型）。具体
机型能不能用取决于固件，以厂商应用中心为准：只要里面能装容器管理器，`uname -m` 又显示
`aarch64`，这个镜像就能跑。

在 NAS 上部署，有两点要注意：

- **优先确认内存，而不是 CPU。** 服务由两个 `serve` 进程和 PostgreSQL 组成。三者部署在同一台机器上时，建议预留至少 2 GB 内存。如果 NAS 同时担任数据库主机，可以调小 PostgreSQL 的 `shared_buffers`，或者让 `GHAT_DB_*` 指向其他位置的数据库；本服务不限制数据库的部署位置。
- **不要将日志卷与备份放在同一块机械盘上。** 日志是本服务写入量最大的数据。`db partitions --drop-before` 之所以快，是因为该命令删除整张表，属于一次元数据操作，而不是逐行删除；这需要底层文件系统支持。

## 端口与部署形态

| 端口 | 进程 | 说明 |
|---|---|---|
| `8000` | `serve all` / `serve rest` | REST API，明文 HTTP，默认只在容器网络内 |
| `18080` | `serve all` / `serve webhook` | GitHub webhook 接收端；可终止 TLS，GitHub 要求 https 回调 |
| `8001` | `serve mcp` | MCP Streamable HTTP，明文 HTTP，默认按需发布 |

`docker-compose.yml` 将服务拆分为两个常驻容器和一个一次性容器：

- `web`：`serve all`，REST 8000 + webhook 18080；只发布 18080。
- `mcp`：`serve mcp`，8001；只发布 8001。
- `init`：首次启动执行 `./docker/init.d` 下的运营脚本（建库建表、PAT 注入），成功后退出；
  `web`、`mcp` 等它成功后才启动。

两个常驻容器使用同一个镜像，仅 `command` 不同。也可以直接使用 `docker run` 单独启动其中一个，
但必须把 init.d 目录挂进去；否则首次启动会明确报错，而不是在未初始化状态下继续运行。

多个容器共用一个库时，只让其中一个承担初始化，其余设 `GHAT_INITDB=skip`。

## 全部环境变量

`GHAT_PUBLIC_BASE_URL` 是 MCP 返回的绝对 action URL 使用的外部地址，也是 webhook 回调 URL 的
默认前缀。

| 变量 | 默认 | 说明 |
|---|---|---|
| `GHAT_DB_HOST/PORT` | `localhost:5432`（compose 内为 `postgres:5432`） | PostgreSQL 坐标 |
| `GHAT_DB_USER/PASSWORD/NAME` | `root` / `password` / `test_db` | 数据库账号与库名 |
| `GHAT_DB_SSLMODE` | `prefer` | libpq sslmode |
| `GHAT_POOL_MIN/MAX` | `1` / `10` | 连接池。每个在处理的请求占一条连接，长轮询也算，见[连接池](operations.md#连接池一个请求占用一条连接) |
| `GHAT_API_KEY` | 空 | REST / MCP 的 Bearer；空表示不鉴权（启动告警），公网必须设置 |
| `GHAT_REST_HOST/PORT` | `0.0.0.0:8000` | REST 监听 |
| `GHAT_MCP_HOST/PORT` | `0.0.0.0:8001` | MCP 监听 |
| `GHAT_WEBHOOK_HOST/PORT` | `0.0.0.0:18080` | webhook 监听 |
| *(webhook secret)* | — | **不是环境变量。** 每个仓库在 `ghat_repo_webhook_secrets` 里有自己的 secret；用 `webhook rotate-secret --name owner/repo` 生成/轮换，该命令会同时推送到该仓库的 hook。没有 secret 的仓库会被以 `secret_not_configured` 拒绝 |
| `GHAT_WEBHOOK_URL` | 空 | 创建 webhook 时的默认回调 URL |
| `GHAT_PUBLIC_BASE_URL` | 空 | MCP action URL 的绝对前缀 |
| `GHAT_WEBHOOK_SSL_CERTFILE/KEYFILE` | 空 | webhook 的 fullchain 与私钥路径；空表示明文 HTTP |
| `GHAT_DEFAULT_EVENTS` | `workflow_run,workflow_job` | 订阅的 webhook 事件 |
| `GHAT_WAIT_MAX_SECONDS` | `300` | `wait_for_run` 单次可等待的上限（`timeout_seconds` 默认 45 秒，超过会被截断）。工具回报的 `p50/p90` 与 `suggested_timeout_seconds` 也受它约束 |
| `GHAT_LOG_CAPTURE` | `1` | 是否采集 step 日志。`0/false/no/off` 关闭后，所有日志读取都走整 job 的合并文本 |
| `GHAT_LOG_CAPTURE_WINDOW_MINUTES` | `60` | run 结束后多久之内仍然尝试采集 step 分片。该值是**实测结果**，不是官方承诺；调大不会产生负面影响，调小会减少采集范围 |
| `GHAT_LOG_LEVEL` | `INFO` | 日志级别；日志一律走 stdout/stderr |
| `GHAT_INITDB` | `auto` | 首次启动是否执行 init.d 脚本：`auto`（表不存在时执行）/ `force` / `skip` |
| `GHAT_INITDB_DIR` | `/docker-entrypoint-init.d` | init.d 脚本目录；compose 只读挂载 `./docker/init.d` |
| `GHAT_LEGACY` | `0` | `1` 等价于 `GHAT_INITDB=skip`（legacy 部署） |
| `GHAT_BOOTSTRAP_PAT` | 空 | 首次初始化注入的 fine-grained PAT（由 `20-pat.sh` 读取） |
| `GHAT_BOOTSTRAP_PAT_FILE` | 空 | 从文件读取 PAT，避免令牌出现在 `docker inspect` |
| `GHAT_BOOTSTRAP_PAT_NAME` | `default` | 凭据别名（CLI 中显示为 `pat/<name>`） |
| `GHAT_BOOTSTRAP_PROBE_REPO` | 空 | 用来验证 Actions 写权限的 `owner/repo` |
| `GHAT_IMAGE` | 发布镜像 | compose 使用的镜像；本地构建后设为 `github-actions-tracker:local` |
| `GHAT_WEBHOOK_PORT_HOST` | `18080` | 宿主发布的 webhook 端口（容器内固定 18080） |
| `GHAT_MCP_PORT_HOST` | `8001` | 宿主发布的 MCP 端口（容器内固定 8001） |
| `GHAT_OAUTH_ISSUER` | 空 | **预留**：OAuth 模式下的 issuer；当前不可用，见下方说明 |
| `GHAT_RESOURCE_URL` | 空 | 本服务对外发布的完整 URL，如 `https://tracker-mcp.example.com:8001/mcp`。**公网部署 MCP 时必须设置**：`GHAT_RESOURCE_URL` 既是 OAuth 模式下的 resource 标识，也是推导 Host 白名单（防 DNS rebinding）的唯一依据；不设置则只有 loopback 的 Host 能通过，公网请求会得到 `421 Invalid Host header` |
| `GHAT_MCP_ALLOWED_HOSTS` | 空 | 额外允许的 `Host`（逗号分隔，支持 `example.com:*` 通配端口）。默认由 `GHAT_RESOURCE_URL` 推导并附带 loopback |
| `GHAT_MCP_ALLOWED_ORIGINS` | 空 | 额外允许的 `Origin`（逗号分隔）。默认由 `GHAT_RESOURCE_URL` 推导；只在请求带 `Origin` 头时校验 |

## 凭据方式

本服务目前**只提供静态 Bearer 鉴权**（`GHAT_API_KEY`），MCP 与 REST 共用同一个 key，
CLI 不使用该 key。`GHAT_RESOURCE_URL` 在 OAuth 分支里也是 resource 标识，但 `GHAT_RESOURCE_URL` 现在
**已经是一个必需的环境变量**，不再是预留项；真正仍不可用的是 `GHAT_OAUTH_ISSUER`——本项目不
附带授权服务器，也没有可用的 JWKS 来源。只设置 `GHAT_OAUTH_ISSUER` 而没有 verifier 时，服务
仍会按 Bearer 模式启动。

## TLS 证书目录

镜像**不包含证书**，约定把证书目录挂到 `/certs`：

```bash
mkdir -p docker/certs
cp <证书目录>/fullchain.pem docker/certs/
cp <证书目录>/privkey.pem  docker/certs/
```

然后在环境里指向容器内路径：

```dotenv
GHAT_WEBHOOK_SSL_CERTFILE=/certs/fullchain.pem
GHAT_WEBHOOK_SSL_KEYFILE=/certs/privkey.pem
```

`docker-compose.yml` 已将 `./docker/certs` 只读挂载到 `/certs`。证书续期后，替换宿主机文件并重启
`web` 即可，命令为 `docker compose restart web`。如果 TLS 由前置反向代理终止，请把这两个变量显式设为
**空字符串**，`web` 将改为监听明文 HTTP。
