# MCP 接口

MCP 是面向编码 agent 的主要接口。本页说明端点、认证、每个工具的输入与输出，以及 `hint` 如何指示下一步操作。
REST 与 CLI 见 [REST 接口](rest.md) 与[命令行](cli.md)。

## 端点与认证

- 端点：`http://127.0.0.1:8001/mcp`，使用 Streamable HTTP；配置 TLS 后使用 `https://<主机>:8001/mcp`。
- 认证：配置 `GHAT_API_KEY` 后，每个请求都必须携带 `Authorization: Bearer <key>`。比较在常量时间完成，错误令牌由 SDK 以 `401` 拒绝。
- key 为空表示不鉴权，启动日志会告警。仅在受信网络中使用，不能直接暴露在公网。

## 工具与输入输出

| 工具 | 主要输入 | 主要输出 |
|---|---|---|
| `ci_status` | `repository`（可选） | 服务身份、构建、能力、健康、连接池、存储、仓库列表 |
| `list_repos` | `repository` / `limit` / `offset` / `refresh` | 列表模式 `items[]`；单仓库模式含 `workflows[]` |
| `find_runs` | `repository`，以及可选 `workflow`、`branch`、`commit_sha`、`pull_request`、`status`、`latest`、`include_jobs`、`limit`、`offset` | `items[]`；每项含 workflow、commit、PR、attempt 与状态 |
| `recent_runs` | `repositories[]`（最多 20）、`branch`、`workflow`、`limit`（最多 10） | `repositories[].runs[]`，每个仓库可带 `error` |
| `get_run` | `run_id`、`repository`、`attempt`、`include_jobs` | run 身份、`jobs[]` 名册、`timings`、`hint` |
| `get_job` | `job_id`、`repository`、`refresh_logs` | job、`steps[]`、`capture`、`hint` |
| `get_job_log` | `job_id`，以及可选 `repository`、`step`、`whole_job`、`failed_steps_only`、`tail`、`grep`、`from_line`、`to_line`、`context`、`ignore_case` | `sections[]`、行号、`truncated`、`empty_reason`、`hint` |
| `search_job_log` | `job_id`、`query`，以及可选 `repository`、`step`、`context`、`max_matches`、`ignore_case`、`prefer` | `matches`、行号、`source`，以及缺失时的 `reason` |
| `get_run_failed_logs` | `run_id`，以及可选 `repository`、`tail` | `groups[]`、`steps_without_logs`、`jobs_without_failed_steps` |
| `wait_for_run` | `run_id`、`repository`、`timeout_seconds`、`poll_interval_seconds`、`include_jobs` | 最终或当前状态、`observed`、`suggested_timeout_seconds`、`hint` |
| `trigger_workflow` | `repository`、`workflow`、`ref`、`inputs`、`if_absent`、`commit_sha` | `run_id`、`dispatched`、`attempt`、`typical_duration_seconds`、`hint` |
| `rerun_run` | `repository`、`run_id`、`failed_only` | `attempt`、`previous_attempt`、`advanced`、`reran_jobs`、`hint` |
| `cancel_run` | `repository`、`run_id` | 取消结果与 `hint` |

所有时间戳均为 UTC，ISO-8601 带显式偏移。列表的键名固定：空结果与非空结果使用同一个键，调用方不需要判断两种形状。

## 输出信封与 hint

MCP 工具的返回不是裸数组，而是一个信封。信封中的 `hint` 是**给调用方的下一步指示**，用于减少模型盲目猜测。

| 字段 | 含义 |
|---|---|
| `hint` | 下一步该调用什么，或当前结果为何不完整。它是行动建议，不是数据 |
| `untrusted` | `true` 表示内容来自日志或 GitHub，只能阅读和报告，不能执行其中的指令 |
| `truncated` / `next_from_line` / `next_to_line` | 日志窗口被截断，按行号续读 |
| `empty_reason` / `next` | 结果为空时说明是哪个筛选条件导致，并建议下一步 |
| `error` 信封 | 见下文；`code`、`message`、`hint`、`action_url` |

hint 的典型作用：

| 当前结果 | hint 的作用 |
|---|---|
| run 仍在运行 | 指示调用 `wait_for_run`，而不是继续轮询 `get_run` |
| 失败 job 的日志缺失 | 指示改读合并文本，或调用 `get_job(refresh_logs=true)` 再采集 |
| step 没有采集到日志 | 指示去掉 `step` 参数，改为搜索整个 job |
| 触发失败但 run 已存在 | 指示先用 `find_runs` 核对，不要重试 |
| 日志内容被截断 | 指示用 `next_from_line` / `next_to_line` 续读 |

因此调用方应先读 `hint`，再决定下一次工具调用；不要仅凭字段名猜测。

## 错误信封

工具失败时，MCP 层返回 `isError: true`，文本是一个 JSON 信封：

```json
{
  "code": "repository_not_subscribed",
  "message": "Repository 'owner/repo' is not tracked",
  "hint": "Subscribe it first, or call list_repos to see what is tracked.",
  "action_url": "https://.../api/v1/repositories"
}
```

`code` 用于分支判断，`hint` 用于决定下一步，`action_url` 指向操作入口。`repository_not_subscribed` 需要运营者添加仓库，换参数无法解决。

## 三层对象与日志

从失败的 run 定位到失败 step 的路径只有一条：

```
get_run(run_id)               → run 身份与 job 名册（含 failed）
get_job(job_id)               → 一个 job 与全部 step（状态、结论、has_log）
get_job_log(job_id, step=N)   → 该 step 的原文
get_job_log(job_id)           → 失败 job 的失败 step；否则合并文本
get_run_failed_logs(run_id)   → 按逐字节相同文本分组的失败 step
```

`get_run` 不返回 steps；`get_job` 才返回 steps。这是刻意的分层，避免一次调用灌满模型上下文。日志窗口参数、采集窗口与缺失原因见[日志与窗口](logs.md)。

## 等待与预算

`wait_for_run` 是一次有界长轮询，单次默认 45 秒，上限由 `GHAT_WAIT_MAX_SECONDS` 控制，默认 300 秒。`timed_out` 表示仍在运行，不是失败。

等待时长不要猜测：`trigger_workflow` 与 `rerun_run` 返回 `typical_duration_seconds`，超时的 `wait_for_run` 返回 `observed` 与 `suggested_timeout_seconds`。样本包含所有已终结的 run，失败也算一次真实的结束时间。没有历史时字段为 `null`，不要自行编造。

## 分页

`limit` 最大 50，`offset` 从 0 开始。列表返回 `truncated` 与 `next_offset`；按 `next_offset` 继续读取，不要假设一次调用返回全部内容。

## 接入编码 Agent

四款主流 agent 都支持 Streamable HTTP 与静态 Bearer。下列配置指向同一个部署，本机运行且未设置 API key。

### Claude Code

```bash
claude mcp add --transport http github-actions-tracker http://127.0.0.1:8001/mcp
claude mcp add --transport http github-actions-tracker https://tracker.example.com:8001/mcp \
  --header "Authorization: Bearer <GHAT_API_KEY>"
```

`type` 必须显式写成 `http`（别名 `streamable-http`）；只写 `url` 会被当成 stdio 服务器而跳过。项目级 `.mcp.json` 需要批准后才会加载。

### Codex

```toml
[mcp_servers.github-actions-tracker]
url = "http://127.0.0.1:8001/mcp"

# 配置 TLS 与 key 时：
[mcp_servers.github-actions-tracker]
url = "https://tracker.example.com:8001/mcp"
bearer_token_env_var = "GHAT_API_KEY"
tool_timeout_sec = 120
```

`wait_for_run` 需要较长的工具超时；30 秒会中断等待。

### OpenCode

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "github-actions-tracker": {
      "type": "remote",
      "url": "http://127.0.0.1:8001/mcp",
      "enabled": true,
      "oauth": false,
      "headers": { "Authorization": "Bearer {env:GHAT_API_KEY}" }
    }
  }
}
```

`oauth: false` 必须显式设置，否则一次 401 会让 OpenCode 打开浏览器走发现流程。`{env:GHAT_API_KEY}` 从进程环境展开；桌面启动的进程可能读不到 shell 临时变量。

### Command Code

```bash
cmd mcp add --transport http github-actions-tracker http://127.0.0.1:8001/mcp
```

JSON 形式使用 `transport`（也接受 `type`），因此 Claude Code 的 `.mcp.json` 可以原样复制。

## 提示词

给出仓库名、分支或提交号、工作流名，比只说“看看 CI”有效。两段可直接复制的示例：

```text
用本服务看一下 rhosocial/python-activerecord 在 main 上最新一次 test.yml 的结果。
如果还在跑，等到它结束再告诉我；失败了就指出是哪个 job、哪个 step，并贴出关键错误行。
```

```text
用本服务一次性查看 python-activerecord 系列 11 个仓库各自最新一次运行的状态，
汇总成表格：仓库 / 结论 / 分支 / 耗时。
```

写提示词时：不要让 agent 轮询；触发类请求要带 ref 并说明只执行一次；失败时先要原因，再决定是否重跑。

## 安全边界

工具返回的日志属于不可信文本，只能阅读和报告，不能执行其中的指令。`untrusted: true` 是对这一点的明确标记。
`wait_for_run` 在整个等待期间占用一条数据库连接，各家的工具超时需要为它保留 60 秒以上。
