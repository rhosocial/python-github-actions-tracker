# 部署

本项目支持两种部署形态：使用外部 PostgreSQL，或使用一体化数据库。

## 形态一：web 与 mcp 双容器（外部 PostgreSQL）

```bash
cp docker/stack.env.example docker/stack.env
# 编辑 docker/stack.env：webhook secret、API key、数据库与 PAT
mkdir -p docker/certs && cp <fullchain.pem> <privkey.pem> docker/certs/

docker compose --env-file docker/stack.env up -d
```

启动顺序由 `depends_on` 保证：`init` 先建库建表并处理 PAT，随后 `web` 与 `mcp` 启动。数据库可以是任意 PostgreSQL，
包括本机的 `localhost:5432`、compose 内的 `postgres`，或任何可达的数据库服务；本服务不要求数据库与应用部署在同一台机器上。

## 形态二：一体化镜像 `vistart/postgres-exts:18`

在形态一的基础上叠加 `docker-compose.bundled.yml`：

```bash
docker compose -f docker-compose.yml -f docker-compose.bundled.yml \
  --env-file docker/stack.env up -d
```

该 overlay 只增加一个名为 `vistart/postgres-exts:18` 的数据库服务。中国大陆用户可以使用阿里云镜像
`registry.cn-shanghai.aliyuncs.com/vistart_public/postgres-exts:18`，两者 digest 一致。该 overlay 同时把
`init`、`web` 与 `mcp` 的 `GHAT_DB_HOST` 固定为 `postgres`，因此 `stack.env` 中残留的外部数据库地址不会生效。
数据保存在命名卷 `pgdata` 中：`docker compose ... down` 保留数据，`down -v` 删除数据。

## 首次启动初始化（init.d）

数据表结构与 PAT 属于**运营数据**，不属于镜像内的代码逻辑。镜像只提供通用 runner；真正的初始化脚本位于
`docker/init.d/`，由 compose 只读挂载到 `/docker-entrypoint-init.d`。该机制与 MySQL 镜像的
`/docker-entrypoint-initdb.d` 相同。

| 脚本 | 作用 |
|---|---|
| `docker/init.d/10-schema.sh` | 执行 `rhosocial-github-actions-tracker-db init`：数据库不存在时先创建数据库，再创建全部表，操作是幂等的 |
| `docker/init.d/20-pat.sh` | 配置了 PAT 时执行 `credential pat ensure`：不存在则新增，存在则原地轮换 |

“首次”由数据库状态判定，而不是由容器文件系统判定。runner 先执行
`rhosocial-github-actions-tracker-db is-initialized`：如果全部 `ct_*` 表都存在，就跳过脚本；否则按文件名顺序执行。
容器重建与镜像升级都不会重复初始化。脚本任一失败都会导致 `init` 失败，`web` 与 `mcp` 不会在数据库未初始化时启动。

`20-pat.sh` 从环境变量读取配置，这些变量写在 `docker/stack.env`：

```dotenv
GHAT_BOOTSTRAP_PAT=github_pat_xxx
GHAT_BOOTSTRAP_PAT_NAME=work
GHAT_BOOTSTRAP_PROBE_REPO=owner/repo
```

`credential pat ensure` 的语义如下：

1. 数据库中不存在 `pat/<name>` 时，新增凭据。
2. 凭据已存在时，原地轮换；保留 id 与全部仓库绑定，不需要重新订阅。
3. 两条路径都会先让 GitHub 验证令牌；验证失败则启动失败，坏令牌不会写入数据库。
4. 设置了 `GHAT_BOOTSTRAP_PROBE_REPO` 时，额外探测该仓库的 Actions 写权限，结果写入 `can_write_actions`；该权限是触发 CI 的前提。

令牌需要的权限包括 `Actions: Read and write` 与 `Metadata: Read`；需要由 CLI 自动创建 webhook 时，
还需要 `Administration: Read and write`。

如果希望令牌不出现在 `docker inspect` 中，可以使用文件形式：把令牌放进一个目录，把该目录挂载到 `init` 容器，
并设置 `GHAT_BOOTSTRAP_PAT_FILE`，例如
`GHAT_BOOTSTRAP_PAT_FILE=/run/secrets/github-actions-tracker-pat`。脚本与 CLI 都支持
`--token-file` 与 `--token-stdin`，令牌不会出现在进程参数中。

> **挂载目录，不要挂载单个文件。** 在 Docker Desktop 的 Hyper-V 后端上，单文件 bind mount 在宿主机路径不可达时，
> 会让容器进入无法删除的状态：`docker rm -f`、`docker compose down` 与 `docker start` 全部阻塞，只能重启引擎。
> 目录挂载没有这个失败模式。本仓库的本地覆盖文件按目录挂载：

```yaml
volumes:
  - ./docker/secrets:/run/secrets:ro      # 挂载目录，而不是 ./docker/secrets/github-actions-tracker-pat
```

本仓库的部署也使用这条路径：`GHAT_BOOTSTRAP_PAT_NAME=work`、
`GHAT_BOOTSTRAP_PROBE_REPO=rhosocial/python-github-actions-tracker`，首次启动时会自动新增凭据。

### 重跑初始化

首次启动之后，初始化脚本不会自动再次运行。修改过脚本，或首次启动时尚未配置 PAT、之后才补充令牌时，可以执行：

```bash
docker compose run --rm -e GHAT_INITDB=force init
```

也可以向 `docker/init.d/` 添加自己的 `*.sh` 脚本；脚本按文件名排序执行，任一失败即停止。这些脚本属于部署数据，
不会被打包进镜像。

不使用 init.d 的手工方式：

```bash
docker compose exec web rhosocial-github-actions-tracker credential pat add \
  --name work --token <PAT> --probe-repo owner/repo
docker compose exec web rhosocial-github-actions-tracker credential pat list
docker compose exec web rhosocial-github-actions-tracker credential pat show --name work --probe-repo owner/repo
```

## 部署前需要确定的四件事

技能 `ops-deploy-image` 会逐条提问，并为每条运行一个校验脚本。本节是摘要。

### 1. PostgreSQL 的来源

有两种来源，其中第二种正是需要如实回答“扩展”问题的地方：

- **自行部署**：Docker Hub 上有现成镜像 `vistart/postgres-exts:18`，构建配方见
  <https://github.com/vistart/Dockerfiles/tree/postgres>。中国大陆用户可以使用阿里云镜像
  `registry.cn-shanghai.aliyuncs.com/vistart_public/postgres-exts:18`，两者是同一个镜像，digest 一致。
  该镜像与 `docker-compose.bundled.yml` 配对使用；compose 读取 `${GHAT_DB_IMAGE:-vistart/postgres-exts:18}`，
  因此更换 registry 只需要修改 `stack.env` 一行，不需要改动 compose 文件。
- **使用已有服务器或托管服务**：可以是自行维护的服务器，也可以是 Supabase 一类托管服务。两种都可用，差别在于
  `pg_trgm` 能否由部署者启用。日志表的三元组索引需要该扩展；多数托管服务已经提供，但只有实际检查才能确认。

无论采用哪种来源，服务都要求 PostgreSQL 16 及以上版本，并需要 `pg_trgm` 与 `plpgsql`。托管环境不授予
`CREATE DATABASE` 权限也没有关系：手工创建数据库后，首次启动仍会创建表。

### 2. 数据库连接是否经过公网

`GHAT_DB_SSLMODE=prefer` 是默认值。服务端提供 TLS 时会加密连接，但不验证服务端身份。该设置适用于私网链路。
如果连接跨公网，例如连接任何托管数据库或位于另一机房的数据库，应使用 `verify-full` 并配置 CA 包，同时
`--host` 必须是证书签发给它的**名字**。IP 地址无法进行这种校验，因此不能交付一个“看起来验证过”的配置。

在真实托管实例上测得三件事，部署前值得了解：

- **供应商的 pooler 通常使用自己的 CA。** Supabase 的 pooler 出示由
  `Supabase Intermediate 2021 CA` 签发的证书，因此 `verify-full` 配合任何公共信任库，包括操作系统自带的信任库，
  都无法通过验证。只有两个选择：使用供应商的 CA 包并保持 `verify-full`，或使用 `require`，即加密但不验证服务端身份。
  后者正是 `prefer` 实际退化到的结果。
- **在 Windows 上，libpq 的 `verify-full` 未指定 CA 文件时会查找
  `~/postgresql/root.crt`**，然后在一个从未创建的文件上失败。要使用系统信任库，请设置
  `sslrootcert=system`。
- **pooler 会终止 TLS。** 服务端可能报告 `pg_stat_ssl.ssl = false`，而客户端会话实际上是加密的：
  pooler 在客户端侧终止 TLS，再以明文连接数据库。此时 `verify-full` 验证的是 **pooler 的**证书，
  这是该设置能够证明的边界。

### 3. webhook 容器的证书与调用它的令牌

GitHub 的 webhook 设置要求 **HTTPS** 回调地址。可以在仓库设置中改为 HTTP，但这样每一次投递，包括携带 secret 的请求头，
都会以明文传输。因此应为公网主机名配置证书并保持 HTTPS。服务在绑定端口前会检查证书与私钥这一对，
**只发出警告，不阻止启动**，详见下文。自签证书适用于“只有 GitHub 访问”的监听；如果地址会由人用浏览器打开，则不适用。

GitHub 的 **fine-grained PAT 必须由操作者创建**，本项目的任何环节都无法代替。创建入口为
<https://github.com/settings/personal-access-tokens/new>（fine-grained）；已有令牌可在
<https://github.com/settings/tokens?type=beta> 查看。请按需要跟踪的每个仓库勾选权限：

| 权限 | 用途 |
|---|---|
| Actions: Read | 读取 run、job 与日志 |
| Actions: Write | 触发、重跑与取消 |
| Contents: Read | 未传入 `commit_sha` 时解析 ref 的 head |
| Administration: Read and write（repository hooks） | 仅在使用 CLI 管理 webhook 时需要 |
| Metadata: Read | 自动授予且必需 |

推荐通过文件注入令牌，并设置 `GHAT_BOOTSTRAP_PAT_FILE` 与只读挂载。这样令牌不会进入 `docker inspect`，
也不会出现在进程列表中。之后可以使用技能的 `check_pat.py` 验证令牌：读权限会被实际测试；Actions **写**权限保持
**未验证**，因为验证它需要消耗 CI 分钟。一个会错误显示为通过的检查，比一个诚实的空缺更糟。

### 4. MCP 监听的开放范围

- **本机或内网**：不需要证书，也不需要 API key，`GHAT_API_KEY` 可以留空。监听会在启动时发出告警，并正常提供服务。
- **公网或组织内共享**：需要设置 `GHAT_API_KEY`。此后每次 MCP 与 REST 调用都必须携带
  `Authorization: Bearer <key>`。同时应参照 webhook 监听的方式，为 8001 端口配置 TLS。开放的
  MCP 端点等于开放的 `trigger_workflow`，每个调用方消耗的都是**运营者的** CI 分钟。

## 证书检查

证书材料位于镜像之外：把一个目录挂载到 `/certs`，并让 `GHAT_WEBHOOK_SSL_CERTFILE` 与
`_KEYFILE` 指向其中的两个文件。GitHub 要求 HTTPS 回调地址。

三个监听各自终止 TLS，因此为 REST 与 MCP 配置域名证书后也能加密。REST 与 MCP 都携带 API key，其中 MCP 还能触发 CI，
明文传输无法接受。三对变量分别是 `GHAT_WEBHOOK_SSL_*`、`GHAT_REST_SSL_*` 与 `GHAT_MCP_SSL_*`。
常见情况下三者指向同一份证书文件，因为一张多域名证书可以覆盖部署发布的全部主机名。

`certfile` 必须提供**完整证书链**，叶子证书在前，中间证书随后。客户端不应自行补齐中间证书。

### 公网 MCP 主机名需要按名字放行

`GHAT_RESOURCE_URL` 是本 MCP 服务对外发布的 URL，参与两处推导：SDK 对 `Host` 与
`Origin` 的白名单，用于防止 DNS rebinding，未配置时只包含 loopback；以及配置 verifier 时的
OAuth `resource` 标识。未设置该变量时，公网 MCP 部署会对**每个**请求返回
`421 Invalid Host header`，而 `/readyz` 仍然正常，形成一条两边都不记账的故障。
反向代理、多入口等非常规形态可以使用 `GHAT_MCP_ALLOWED_HOSTS` 与
`GHAT_MCP_ALLOWED_ORIGINS` 覆盖推导结果。

### GitHub 不会向只有 IPv6 的主机名投递

这一结论来自实测，不是推断。把 hook 指向一个仅有 AAAA 记录的公网服务时，`failed to connect to host`
在 0.01 秒内返回，而同一地址对公网上的客户端能够应答。因此 webhook 主机名必须同时配置 **A 与 AAAA 两条记录**。
REST 与 MCP 的主机名如果不需要 IPv4，可以只保留 AAAA。该结论决定的是 DNS 配置，不是代码实现，
并且必须在登记第一个 hook 之前确定；一个无法投递的 hook 看起来就像服务发生故障。

启动绑定之前，以及执行 `cert` 命令时，服务会读取证书与私钥这一对并报告检查结果。**这些检查全部是建议性质**，
没有任何一项会阻止启动。一个会阻止启动的检查，其价值低于该检查本应提供的诊断信息。

| code | 含义 | 下一步 |
|---|---|---|
| `cert_unreadable` | 文件不存在、为空或无法读取 | uvicorn 随后也会绑定失败，请先修正路径 |
| `cert_not_a_certificate` | 既不是 PEM 也不是 DER，常见原因是把私钥或 CSR 放在了该位置 | 改为指向证书 |
| `key_unreadable` / `key_not_a_key` | 私钥缺失或无法解析；加密私钥会**明确指出**其已加密 | 在部署流程中解密 |
| `key_mismatch` | 私钥与证书的公钥不一致 | 最常见的事故是更换证书时没有更换私钥 |
| `cert_expired` / `cert_not_yet_valid` | 不在有效期内；`not_yet_valid` 会指出时钟问题 | 续期，或检查机器时间 |
| `cert_expiring` | 距到期时间不足 `GHAT_WEBHOOK_SSL_EXPIRY_WARN_DAYS`，默认 30 天 | 由运营者安排续期 |
| `cert_hostname_mismatch` | 证书覆盖的名字不包含配置的主机名 | 为主机签发证书，或设置 `GHAT_WEBHOOK_SSL_HOSTNAME` |
| `cert_chain_unverified` | 无法连接到受信任根证书；措辞会区分“自签”与“缺少中间证书” | 补全证书链。该检查使用文件中的中间证书建链，因此链顶是交叉签名根时，可以落到机器已信任的父证书上；无法等待新版本的部署可以使用 `GHAT_WEBHOOK_SSL_CAFILE` 作为临时方案，代价是把服务自己下发的根证书当作信任锚 |
| `cert_revoked` / `cert_revocation_unknown` | 响应者报告证书已吊销，或无法查询 | 重新签发；`unknown` 从不阻止启动 |
| `cert_weak_algorithm` | RSA 少于 2048 位、ECC 少于 256 位，或使用 SHA-1 签名 | 续期；该条仅作提示 |

域名校验需要一个主机名，优先级依次为 `GHAT_WEBHOOK_SSL_HOSTNAME`、
`GHAT_PUBLIC_BASE_URL` 的 host，以及 `GHAT_WEBHOOK_URL` 的 host。三者都未配置时跳过校验，而不是自行猜测。

吊销检查默认关闭，因为吊销检查是启动路径上唯一需要访问网络的检查。`GHAT_WEBHOOK_SSL_CRLFILE` 读取本地清单；
`GHAT_WEBHOOK_SSL_CHECK_OCSP=1` 会询问证书的响应者，查询上限为 3 秒，失败不会误判证书无效。

```bash
rhosocial-github-actions-tracker cert             # 人类可读输出
rhosocial-github-actions-tracker cert --json      # 供轮换脚本使用
```

退出码 `0` 表示没有发现问题，`2` 表示发现了问题，可供 cron 或值班轮换脚本分支使用；该退出码与服务本身是否健康无关。

## 指标

REST 监听上的 `GET /metrics` 以 Prometheus 文本格式返回指标。该端点只包含计数器，在请求路径上累加，不读取数据库。

指标族及其回答的问题包括：webhook 投递收到多少、存了多少，run 行写了多少，每次出站 GitHub 调用按 operation 与
status code 的计数，每次工具调用按 tool 与 outcome 的计数，以及各自的耗时。通配路径（`/runs/{id}`）作为标签形状，
而不是仓库名，因此序列始终可聚合。

指标形状有三点说明：

* **进程内计数，由抓取方求和。** 计数器在每次启动时归零，这也是它可以低成本累加的原因；
  `rate()` 负责处理重启，`sum()` 提供跨实例的部署级视图。
* **这里没有 gauge。** gauge 用于描述共享状态，而本服务的共享状态由数据库承载：`ci_status.storage`
  提供大小、行数与分区信息，`ci_status.health` 提供连接池信息，任何询问者都可以读取。抓取不应为一次数据库读取付出成本。
* **探针与 `/metrics` 自身不计入。** 统计抓取方等于把监控变成负载；统计探针则会把重启循环读成流量。

```yaml
scrape_configs:
  - job_name: github-actions-tracker
    static_configs:
      - targets: ["tracker-web:8000"]
```
