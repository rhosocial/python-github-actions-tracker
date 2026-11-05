# 部署

两种部署形态，外部 PostgreSQL 与一体化。

### 形态一：web + mcp 双容器（外部 PostgreSQL）

```bash
cp docker/stack.env.example docker/stack.env
# 编辑 docker/stack.env：webhook secret、API key、数据库、PAT
mkdir -p docker/certs && cp <fullchain.pem> <privkey.pem> docker/certs/

docker compose --env-file docker/stack.env up -d
```

启动顺序由 `depends_on` 保证：`init` 建库建表并处理 PAT → `web` 与 `mcp` 启动。数据库可以是
任意 PostgreSQL（本机 `localhost:5432`、compose 内的 `postgres`、或任何可达的服务），不要求和
应用同机。

### 形态二：一体化 `postgres:18`

在形态一的基础上叠加 `docker-compose.bundled.yml`：

```bash
docker compose -f docker-compose.yml -f docker-compose.bundled.yml \
  --env-file docker/stack.env up -d
```

这个 overlay 只增加官方 `postgres:18` 服务，并把 `init` / `web` / `mcp` 的
`GHAT_DB_HOST` 固定为 `postgres`，因此 `stack.env` 里残留的外部数据库地址不会误生效。
数据在命名卷 `pgdata`；`docker compose ... down` 保留数据，`down -v` 删除。

### 首次启动初始化（init.d）

数据表模式与 PAT 是**运营数据**，不是镜像里的代码逻辑。镜像只提供一个通用 runner；真正的
初始化脚本在 `docker/init.d/`，由 compose 只读挂载到 `/docker-entrypoint-init.d`，机制与 MySQL
镜像的 `/docker-entrypoint-initdb.d` 相同。

| 脚本 | 作用 |
|---|---|
| `docker/init.d/10-schema.sh` | `rhosocial-github-actions-tracker-db init`：库不存在则先建库，再建全部表（幂等） |
| `docker/init.d/20-pat.sh` | 配置了 PAT 时执行 `credential pat ensure`：不存在则新增，存在则原地轮换 |

“首次”由数据库状态判定，而不是容器文件系统：runner 先执行
`rhosocial-github-actions-tracker-db is-initialized`，全部 `ct_*` 表都在就跳过脚本，否则按文件名
顺序执行。容器重建、镜像升级都不会重复初始化；脚本任一失败则 `init` 失败，`web` / `mcp`
不会带着未初始化的库启动。

`20-pat.sh` 从环境读取（`docker/stack.env`）：

```dotenv
GHAT_BOOTSTRAP_PAT=github_pat_xxx
GHAT_BOOTSTRAP_PAT_NAME=work
GHAT_BOOTSTRAP_PROBE_REPO=owner/repo
```

`credential pat ensure` 的语义：

1. 数据库里没有 `pat/<name>` → **新增**；
2. 已存在 → **原地轮换**（保留 id 与所有仓库绑定，不需要重新订阅）；
3. 两条路径都先让 GitHub 验证令牌，验证失败则启动失败，不会把坏令牌写进数据库；
4. 设置了 `GHAT_BOOTSTRAP_PROBE_REPO` 时，额外探测该仓库的 Actions 写权限，
   结果决定 `can_write_actions`（触发 CI 的前提）。

令牌需要的权限：`Actions: Read and write`、`Metadata: Read`；需要自动创建 webhook 时再加
`Administration: Read and write`。

不想让令牌出现在 `docker inspect` 时，用文件形式：把令牌放进一个**目录**，把该目录挂进 `init` 容器，并设置 `GHAT_BOOTSTRAP_PAT_FILE`，例如
`GHAT_BOOTSTRAP_PAT_FILE=/run/secrets/github-actions-tracker-pat`。脚本和 CLI 都支持
`--token-file` / `--token-stdin`，令牌不会出现在进程参数里。

> **挂目录，不要挂单个文件。** 在 Docker Desktop（Hyper-V 后端）上，单文件 bind mount 会让容器在宿主机路径不可达时变成**无法删除**的状态：`docker rm -f`、`docker compose down`、`docker start` 全部阻塞，只能重启引擎。目录挂载没有这个失败模式。本仓库的本地覆盖文件就是按目录挂载的：

```yaml
volumes:
  - ./docker/secrets:/run/secrets:ro      # 目录，不是 ./docker/secrets/github-actions-tracker-pat
```

本仓库的部署同样走这条路径：`GHAT_BOOTSTRAP_PAT_NAME=work`、
`GHAT_BOOTSTRAP_PROBE_REPO=rhosocial/python-github-actions-tracker`，首次启动即自动新增。

#### 重跑初始化

首次启动之后脚本不会自动再跑。改过脚本，或首次启动时还没配 PAT、之后才补上令牌时：

```bash
docker compose run --rm -e GHAT_INITDB=force init
```

也可以往 `docker/init.d/` 里加自己的 `*.sh`（按文件名排序，任一失败即停止）。这些脚本是部署
数据，不会被烘进镜像。

手工方式（不使用 init.d 时）：

```bash
docker compose exec web rhosocial-github-actions-tracker credential pat add \
  --name work --token <PAT> --probe-repo owner/repo
docker compose exec web rhosocial-github-actions-tracker credential pat list
docker compose exec web rhosocial-github-actions-tracker credential pat show --name work --probe-repo owner/repo

## 部署前要定的四件事

技能 `ops-deploy-image` 会逐条提问并为每条跑一个校验脚本；这里是摘要。

### 1. PostgreSQL 从哪来

两种形态，第二种正是"扩展"问题要如实回答的地方：

- **自建**，同一 registry 家族里有现成镜像：
  `registry.cn-shanghai.aliyuncs.com/vistart_public/postgres-exts:18`
  （构建配方：<https://github.com/vistart/Dockerfiles/tree/postgres>），与
  `docker-compose.bundled.yml` 配对。
- **自己的或云上的**——自己拉的服务器，或 Supabase 一类托管。两种都行；差别只在于
  `pg_trgm` 能不能由你启用。日志表的三元组索引需要它，多数托管服务都有，但只有一次检查能证明。

无论哪种，服务都要求 PostgreSQL 16+、`pg_trgm`、`plpgsql`。托管环境不给你
`CREATE DATABASE` 也没关系：手工建好库，首次启动仍会建表。

### 2. 数据库连接是否过公网

`GHAT_DB_SSLMODE=prefer`（默认）在服务端提供时加密，但不证明服务端是谁。私网链路上
够了；跨公网——任何托管库、任何另一个机房——就该用 `verify-full` 加 CA 包，并且
`--host` 必须是证书签发给它的**名字**。IP 地址没法这样校验：如实说明，不要交付一个"看起来
验过了"的配置。

在真实托管实例上量到的三件事，动手前值得先知道：

- **供应商的 pooler 常用自己的 CA。** Supabase 的 pooler 出示的是
  `Supabase Intermediate 2021 CA` 签发的证书，因此 `verify-full` 配上任何公共信任库
  （含操作系统自带的）都验不过。选择只有两个：用供应商的 CA 包走 `verify-full`，或者用
  `require`（加密但不验证服务端）——而后者正是 `prefer` 悄悄退化到的结果。
- **Windows 上 libpq 的 `verify-full` 不指定 CA 文件时会去找
  `~/postgresql/root.crt`**，然后在一个没人创建过的文件上失败。想用系统信任库就写
  `sslrootcert=system`。
- **pooler 会终止 TLS。** 服务端可能报 `pg_stat_ssl.ssl = false`，而会话其实是加密的：
  pooler 在客户端侧终止了 TLS，再以明文连到数据库。此时 `verify-full` 验的是 *pooler 的*
  证书——这就是这个设置能证明的诚实边界。

### 3. webhook 容器的证书，以及将来调用它的令牌

GitHub 的 webhook 设置要求 **HTTPS** 回调地址。可以在仓库设置里改成 HTTP，而那样每一次投递
（含 secret 头）都是明文——所以：为公网名字挂一张证书，保持 HTTPS。服务在绑定前检查这一对，
**只警告不阻断**（见下）；自签证书对"只有 GitHub 能访问"的监听是可接受的，对"人会用浏览器
打开"的地址则不是。

GitHub 的 **fine-grained PAT 要操作者自己创建**，这里没有任何环节能代劳。创建入口
<https://github.com/settings/personal-access-tokens/new>（fine-grained），已有的令牌见
<https://github.com/settings/tokens?type=beta>。按要跟踪的每个仓库勾选：

| 权限 | 用途 |
|---|---|
| Actions: Read | run、job、日志 |
| Actions: Write | 触发、重跑、取消 |
| Contents: Read | 不传 `commit_sha` 时解析 ref 的 head |
| Administration: Read and write（repository hooks） | 仅当用 CLI 管理 webhook |
| Metadata: Read | 自动且必需 |

用文件注入（`GHAT_BOOTSTRAP_PAT_FILE`，只读挂载），令牌不进 `docker inspect`、不进进程
列表；然后用技能的 `check_pat.py` 验一遍：读权限是**实测**的，Actions *写* 保持**未验证**——
证明它要花 CI 分钟，一个会说谎的绿灯比一个诚实的空缺更糟。

### 4. MCP 监听开放给谁

- **本机或内网**：不需要证书、不需要 API key，`GHAT_API_KEY` 留空即可，监听会在启动时
  告警并正常服务。
- **公网或组织内共享**：设 `GHAT_API_KEY`——此后每次 MCP / REST 调用都要
  `Authorization: Bearer <key>`——并按 webhook 监听同样的方式给 8001 套上 TLS。一个开放的
  MCP 端点就是一个开放的 `trigger_workflow`：每个调用方花的都是**你的** CI 分钟。

## 证书检查

证书材料在镜像外：把一个目录挂到 `/certs`，`GHAT_WEBHOOK_SSL_CERTFILE` /
`_KEYFILE` 指向里面的两个文件。GitHub 要求 https 回调地址。

启动绑定之前（以及 `cert`）会把这一对读一遍并报告所见。**全部是建议性质**：
这里没有任何一项会阻止启动——一个能阻止启动的检查，价值低于它本该给出的原因。

| code | 含义 | 下一步 |
|---|---|---|
| `cert_unreadable` | 文件不存在、为空或读不了 | uvicorn 接下来也会绑定失败，先修路径 |
| `cert_not_a_certificate` | 既不是 PEM 也不是 DER——常见是把私钥或 CSR 放在了这个位置 | 指向证书 |
| `key_unreadable` / `key_not_a_key` | 私钥缺失或不可解析；加密私钥会**点名**说它加密了 | 在部署流程里解密 |
| `key_mismatch` | 私钥与证书的公钥不同 | 最常见的事故：换了证书没换私钥 |
| `cert_expired` / `cert_not_yet_valid` | 不在有效窗口内（`not_yet_valid` 会点名时钟） | 续期，或查机器时间 |
| `cert_expiring` | 距到期不足 `GHAT_WEBHOOK_SSL_EXPIRY_WARN_DAYS`（默认 30 天） | 按你的节奏续期 |
| `cert_hostname_mismatch` | 证书覆盖的名字不含配置的主机名 | 为主机签发，或设 `GHAT_WEBHOOK_SSL_HOSTNAME` |
| `cert_chain_unverified` | 无法连到受信根——措辞区分"自签"与"缺中间件" | 补全链，或用 `GHAT_WEBHOOK_SSL_CAFILE` |
| `cert_revoked` / `cert_revocation_unknown` | 响应者说已吊销 / 问不到 | 重签；`unknown` 从不阻断 |
| `cert_weak_algorithm` | RSA 不足 2048、ECC 不足 256，或 SHA-1 签名 | 续期；这条只是提示 |

域名校验需要一个主机名：先 `GHAT_WEBHOOK_SSL_HOSTNAME`，再
`GHAT_PUBLIC_BASE_URL` 的 host，最后 `GHAT_WEBHOOK_URL` 的 host。三者都无
就跳过校验，而不是猜一个。

吊销默认关闭——它是启动路径上唯一触网的检查。`GHAT_WEBHOOK_SSL_CRLFILE` 读
本地清单；`GHAT_WEBHOOK_SSL_CHECK_OCSP=1` 会问证书的响应者，3 秒上限、失败不冤枉。

```bash
rhosocial-github-actions-tracker cert             # 人读
rhosocial-github-actions-tracker cert --json      # 给轮换脚本
```

退出码 `0` 表示什么都没发现，`2` 表示发现了什么——供 cron 或值班轮换分支用，
与服务本身是否健康无关。

## 指标

REST 监听上的 `GET /metrics` 以 Prometheus 文本格式回答：只有计数器，在请求路径上
累加，不读数据库。

指标族与各自回答的问题：webhook 投递收到多少、存了多少，run 行写了多少，每一次出站
GitHub 调用按 operation 与 status code，每一次工具调用按 tool 与 outcome，以及各自的
耗时。通配路径（`/runs/{id}`）是标签形状而不是仓库名，所以序列始终可聚合。

关于形状的三点：

* **进程内计数，由抓取方求和。** 计数器每次启动归零，这正是它可以免费累加的原因；
  `rate()` 处理重启，`sum()` 跨实例就是部署级视图。
* **这里没有 gauge。** gauge 描述共享状态，而共享状态是数据库：`ci_status.storage`
  （大小、行数、分区）与 `ci_status.health`（连接池）为任何询问者而读。抓取不该为
  一次读取付钱。
* **探针与 `/metrics` 自身不计入。** 计数抓取方等于把监控变成负载，计数探针则会把
  重启循环读成流量。

```yaml
scrape_configs:
  - job_name: github-actions-tracker
    static_configs:
      - targets: ["tracker-web:8000"]
```
```
