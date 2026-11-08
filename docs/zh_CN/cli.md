# 命令行

本项目提供两个可执行入口：`rhosocial-github-actions-tracker` 与
`rhosocial-github-actions-tracker-db`。本页按命令列出参数、输出与退出码。MCP 与 REST 见
[MCP 接口](mcp.md) 与 [REST 接口](rest.md)。

CLI 与另外两个界面共用同一组控制器：查询会回源 GitHub，写操作按顺序落库，不存在跨模型事务。
CLI 直接访问数据库，因此不需要 API key。

## 全局参数

全局参数必须写在子命令之前。

| 参数 | 含义 |
|---|---|
| `--format {text,json}` 或 `-f` | 输出格式；默认 `text` |
| `--verbose` 或 `-v` | 把库的 DEBUG 日志写到标准错误；默认只显示 `WARNING` 及以上 |
| `--api-key` | 协议端点的 key；CLI 命令本身不使用，通常也不需要设置 |

```bash
rhosocial-github-actions-tracker --format json runs --repo owner/repo --limit 5
```

库默认会把每条查询的调试日志写到标准错误。CLI 在启动时把库的日志级别固定为 `WARNING`，
只有 `--verbose` 才提升到 `DEBUG`。因此重定向标准输出得到的 JSON 或文本不会被日志行打断；
需要排查时显式加 `--verbose`。`GHAT_LOG_LEVEL` 属于服务进程，不影响命令行。

## 查询与操作命令

| 命令 | 必填参数 | 可选参数 | 输出 |
|---|---|---|---|
| `status` | `--repo`、`--run-id` | 无 | 一次 run 的状态、结论、分支与耗时 |
| `runs` | `--repo` | `--workflow`、`--branch`、`--status`、`--limit`（默认 10） | 最近的 run 列表 |
| `latest` | `--repo` | `--branch`、`--workflow`、`--wait` | 最新一次 run；不传 `--branch` 表示整个仓库最新的一次 |
| `jobs` | `--repo`、`--run-id` | 无 | 该 run 的 job 列表：编号、名称、状态、结论、runner、起止时间、耗时 |
| `logs` | `--repo`、`--job-id` | `--tail`、`--step`、`--failed-steps`、`--output` | 日志文本；`--output` 把完整文本写入文件 |
| `failures` | `--repo`、`--run-id` | `--tail`（默认 60） | 失败 step 的文本，按逐字节相同的内容分组 |
| `pr` | `--repo`、`--pr` | 无 | 该拉取请求的 run 列表 |
| `refresh` | `--repo`、`--run-id` | 无 | 强制从 GitHub 同步的结果：新增与变化、job 数与 step 数 |
| `trigger` | `--repo`、`--workflow`、`--ref` | `--input`、`--commit-sha`、`--if-absent`、`--wait` | 触发结果；`--wait` 时等待到结束 |
| `rerun` | `--repo`、`--run-id` | `--failed-only`、`--wait` | 重跑结果，以及 attempt 是否新增 |
| `cert` | 无 | `--certfile`、`--keyfile`、`--hostname`、`--cafile`、`--crlfile`、`--json` | 证书检查结果 |
| `serve {all,mcp,rest,webhook}` | 服务名 | `--host`、`--port` | 常驻进程；不返回普通结果 |

`status` 与 `jobs`、`failures` 都需要同时给出仓库与 run 编号：run 编号虽然全局唯一，但回源读取
需要知道它属于哪个仓库。

`--input` 可以重复，每次写一个 `键=值`：

```bash
rhosocial-github-actions-tracker trigger --repo owner/repo --workflow ci.yml --ref main \
  --input target=postgres --input python=3.12
```

`trigger` 默认就是触发，会消耗 CI 分钟。要“确保这个提交已经跑过”而不是再起一次，使用
`--if-absent`；服务会按 workflow 与提交查 GitHub，已有 run 时返回该 run，不再触发。
`--commit-sha` 只在 `--if-absent` 需要判断时使用；不传则读取 `--ref` 的当前提交，这要求凭据具备
Contents read 权限。

`jobs` 只列出 job，不列出 step。失败 step 的文本可以直接用 `logs --failed-steps` 读取；
`logs --step N` 的编号来自 MCP 的 `get_job` 或 REST 的 `GET /jobs/{job_id}`。

`--format json` 对返回数据的命令有效；`serve` 不返回普通结果，`cert` 的退出码语义见下文。

## 凭据管理

| 命令 | 必填参数 | 可选参数 | 输出 |
|---|---|---|---|
| `credential pat add` | `--name`、`--token` | `--scope`、`--github-name`、`--probe-repo` | 新增凭据；凭据名已存在时拒绝 |
| `credential pat ensure` | `--name`，以及 `--token` / `--token-file` / `--token-stdin` 之一 | `--scope`、`--github-name`、`--probe-repo` | 新增或轮换凭据，并验证令牌 |
| `credential pat list` | 无 | 无 | 凭据列表，不含明文 |
| `credential pat show` | `--name` | `--probe-repo` | 元数据与探测结果 |
| `credential pat rotate` | `--name`、`--token` | `--probe-repo` | 替换令牌，保留编号与绑定 |
| `credential pat refresh` | `--name` | `--probe-repo` | 重新探测并更新能力缓存 |
| `credential pat remove` | `--name` | 无 | 删除凭据；仍被仓库引用时拒绝 |

`--probe-repo` 指定 `owner/repo` 形式的仓库，用于测量 Actions 写权限与 webhook 管理权限。
两项能力都与仓库相关，不指定时保持未知。

`add` 只接受命令行上的 `--token`。需要从文件或标准输入读取时使用 `ensure`，它同时负责“不存在
则新增、已存在则轮换”。

## 仓库与 webhook

| 命令 | 必填参数 | 可选参数 | 输出 |
|---|---|---|---|
| `repo subscribe` | `--name` | `--credential`、`--webhook-secret-file`、`--create-hook` | 订阅结果；secret 未推送时只显示一次 |
| `repo list` | 无 | 无 | 已订阅仓库，以及每个仓库使用的凭据与写权限 |
| `repo unsubscribe` | `--name` | 无 | 删除本地订阅与缓存 |
| `repo bind` | `--name`、`--credential` | 无 | 绑定结果 |
| `repo unbind` | `--name` | 无 | 解除绑定 |
| `repo discover` | 无 | `--credential` | 该凭据在 GitHub 上可以访问的仓库 |
| `webhook create` | `--name` | 无 | 在 GitHub 创建 hook |
| `webhook delete` | `--name` | 无 | 删除 GitHub hook |
| `webhook list` | `--name` | 无 | GitHub hook 的信息 |
| `webhook ping` | `--name` | 无 | 发送一次 ping |
| `webhook status` | `--name` | 无 | 本地 secret 是否配置、状态与 hook 编号 |
| `webhook rotate-secret` | `--name` | `--create-hook` | 轮换 secret；未推送时只显示一次 |

`repo discover` 在还没有任何绑定时使用部署的 ambient 凭据；`--credential` 可以改为按别名指定
（例如 `work`）。其他 `repo` 命令按 `kind/name` 形式指定凭据（例如 `pat/work`）。

secret 只在“已生成但尚未推送到 GitHub”时显示一次。需要再次获取时只能轮换。

## 数据库维护

`rhosocial-github-actions-tracker-db` 负责数据库侧操作。

| 命令 | 必填参数 | 可选参数 | 输出 |
|---|---|---|---|
| `init` | 无 | `--no-create-database`、`--maintenance-db` | 建库建表；可重复执行 |
| `is-initialized` | 无 | 无 | 退出码 `0` 表示已初始化，`1` 表示尚未初始化 |
| `reset` | `--yes` | 无 | 删除并重建全部 `ghat_*` 表 |
| `drop` | `--yes` | `--maintenance-db` | 删除整个数据库 |
| `cleanup` | 至少一个清理参数 | `--runs-before`、`--logs-before`、`--deliveries-before` | 按时间清理 |
| `partitions` | 无 | `--list`、`--ensure N`、`--drop-before NyM`、`--drop-old`、`--json` | 分区列表，或创建与删除结果 |
| `backfill-log-times` | 无 | `--dry-run` | 为早期写入的日志行补齐触发时间 |
| `stats` | 无 | 无 | 各表的精确行数 |
| `usage` | 无 | `--json` | 数据库总大小、每表大小、最近一天新增 run、`pg_trgm` 状态 |
| `export` | `--table`、`--output` | 无 | 把一张表写成 JSON 文件 |
| `deliveries` | 无 | `--unhandled`、`--stream`、`--limit` | webhook 投递台账 |

`partitions --drop-before NyM` 默认只报告将要删除的分区，只有再加 `--drop-old` 才真正删除。

`stats` 使用 `COUNT(*)`，得到精确行数。`usage` 的行数是查询规划器的估计：表刚建好或长时间没有
执行 `ANALYZE` 时，这个估计可能明显偏离真实行数，它只用来判断磁盘占用，不要拿来核对行数。

`export` 按表原样导出，包括 `ghat_pat_credentials.token` 这样的明文列。导出这类表得到的文件必须
按密钥管理，不能放进仓库或聊天记录。

`deliveries` 打印的时间戳带数据库会话的显式偏移，可能是 `+08:00`；接口返回的时间戳始终是 UTC。

## 错误与退出码

命令失败时，CLI 把一句话写到标准错误，并以退出码 `1` 结束，例如：

```text
error: Run 12345 of 'owner/repo' has no failed job in its latest attempt (1); ...
```

命令报告的业务失败只写一句话，例如仓库未订阅、run 没有失败的 job；这类失败不会打印 Python traceback。
数据库驱动抛出的异常或程序缺陷仍会显示原始异常，届时需要结合 `--verbose` 的日志排查。

| 退出码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 一般错误：参数错误、仓库未订阅、GitHub 拒绝、数据库失败 |
| `2` | `cert` 发现证书问题；证书本身仍然可用，是否阻断由部署者决定 |

`is-initialized` 的 `0/1` 是状态而不是失败：`1` 表示尚未初始化。

## 示例

```bash
rhosocial-github-actions-tracker --format json runs --repo owner/repo --limit 5
rhosocial-github-actions-tracker latest --repo owner/repo --branch main
rhosocial-github-actions-tracker status --repo owner/repo --run-id 12345
rhosocial-github-actions-tracker jobs --repo owner/repo --run-id 12345
rhosocial-github-actions-tracker logs --repo owner/repo --job-id 67890 --tail 80
rhosocial-github-actions-tracker logs --repo owner/repo --job-id 67890 --failed-steps
rhosocial-github-actions-tracker failures --repo owner/repo --run-id 12345 --tail 40
rhosocial-github-actions-tracker trigger --repo owner/repo --workflow ci.yml --ref main \
  --if-absent
rhosocial-github-actions-tracker-db usage --json
rhosocial-github-actions-tracker-db partitions --list
```
