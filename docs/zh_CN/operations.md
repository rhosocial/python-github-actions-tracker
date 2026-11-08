# 运维

本页说明停机与重启、探针、连接池、凭据、清理与验证。

## 停机与重启

四个操作的效果不同，区别在于容器是否被重建，以及数据卷是否被删除：

| 操作 | 容器 | init 是否再次运行 | 数据 |
|---|---|---|---|
| `docker compose stop` / `start` | 保留，仅停止或启动进程 | 运行一次；`auto` 检测到表已存在后立即跳过 | 不变 |
| `docker compose restart web` | 保留，重启进程 | 同上；`web` 设置了 `GHAT_INITDB=skip`，不会运行 | 不变 |
| `docker compose down` → `up -d` | 删除并重建 | 运行，仍然跳过 | 外部数据库不变；一体化形态的数据卷保留 |
| `docker compose down -v` | 删除 | 运行；此时表已不存在，因此**真正执行建表** | 一体化形态的数据卷被删除，数据丢失 |

需要了解以下几点：

- `restart: unless-stopped` 写在 `x-app` 上，因此宿主机重启后 `web` 与 `mcp` 会自动恢复。
  `init` 使用 `restart: "no"`，`init` 只在每次 `up` 时运行一次然后退出；`Exited (0)` 是正常状态，不是失败。
- 停机期间的服务不会自行恢复数据。webhook 在停机期间的投递会失败，GitHub 会记录一次失败投递。
  服务恢复后直接调用 `find_runs` 即可补齐停机期间漏掉的 run，因为该工具每次都读取 GitHub，这条路径不依赖 webhook。
- 升级镜像的步骤如下：

  ```bash
  docker compose --env-file docker/stack.env pull
  docker compose --env-file docker/stack.env up -d
  docker compose --env-file docker/stack.env ps      # web 与 mcp 应为 healthy
  curl -k https://127.0.0.1:18080/readyz             # {"status":"ready",...}
  ```

  拉取新镜像后执行 `up` 会重建容器；`init` 运行一次并跳过，正在提供服务的数据库不受影响。

- **表结构发生变化后，需要重启 `web` 与 `mcp`。** 在 PostgreSQL 上，连接池使用服务端预处理语句。
  另一个进程（`init`、`db init`、`db reset`）修改表结构后，已有连接中缓存的执行计划会以
  `cached plan must not change result type` 失败，直到这些连接被替换。执行 `up -d` 重建容器不会出现该问题，
  因为连接是新的。如果只运行 `init` 而没有重建应用容器，需要显式执行
  `docker compose restart web mcp`。已经失败的投递不会丢失：`ci_status.health.alerts` 的
  `runs` 会列出受影响的 run id，对每个 id 调用一次 `get_run` 即可从 GitHub 补齐。
- **哪些改动需要重建镜像**，与普通的“修改代码”不同：

  | 改动位置 | 镜像是否自动重建 | 需要执行的操作 |
  |---|---|---|
  | 本仓库的 `src/**`、`pyproject.toml`、`Dockerfile`、`docker/**` | **否**，`images.yml` 只能手动触发 | 测试通过后手动构建，再执行 `pull` 与 `up -d` |
  | 仅修改 `tests/**`、`AGENTS.md`、`README.md` | 否 | 无需操作，线上行为不变 |
  | **兄弟库**（`python-activerecord`、`python-activerecord-postgres`、`python-mcpserver`） | **否** | 手动触发一次镜像构建 |

  **镜像始终需要手动发布。** 推送到 main 只运行测试（`test.yml`），不会同时发布镜像。发布一次多架构镜像是有意为之的动作，
  不是合并的副作用。需要新镜像时执行：

  ```bash
  gh workflow run images.yml --ref main
  gh run watch   # 或等待通知；multi-arch 构建通常需要 7–8 分钟
  docker compose --env-file docker/stack.env pull && docker compose --env-file docker/stack.env up -d
  ```

  #### 两个 registry，一次构建，一个 tag

  `images.yml` 把**同一份**多架构构建推送到两个目的地，tag 都只有 `latest`。两处发布共享一份 manifest，
  因此两处的 `latest` 是同一串字节：

  | 目的地 | 镜像 | secret（Settings → Secrets and variables → Actions） |
  |---|---|---|
  | 阿里云 | `registry.cn-shanghai.aliyuncs.com/vistart_public/github-actions-tracker` | `ALIYUN_REGISTRY_USERNAME` / `ALIYUN_REGISTRY_PASSWORD` |
  | Docker Hub | `docker.io/vistart/github-actions-tracker` | `DOCKERHUB_REGISTRY_USERNAME` / `DOCKERHUB_REGISTRY_PASSWORD` |

  两个目的地各自独立：缺少一对 secret 只会跳过对应的目的地，run summary 会写明跳过的是哪一个，另一个照常发布。
  两对 secret 都缺少时仍然会构建；构建失败是真实失败，此时只是不推送。在分支或 tag 上运行**不会推送任何内容**，
  只证明构建能够通过，功能分支的代码不会成为生产环境拉取的版本。在某个提交上执行 `workflow_dispatch`
  是有效的发布方式，这也是回滚的做法（`gh workflow run images.yml --ref <旧提交>`）。从那一刻起，
  该提交的 `latest` 就是生产环境拉取的版本。

  “线上运行的是哪一版”不再由 tag 回答，而由 `build.revision` 回答。`/readyz` 与 `ci_status` 会从容器内部报告该值，
  构建时由工作流写入镜像。固定版本部署仍然可行：把 `GHAT_IMAGE` 指向某个 registry 地址，再用 `build.revision`
  确认实际拉取的版本。

  兄弟库的改动容易被遗漏：镜像在**构建时**通过
  `pip install git+https://github.com/rhosocial/python-activerecord.git@<ref>` 固化依赖，运行时不重新获取。
  因此兄弟库合并后，也需要手动重建一次镜像。仅在本地验证时不必经过 registry，可以执行
  `GHAT_IMAGE=github-actions-tracker:local docker compose build`，然后执行
  `GHAT_IMAGE=github-actions-tracker:local docker compose up -d`。
- 证书续期只影响 `web`：替换 `docker/certs/` 下的文件后，执行 `docker compose restart web`。

## 探针：`/livez` 与 `/readyz`

两个探针回答两个不同问题，**不要混用**：

| 路径 | 回答的问题 | 检查 | 失败时的操作 |
|---|---|---|---|
| `/livez` | 进程是否存活、事件循环是否还能响应 | **无 I/O** | 重启容器 |
| `/readyz` | 当前实例能否接收请求 | 一次数据库往返，执行 `SELECT 1`，上限 2 秒 | 从负载均衡中摘除，**不重启** |

`/health` 与 `/healthz` 是 `/readyz` 的别名，供旧部署与外部监控使用。四个路径都不要求 API key，
也不占用每请求的连接作用域。

- `docker-compose.yml` 的 healthcheck 请求 `/readyz`，两个常驻容器各有一份配置；
- 数据库不可达时返回 `503 {"status":"not_ready","checks":{...}}`；数据库恢复后会自动回到 200，不需要重启容器；
- 缺少表不参与就绪判定：`checks.schema` 会写出 `missing: ...`，该检查在启动时执行一次；建表是 `init` 的职责；
- `ci_status.health` 面向人和 agent，提供投递流、回填速率与写入失败等运维信息，**不是编排器探针**。

### 当前进程对应哪个构建：`build` 块

`version` 是源码中的常量，同一份代码的每个镜像都会返回相同的值，因此**无法判断“线上是否是我刚推送的构建”**。
镜像在构建时写入自身标识，`/livez`、`/readyz` 与 `ci_status.service` 都会返回：

```jsonc
{
  "status": "ready",
  "version": "1.0.0.dev1",              // 源码常量，不区分构建
  "build": {
    "revision": "e5815a5f5b31df495dc69638d11602df685a0e4b",  // 完整提交号
    "short_revision": "e5815a5",                            // git log --oneline 中的短提交号
    "number": "57",                                        // 发布工作流的构建号
    "built_at": "2026-10-31T02:27:51Z"                     // UTC
  },
  "checks": {"container": "ok", "database": "ok", "schema": "ok"}
}
```

- 这些值来自镜像环境变量 `GHAT_BUILD_REVISION`、`GHAT_BUILD_NUMBER` 与 `GHAT_BUILD_BUILT_AT`，
  由 `images.yml` 在构建时传入；
- 本地执行 `pip install -e .` 或自建镜像没有这些参数时，四个字段都是 `null`。**`null` 表示镜像没有构建标识，
  不是“旧版本”**；
- 镜像同时带有 `org.opencontainers.image.revision` 标签，`docker inspect` 也可以查看；
- 部署后确认镜像版本：执行 `curl -k https://127.0.0.1:18080/readyz | jq .build`，与
  `git rev-parse HEAD` 对比。如果不一致，说明 `latest` 没有更新，或容器没有重建。

## 连接池：一个请求占用一条连接

连接作用域是**每请求**的，REST 中间件与 MCP 包装层各创建一个，因此：

- 每个正在处理的请求占用 `GHAT_POOL_MAX` 中的一个名额，默认值为 10；
- **长轮询同样占用连接**：`wait_for_run` 在等待期间持续占用自己的连接；300 秒的等待就是 300 秒的占用；
- `GHAT_POOL_MAX` 必须覆盖“并发长轮询 + 并发普通请求”。配置不足时，后续请求会在 `acquire` 上排队，
  30 秒后返回 `Failed to acquire connection within 30.0 seconds`。

该错误的判读方式：

| `Pool stats` | 含义 | 处理 |
|---|---|---|
| `in_use` 接近 `max_size` | **饱和**：并发确实超过了连接池容量 | 调大 `GHAT_POOL_MAX`，或减少并发长轮询 |
| `available=0, in_use=0` | **连接许可泄漏** | 重启容器；这是 1.0.0.dev1 之前版本的缺陷，`python-activerecord` 的 `a55ebbd9` 已修复 |

## 更新或替换 PAT

令牌有两种存放形式，更新步骤不同，但都以 `force init` 收尾。

**文件形式（推荐，令牌不会进入 `docker inspect`）**

```bash
# 1. 修改宿主机上的令牌文件；该文件由 compose 只读挂载到
#    /run/secrets/github-actions-tracker-pat，见本机 docker-compose.override.yml。
#    文件中只放一行令牌，不要添加多余换行。
# 2. 重跑初始化，让 20-pat.sh 重新执行 ensure。
docker compose --env-file docker/stack.env run --rm -e GHAT_INITDB=force init
```

**环境变量形式**

```bash
# 1. 修改 docker/stack.env 中的 GHAT_BOOTSTRAP_PAT。
# 2. 重跑初始化。
docker compose --env-file docker/stack.env run --rm -e GHAT_INITDB=force init
```

两种方式最终都执行 `credential pat ensure`，因此：

- 同名凭据会**原地轮换**：id 不变，仓库绑定全部保留，**不需要重新订阅**；
- 更换 `GHAT_BOOTSTRAP_PAT_NAME` 会**新增**一条凭据，旧凭据及其绑定不受影响。若要让新令牌接管仓库，
  可以沿用同名，或者执行 `repo bind --name owner/repo --credential pat/<新名>` 重新绑定仓库。

验证轮换结果：

```bash
docker compose exec web rhosocial-github-actions-tracker credential pat list
docker compose exec web rhosocial-github-actions-tracker credential pat show \
  --name work --probe-repo owner/repo
```

`can_write_actions` 为 `true` 才表示触发 CI 的能力已经过验证。`--probe-repo` 是这项探测的目标仓库；
不提供该参数时，能力保持未知状态。

### 手工配置 PAT 时可能遇到的问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `init` 退出码非 0，`web` 与 `mcp` 不启动 | GitHub 拒绝了令牌，例如拼写错误、已吊销或已过期 | 更换令牌后重跑 `force init`。坏令牌不会写入数据库 |
| 启动成功，但 `ci_status` 的 `can_trigger` 为 `false` | 令牌缺少 `Actions: Read and write`，或未提供 `--probe-repo` 导致能力未知 | 补充权限，或补充 `GHAT_BOOTSTRAP_PROBE_REPO` 后重跑 |
| 工具返回 `credential_expired` / `credential_invalid` | 运行期间令牌过期或被吊销；服务不会因为令牌问题停机 | 按 `action_url` 打开设置页，或按上一节轮换令牌；不要用同一令牌重试 |
| 工具返回 `credential_insufficient` | 令牌缺少该操作需要的权限 | 在 GitHub 上补充权限并重新录入；不要通过更换凭据来规避 |
| `--probe-repo` 指向不存在的仓库 | 探测目标填写错误 | 改为真实仓库；这不是致命错误，但 `can_write_actions` 会保持未知 |
| 令牌出现在 `docker inspect` 中 | 使用了 `GHAT_BOOTSTRAP_PAT`，而不是 `GHAT_BOOTSTRAP_PAT_FILE` | 改用文件形式；已经泄露的令牌应立即在 GitHub 上吊销 |
| 令牌出现在 shell 历史或进程列表中 | 手工执行了 `credential pat add --token <明文>` | 使用 `--token-file` 或 `--token-stdin`，清除历史记录并吊销旧令牌 |

> 令牌权限最小集为 `Actions: Read and write` 与 `Metadata: Read`；需要本服务代为创建 webhook 时，
> 再增加 `Administration: Read and write`。`Contents` 只在需要解析工作流文件时使用。

## legacy / 手工模式

已经自行管理 schema 与凭据的部署，例如旧数据库、外部迁移工具或手工维护的 PAT，可以使用：

```dotenv
GHAT_INITDB=skip
```

此时 `init` 不执行任何脚本，`web` 与 `mcp` 也不建表、不处理 PAT。`GHAT_LEGACY=1` 是等价写法。

legacy 模式下，部署者需要自行保证以下事项：

```bash
# 只建表；操作幂等，数据库不存在时会先创建
docker compose run --rm web rhosocial-github-actions-tracker-db init

# 录入凭据
docker compose exec web rhosocial-github-actions-tracker credential pat ensure \
  --name work --token <PAT> --probe-repo owner/repo
```

`credential pat ensure` 同样是幂等操作：不存在则新增，存在则轮换，适合放入自己的 init 脚本。

## 清理数据库

```bash
# 检查是否已初始化；0 表示是，1 表示否，适合例行检查
docker compose exec web rhosocial-github-actions-tracker-db is-initialized

# 统计各表行数
docker compose exec web rhosocial-github-actions-tracker-db stats

# 数据库占用：总大小、每表体积与行数、最近一天新增 run、pg_trgm 状态
docker compose exec web rhosocial-github-actions-tracker-db usage
docker compose exec web rhosocial-github-actions-tracker-db usage --json

# 按时间清理历史，例如保留最近 90 天的 run 与 365 天的日志
docker compose exec web rhosocial-github-actions-tracker-db cleanup \
  --runs-before 90d --logs-before 365d

# 分区维护。两张日志表按执行触发时刻所在的 UTC 月分区，
# 因此“清理 6 个月”等于丢弃 6 个整月；这是一次元数据操作，不锁表，也不需要 VACUUM。
docker compose exec web rhosocial-github-actions-tracker-db partitions --list
docker compose exec web rhosocial-github-actions-tracker-db partitions --ensure 2
docker compose exec web rhosocial-github-actions-tracker-db partitions --drop-before 6m           # 试运行，只报告
docker compose exec web rhosocial-github-actions-tracker-db partitions --drop-before 6m --drop-old

# 删除并重建全部 ghat_* 表，保留数据库本身
docker compose exec web rhosocial-github-actions-tracker-db reset --yes

# 删除整个数据库；先断开连接，供 teardown 使用
docker compose exec web rhosocial-github-actions-tracker-db drop --yes

# 一体化形态：连同数据卷一起删除
docker compose -f docker-compose.yml -f docker-compose.bundled.yml down -v
```

`drop` 只删除 `GHAT_DB_NAME` 指向的数据库，并拒绝删除 `--maintenance-db`，默认值为 `postgres`。
`reset` 会连同凭据行一起清空。要恢复自动注入，可以执行：

```bash
docker compose run --rm -e GHAT_INITDB=force init
```

执行前请确认 `GHAT_DB_HOST` 与 `GHAT_DB_NAME` 指向正确的环境。

`stats` 统计行数，`usage` 统计存储占用。行数无法换算为字节，PostgreSQL 使用自己的方式度量存储，
包括 TOAST、索引与空闲空间；只有 introspector 知道这些信息。`usage` 的行数同样是 planner 的估算，
而不是 `COUNT(*)`。建表后尚未执行 analyze 的表不会打印数字：这表示“没有答案”，而不是会把读者误导成
“表是空的”的 0。`--json` 输出的报告与 `ci_status.storage` 携带的报告相同。

`init` 还会启用 `pg_trgm`，执行 `CREATE EXTENSION IF NOT EXISTS pg_trgm`。集群中“可用”的扩展记录在
`pg_available_extensions`，“已安装”的扩展记录在 `pg_extension`，这两张表不同。如果部署时未启用该扩展，
运行时会出现 `operator error: type trigram does not exist`，而不是功能降级。因此启用工作由本服务完成。
该步骤幂等，也不会阻断启动：服务器拒绝启用时，例如托管套餐未提供该扩展或权限不足，表仍会照常创建，
`usage` 会报告 `trgm_installed: false`。缺少加速器是一个可见状态，不是意外。

## 本服务如何得知有 CI 在运行

**唯一的判定条件是仓库是否已订阅。** 本服务不关心触发者是谁：push、PR、协作者在页面手点、
`gh workflow run` 以及本服务触发，一律采纳。因此不存在“是否需要通知本服务”的问题。

| 场景 | 本服务如何得知 | 运营者需要做什么 |
|---|---|---|
| 1. agent 提交并 push，工作流按 `on: push` 自动运行 | GitHub 把 `workflow_run` 投递到已配置的 webhook，本服务直接采纳，run 立即入库 | 无需操作 |
| 2. push 后在 GitHub 页面手动执行 Run workflow | 同上，`event` 记录为 `workflow_dispatch` | 无需操作 |
| 3. 通过本服务触发（`trigger_workflow` 或 CLI） | 触发时登记，随后由 webhook 补齐状态 | 执行一次调用 |
| 4. 使用 `gh workflow run` 直接触发 | 与场景 1 相同：webhook 照常到达，本服务照常采纳 | 无需操作；但本服务不知道这次触发来自运营者，也不会替运营者执行幂等判断 |
| 5. 发起 PR，由 `on: pull_request` 触发 | 与场景 1 相同，同时记录 `pr_number` | 无需操作 |

以下两个前提缺一不可：

1. **仓库已订阅**，即执行过 `repo subscribe`。未订阅仓库的投递会到达但被拒绝，只在投递台账中留下一条
   `handled=false`；
2. **仓库有 hook 指向本服务**，即配置了 `webhook_url` 与 `webhook_secret`。可以通过
   `webhook create --name owner/repo` 创建，也可以在 GitHub 上手工配置。没有 hook 时无法收到任何投递。

场景 1 至 5 都可能发生在**尚未订阅**的仓库上。此时不会有任何 run 入库。如果已登记的 fine-grained PAT
可以访问该仓库或所属组织，运营方补充一条 `repo subscribe` 即可开始纳管；历史 run 可以通过
`find_runs` 或 `--sync-runs` 补拉，不需要等待下一次触发。

只有场景 3 需要特别注意：`trigger_workflow` 与 `rerun_run` 不是幂等的。响应不明确时，先用 `find_runs`
核对，不要重试；重试会启动第二次运行。

## PAT 失效或权限不足时的表现

分为三个时点，表现完全不同：

| 时点 | 情形 | 表现与处置 |
|---|---|---|
| 首次初始化 | 令牌无效、被吊销或已过期 | `credential pat ensure` 会先让 GitHub 验证；验证失败则 **init 失败**，`web` 与 `mcp` 不启动；坏令牌不会写入数据库。更换令牌后重跑 `force init` |
| 运行期 | 令牌过期或被吊销 | 服务**不会停机**。需要访问 GitHub 的操作返回 `credential_expired` 或 `credential_invalid`，并带 `action_url`；只读数据库的工具（如 `ci_status`）照常工作；`find_runs` 与 `get_run` 需要访问 GitHub，会返回同一错误。按文档轮换令牌即可，不要用同一令牌重试 |
| 运行期 | 令牌缺少某个仓库的访问权限 | 访问该仓库的调用返回 `github_not_found`；GitHub 对无权仓库返回 404，不区分“不存在”与“无权”。`repo subscribe` 会因为 GitHub 不可见而失败。补充授权后重新录入，`credential pat ensure` 会原地轮换 |
| 运行期 | 令牌缺少 Actions 写权限 | 读取操作一切正常；`trigger_workflow` 与 `rerun_run` 会被拒绝，`ci_status.can_trigger` 为 `false`。补充权限后重跑 `credential pat show --probe-repo <repo>` 复测 |
| 任何时点 | webhook 仍会到达 | hook 安装在仓库侧，与令牌无关。因此库中的 run 状态仍会更新；只有需要访问 GitHub 的动作会失败，例如 `find_runs`、`get_run` 与补抓日志，台账中的 `unhandled` 可能增长 |

概括来说，**令牌决定能主动访问什么，hook 决定能被动收到什么**。令牌失效不会让推送通道静默，
但运营者在需要补充数据时会遇到阻碍。

## 运行时代理（仅中国大陆）

镜像本身不包含代理配置。需要代理的部署应在**运行时**注入，例如写入本地覆盖文件：

```yaml
services:
  web:
    environment:
      HTTP_PROXY: http://host.docker.internal:7897
      HTTPS_PROXY: http://host.docker.internal:7897
      NO_PROXY: localhost,127.0.0.1,::1,host.docker.internal,192.168.1.3
```

- Docker Desktop 会把宿主代理以 `127.0.0.1:7897` 注入容器，但容器内的 `127.0.0.1` 指向容器自身，
  因此必须覆盖为 `host.docker.internal`。
- `NO_PROXY` 必须包含数据库主机，否则数据库连接也会经过代理。
- 全球用户不需要这些变量；不设置时即为直连。

## 验证

```bash
curl -fsS https://<域名>:18080/readyz     # 就绪：包含一次数据库往返；失败时返回 503 与 checks
curl -fsS https://<域名>:18080/livez      # 存活：不访问数据库
docker compose logs -f init web mcp
docker compose exec web rhosocial-github-actions-tracker credential pat list
```

MCP 客户端配置见下一节。

### 投递台账与推送通道健康

每一个到达接收端的 GitHub 投递都会写入一行 `ghat_webhook_delivery`，无论该投递被处理、被忽略，
还是因为验签失败被拒绝。`workflow_run` 与 `workflow_job` 在 GitHub 侧是两条独立的流，payload 与生命周期都不同，
因此台账用 `event_type` 区分，并在报告中分开统计：

```bash
# 行数与按流统计都使用该命令
docker compose exec web rhosocial-github-actions-tracker-db stats

# 某个仓库没有 secret 时，投递会被拒绝并记录 secret_not_configured：
#   webhook status --name owner/repo     # configured: false 就是这个原因
#   webhook rotate-secret --name owner/repo --create-hook
```

`ci_status` 的 `health` 按流提供结构化信息，同时保留两个旧字段以兼容旧调用方：

```json
{
  "ledger_available": true,
  "runs_tracked": 15,
  "last_webhook_at": "2026-10-26T15:20:11+00:00",
  "webhooks_in_last_hour": 13,
  "push_channel": "active",
  "alerts": [],
  "streams": {
    "workflow_run": {"last_at": "...", "in_last_hour": 2, "deliveries_total": 37,
                     "redelivered": 1, "unhandled": 0, "unhandled_reasons": {},
                     "write_failures": 0, "last_write_failure": null},
    "workflow_job": {"last_at": "...", "in_last_hour": 11, "deliveries_total": 402,
                     "redelivered": 0, "unhandled": 4,
                     "unhandled_reasons": {"ignored": 4},
                     "write_failures": 0, "last_write_failure": null}
  }
}
```

判读方式如下：

- `last_at` 为 `null` 表示**该条流**从未收到过投递。两条流各自独立，不能用一条流的静默判断整条通道；
- `redelivered` 统计同一个 `X-GitHub-Delivery` 第二次到达的次数，例如在 GitHub 页面点击过 “Redeliver”。
  重发与自动投递对“通道是否存活”是同等证据，区别只在于时间；
- `unhandled` 统计到达但未改变任何状态的投递数量，例如未订阅仓库、`ping` 与非订阅事件。非零不等于故障，
  但持续增长通常意味着订阅范围与投递范围不一致；
- `unhandled_reasons` 是最近 24 小时内未处理原因的数量分布；`unhandled` 只是总数；
- **`write_failures` 与 `alerts` 是需要处理的一类。** `ignored`、`unsupported_event`、`invalid_json` 与
  `signature_failed` 表示“我们选择不落库”，归入 `unhandled_reasons`。失败是另一回事：例如 `IntegrityError`
  或投递根本没有到达数据库，意味着对应的 run、job 或 step 在库中缺失或陈旧。下一次读取会从 GitHub 补齐，
  因此这类告警描述的是推送通道，而不是数据丢失。这类原因会提升为顶层告警：

```json
"alerts": [{
  "code": "webhook_write_failed",
  "stream": "workflow_job",
  "count": 7,
  "last": {"delivery_id": "...", "received_at": "...", "repository": "...",
           "run_id": 40378123978, "job_id": 121746271630, "reason": "IntegrityError: ..."},
  "hint": "The delivery arrived but storing what it described failed ... call find_runs, which reads GitHub ..."
}]
```

告警带有 `delivery_id` 与目标 run 或 job，因此可以定位到具体投递与具体运行：

```bash
# 列出未落库的投递，包含 delivery_id、run、job 与原因
docker compose exec web rhosocial-github-actions-tracker-db deliveries --unhandled --limit 20
docker compose exec web rhosocial-github-actions-tracker-db deliveries --stream workflow_job --limit 50
```

修复路径是使用 `find_runs` 重新拉取；拉取通道不依赖 webhook。

**一条投递是一条写序列，不是一个原子单元。** 一个 `workflow_job` 事件会写入 run（如果尚不存在）、job 及其 steps。
本项目没有 unit of work，也没有跨模型事务：某一步失败时，写入就停在失败处，投递台账记录原因。
已写入的行都是完整行，缺失的部分由下一次读从 GitHub 补齐。若写入被当成一个整体并留下“job 存在但没有 steps”的
半截状态，该状态看起来仍然完整，不会有人修复，只能等到下一次与 GitHub 比对时才暴露；因此台账必须记录失败，
而不是把失败伪装成成功。

台账只保存关联与结论，**不保存 payload**。GitHub 自己的 Recent Deliveries 会保留 30 天，可以凭 `delivery_id`
取回原文。因此台账可以定期清理，不影响已落库的运行数据：

```bash
# 清理 30 天前的投递记录；run、job 与 step 数据不受影响
docker compose exec web rhosocial-github-actions-tracker-db cleanup --deliveries-before 30d
```
