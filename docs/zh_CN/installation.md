# 安装

三种形态：本地 CLI、单容器、compose 双容器。

**不需要 Docker。** 本服务就是一个带两个命令行入口的 Python 包，原生安装即同一套服务、同样的
行为——差别只在首次初始化由谁触发、进程由谁守护。

```bash
pip install rhosocial-github-ci-tracker
```

### 原生部署：四条命令

```bash
# 1. 建库建表（有建库权限时会连库一起建好；托管服务加 --no-create-database）
rhosocial-github-ci-tracker-db init

# 2. 登记凭据，等价于容器里 20-pat.sh 做的事：
rhosocial-github-ci-tracker credential pat ensure \
  --name default --token-file /path/to/citracker-pat --probe-repo owner/repo

# 3. 两个常驻进程（各开一个终端，或交给服务管理器）：
rhosocial-github-ci-tracker serve all    # REST :8000 + webhook :18080
rhosocial-github-ci-tracker serve mcp    # MCP :8001
```

compose 文件 `environment:` 里那一串配置，在原生部署里全部来自 `CITRACKER_*` 环境变量，
两个命令都读：数据库坐标、API key、webhook secret、TLS 路径、PAT 名。除此之外不需要别的
东西——没有配置文件，没有 init 目录，也没有数据卷。`docker/stack.env.example` 是一份现成的
清单，一个容器都不跑也能照它逐项设环境变量。

容器替你做的两件事，原生部署要自己来：

| 容器里 | 原生部署 |
|---|---|
| `init` 先跑一次，`web` / `mcp` 等它成功才起 | 先跑 `db init`，再起两个进程 |
| `restart: unless-stopped` 自动拉起 | 自备进程守护：systemd、NSSM、任务计划，或你惯用的进程管理器 |

动手之前先验一下环境：

```bash
python .claude/skills/ops-deploy-image/scripts/check_native_env.py
```

它会报解释器版本（3.10 以上可用，CI 跑的是 3.15）、两个命令装没装上、psycopg 3 能不能导入，
以及下面这条 Windows 注意事项。退出码 `0` 表示就绪，`2` 表示可用但有问题要看，`1` 表示
跑不起来。

**Windows**：psycopg 的异步模式需要 `asyncio.SelectorEventLoop`，CLI 会为自己的进程装上；
用别的方式驱动控制器（自写的 runner、IDE 里的测试）就得自己装：

```python
import asyncio, selectors
asyncio.set_event_loop(asyncio.SelectorEventLoop(selectors.SelectSelector()))
```

### 作为服务运行

两个进程互相独立，分别守护即可，谁都不需要另一个。一个角色一个 unit，最清楚：

```ini
# /etc/systemd/system/citracker-web.service
[Unit]
Description=citracker — REST 与 webhook
After=network-online.target

[Service]
EnvironmentFile=/etc/citracker/env
ExecStart=/opt/citracker/venv/bin/rhosocial-github-ci-tracker serve all
Restart=unless-stopped

[Install]
WantedBy=multi-user.target
```

MCP 监听照抄一份，`ExecStart` 换成 `serve mcp`。`EnvironmentFile` 就是 `CITRACKER_*` 的
落脚处——相当于原生的 `docker/stack.env`，同样属于"不入库"那一类。

最短路径（外部已有 PostgreSQL，证书就绪）：

```bash
cp docker/stack.env.example docker/stack.env   # 填 DB、API key、webhook secret、PAT
mkdir -p docker/certs && cp <fullchain.pem> <privkey.pem> docker/certs/
docker compose --env-file docker/stack.env up -d
curl -k https://127.0.0.1:18080/readyz        # {"status":"ready",...}
```

下面按"镜像 → 端口与变量 → 部署形态 → 初始化 → 升级 → 探针与连接池 → 排障"展开。

### 镜像

```text
registry.cn-shanghai.aliyuncs.com/vistart_public/citracker:latest        # 国内
docker.io/vistart/citracker:latest                                       # 国外
```

只有一个标签：`latest`。它指向 main 最近一次成功发布，因此"拉到的是哪一版"由镜像内部的
`build.revision` 回答（`/readyz`、`ci_status` 都会报），固定版本部署则是把 `CITRACKER_IMAGE`
指向某个 registry 地址，再用 `build.revision` 核对。

`workflow_dispatch` 在 `main` 上是正常发布；在某个提交上运行是**刻意的重发**——这就是回滚的
做法（`gh workflow run images.yml --ref <旧提交>`）。在其它分支或 tag 上运行只构建不发布，
feature 代码不会变成生产要拉的东西。

- 多架构：`linux/amd64`、`linux/arm64`。
- 镜像里只有 Python 运行时、服务代码和入口脚本；**不含数据库、证书、任何代理配置、pip 镜像或索引覆盖**。
  构建由 `images.yml` 在 GitHub runner 上用官方 PyPI 直接完成，`HTTP(S)_PROXY`、`PIP_INDEX_URL`、
  `mirrors.aliyun.com` 等都不会进入镜像。新增构建步骤时由 `tests/app/test_image_hygiene.py`
  和 `images.yml` 的守卫步骤拦截。
- 代理是**运行时**选择：全球用户不需要；中国大陆用户在自己的 compose / 环境里注入即可，见
  [运行时代理](operations.md#运行时代理仅中国大陆)。
- 自建：`docker buildx bake --load`（amd64 + arm64，本机只加载当前架构）；全平台用
  `docker buildx bake citracker-all --push`。

### ARM64 与 ARM NAS

镜像是原生的 `linux/arm64` 构建，不是模拟出来的：同一个 manifest 里含两个架构，`docker pull`
会按宿主机自动挑，运行时没有转译开销。所以带容器管理器的 64 位 ARM NAS 完全可以当宿主机用——
NAS 本来就一直开着，而“CI 现在什么状态”这类问题正适合由它来回答。

部署前先确认这台机器：

```bash
uname -m                                    # aarch64 → 用 arm64 镜像
docker version --format '{{.Server.Arch}}'
docker pull docker.io/vistart/citracker:latest         # 自动取宿主架构
docker image inspect docker.io/vistart/citracker:latest --format '{{.Architecture}}'
```

两家都有支持容器管理的 64 位 ARM 机型，**以下只是举例，不是兼容性清单**：Synology 在
DSM 7.2 上的 Container Manager（DS223、DS423 等 `aarch64` 机型；低端 `j` 系列通常不带容器
支持），QNAP 在 QTS 5 上的 Container Station（TS-233、TS-433 等 Cortex-A55 机型）。具体
机型能不能用取决于固件，以厂商应用中心为准：只要里面能装容器管理器，`uname -m` 又显示
`aarch64`，这个镜像就能跑。

在 NAS 上部署，有两点要注意：

- **先看内存，别急着看 CPU。** 服务是两个 `serve` 进程加 PostgreSQL，都放在同一台机器上，
  建议至少留 2 GB。NAS 兼做数据库的话，要么把 PostgreSQL 的 `shared_buffers` 调小，要么
  让 `CITRACKER_DB_*` 指向别处的数据库——服务不挑数据库在哪。
- **日志卷不要和备份挤在同一块机械盘上。** 日志是这个服务写入量最大的部分；
  `db partitions --drop-before` 之所以快，是因为删的是整张表（一次元数据操作），而不是
  逐行删除，这需要底层文件系统配合。

### 端口与启动参数

| 端口 | 进程 | 说明 |
|---|---|---|
| `8000` | `serve all` / `serve rest` | REST API，明文 HTTP，默认只在容器网络内 |
| `18080` | `serve all` / `serve webhook` | GitHub webhook 接收端；可终止 TLS，GitHub 要求 https 回调 |
| `8001` | `serve mcp` | MCP Streamable HTTP，明文 HTTP，默认按需发布 |

`docker-compose.yml` 把服务拆成两个常驻容器和一个一次性容器：

- `web`：`serve all`，REST 8000 + webhook 18080；只发布 18080。
- `mcp`：`serve mcp`，8001；只发布 8001。
- `init`：首次启动执行 `./docker/init.d` 下的运营脚本（建库建表、PAT 注入），成功后退出；
  `web`、`mcp` 等它成功后才启动。

两个常驻容器使用同一个镜像，只是 `command` 不同。也可以直接 `docker run` 单独启动其中一个，
但要把 init.d 目录挂进去（否则首次启动会明确报错，而不是带病启动）：

```bash
docker run -d --name citracker-web --restart unless-stopped \
  -p 18080:18080 \
  -v /path/to/certs:/certs:ro \
  -v "$PWD/docker/init.d:/docker-entrypoint-init.d:ro" \
  -e CITRACKER_DB_HOST=<db-host> -e CITRACKER_DB_PORT=5432 \
  -e CITRACKER_DB_USER=<user> -e CITRACKER_DB_PASSWORD=<password> \
  -e CITRACKER_DB_NAME=citracker \
  -e CITRACKER_API_KEY=<bearer> \
  -e CITRACKER_WEBHOOK_SECRET=<github-webhook-secret> \
  -e CITRACKER_BOOTSTRAP_PAT=<fine-grained-pat> \
  -e CITRACKER_WEBHOOK_SSL_CERTFILE=/certs/fullchain.pem \
  -e CITRACKER_WEBHOOK_SSL_KEYFILE=/certs/privkey.pem \
  registry.cn-shanghai.aliyuncs.com/vistart_public/citracker:latest   # 国内
  # docker.io/vistart/citracker:latest                                 # 国外，二选一
```

首次启动时容器执行 `10-schema.sh` 与 `20-pat.sh`，随后开始服务；再次启动检测到表已存在就跳过。
多个容器共用一个库时，只让其中一个承担初始化（其余设 `CITRACKER_INITDB=skip`），或按 compose
的方式先运行一次性的 `init` 服务。

**初始化不会清空已有数据。** `10-schema.sh` 只做 `CREATE DATABASE`（库不存在时）与
`CREATE TABLE IF NOT EXISTS`，对已存在的库和表不写入、不删除，所以对生产库重跑 `force init`
是安全的。会丢数据的是另外两个命令，它们与"首次初始化"无关：

| 命令 | 影响 |
|---|---|
| `rhosocial-github-ci-tracker-db reset --yes` | 删除并重建全部 `ct_*` 表，凭据行一起清空，库本身保留 |
| `rhosocial-github-ci-tracker-db drop --yes` | 删除整个数据库（拒绝删 `--maintenance-db`） |
| `docker compose ... down -v` | 一体化形态下删除数据卷，等于删库 |

只有在上表三条之后，才需要重新跑一次 `force init` 恢复 schema 与凭据。

`CITRACKER_PUBLIC_BASE_URL` 是 MCP 返回的绝对 action URL 使用的外部地址，也是 webhook 回调 URL 的
默认前缀。全部环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `CITRACKER_DB_HOST/PORT` | `localhost:5432`（compose 内为 `postgres:5432`） | PostgreSQL 坐标 |
| `CITRACKER_DB_USER/PASSWORD/NAME` | `root` / `password` / `test_db` | 数据库账号与库名 |
| `CITRACKER_DB_SSLMODE` | `prefer` | libpq sslmode |
| `CITRACKER_POOL_MIN/MAX` | `1` / `10` | 连接池。每个在处理的请求占一条连接，长轮询也算，见[连接池](operations.md#连接池一个请求占一条连接) |
| `CITRACKER_API_KEY` | 空 | REST / MCP 的 Bearer；空表示不鉴权（启动告警），公网必须设置 |
| `CITRACKER_REST_HOST/PORT` | `0.0.0.0:8000` | REST 监听 |
| `CITRACKER_MCP_HOST/PORT` | `0.0.0.0:8001` | MCP 监听 |
| `CITRACKER_WEBHOOK_HOST/PORT` | `0.0.0.0:18080` | webhook 监听 |
| `CITRACKER_WEBHOOK_SECRET` | 空 | HMAC 密钥，与 GitHub webhook 配置一致 |
| `CITRACKER_WEBHOOK_URL` | 空 | 创建 webhook 时的默认回调 URL |
| `CITRACKER_PUBLIC_BASE_URL` | 空 | MCP action URL 的绝对前缀 |
| `CITRACKER_WEBHOOK_SSL_CERTFILE/KEYFILE` | 空 | webhook 的 fullchain 与私钥路径；空表示明文 HTTP |
| `CITRACKER_DEFAULT_EVENTS` | `workflow_run,workflow_job` | 订阅的 webhook 事件 |
| `CITRACKER_WAIT_MAX_SECONDS` | `300` | `wait_for_run` 单次可等待的上限（`timeout_seconds` 默认 45 秒，超过会被截断）。工具回报的 `p50/p90` 与 `suggested_timeout_seconds` 也受它约束 |
| `CITRACKER_LOG_CAPTURE` | `1` | 是否采集 step 日志。`0/false/no/off` 关闭后，所有日志读取都走整 job 的合并文本（引入 step 日志之前的行为） |
| `CITRACKER_LOG_CAPTURE_WINDOW_MINUTES` | `60` | run 结束后多久之内仍尝试采集 step 分片。这是**实测值**，不是官方承诺；调大没有坏处，调小会少抓 |
| `CITRACKER_LOG_LEVEL` | `INFO` | 日志级别；日志一律走 stdout/stderr |
| `CITRACKER_INITDB` | `auto` | 首次启动是否执行 init.d 脚本：`auto`（表不存在时执行）/ `force` / `skip` |
| `CITRACKER_INITDB_DIR` | `/docker-entrypoint-init.d` | init.d 脚本目录；compose 只读挂载 `./docker/init.d` |
| `CITRACKER_LEGACY` | `0` | `1` 等价于 `CITRACKER_INITDB=skip`（legacy 部署） |
| `CITRACKER_BOOTSTRAP_PAT` | 空 | 首次初始化注入的 fine-grained PAT（由 `20-pat.sh` 读取） |
| `CITRACKER_BOOTSTRAP_PAT_FILE` | 空 | 从文件读取 PAT，避免令牌出现在 `docker inspect` |
| `CITRACKER_BOOTSTRAP_PAT_NAME` | `default` | 凭据别名（CLI 中显示为 `pat/<name>`） |
| `CITRACKER_BOOTSTRAP_PROBE_REPO` | 空 | 用来验证 Actions 写权限的 `owner/repo` |
| `CITRACKER_IMAGE` | 发布镜像 | compose 使用的镜像；本地构建后设为 `citracker:local` |
| `CITRACKER_WEBHOOK_PORT_HOST` | `18080` | 宿主发布的 webhook 端口（容器内固定 18080） |
| `CITRACKER_MCP_PORT_HOST` | `8001` | 宿主发布的 MCP 端口（容器内固定 8001） |
| `CITRACKER_OAUTH_ISSUER` | 空 | **预留**：OAuth 模式下的 issuer；当前不可用，见下方说明 |
| `CITRACKER_RESOURCE_URL` | 空 | **预留**：OAuth 模式下本服务的 resource 标识；当前不可用 |

#### 凭据方式：当前只有静态 Bearer

本服务目前**只提供静态 Bearer 鉴权**（`CITRACKER_API_KEY`），MCP 与 REST 共用同一个 key，
CLI 不使用它。代码里保留了 OAuth 2.1 的分支（`CITRACKER_OAUTH_ISSUER`、`CITRACKER_RESOURCE_URL`），
但本项目不附带授权服务器，也没有可用的 JWKS 来源，因此**不要把这两个变量当作可用功能**：
只设置 `CITRACKER_OAUTH_ISSUER` 而没有 verifier 时，服务仍会按 Bearer 模式启动。等中心授权
服务器落地后，这一节会改写。

### TLS 证书目录

镜像**不包含证书**，约定把证书目录挂到 `/certs`：

```bash
mkdir -p docker/certs
cp <证书目录>/fullchain.pem docker/certs/
cp <证书目录>/privkey.pem  docker/certs/
```

然后在环境里指向容器内路径：

```dotenv
CITRACKER_WEBHOOK_SSL_CERTFILE=/certs/fullchain.pem
CITRACKER_WEBHOOK_SSL_KEYFILE=/certs/privkey.pem
```

`docker-compose.yml` 已把 `./docker/certs` 只读挂载到 `/certs`。证书续期后替换宿主机文件并重启
`web` 即可（`docker compose restart web`）。如果 TLS 由前置反向代理终止，把这两个变量显式设为
**空字符串**，`web` 就监听明文 HTTP。
