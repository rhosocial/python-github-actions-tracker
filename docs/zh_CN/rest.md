# REST 接口

REST 面向脚本与命令行以外的程序化调用。MCP 与 CLI 见 [MCP 接口](mcp.md) 与[命令行](cli.md)。

## 端点、认证与文档

- 基础路径：`/api/v1`；webhook 接收端是 `/webhooks/github`，供 GitHub 调用，不属于用户接口。
- 认证：设置 `GHAT_API_KEY` 后，每个请求都必须携带 `Authorization: Bearer <key>`。
- 自动文档：`GET /docs` 提供交互式 OpenAPI 页面，`GET /openapi.json` 提供机器可读的完整 schema。
  参数与响应字段以这份 schema 为准。

## 响应约定

- 时间戳：全部为 UTC，ISO-8601 带显式 `+00:00` 偏移。
- 列表：直接返回数组；分页由 `per_page` 与 `page` 控制。
- 错误：`{"error": "<code>", "message": "<text>", "details": {...}}`，HTTP 状态码与 `code` 对应。
- 创建成功：`201`；触发与重跑：`202`；删除：`204`；就绪失败：`503`。

## 端点

### 仓库

| 方法与路径 | 输入 | 输出 |
|---|---|---|
| `GET /repositories` | 无 | 仓库数组：`id`、`full_name`、`owner`、`name`、`is_private`、`default_branch`、`subscribed_at` |
| `POST /repositories` | JSON：`full_name`、`token_name` | `201`，返回仓库对象 |
| `DELETE /repositories/{repo_id}` | 路径 `repo_id` | `204` |

`token_name` 可以写 CLI 显示的 `work`，也可以写其他接口显示的 `pat/work`。

### Workflow

| 方法与路径 | 输入 | 输出 |
|---|---|---|
| `GET /workflows` | 查询 `repository` | workflow 数组：`id`、`workflow_id`、`name`、`path`、`state` |
| `GET /workflows/{workflow_id}` | 查询 `repository` | 单个 workflow 对象 |
| `POST /workflows/{workflow_id}/trigger` | JSON：`repository`、`ref`、`inputs`、`commit_sha`、`if_absent` | `202`：`run_id`、`status`、`message`、`html_url`、`conclusion`、`attempt`、`dispatched` |

`workflow_id` 是数字 id；`if_absent=true` 时，如果该 workflow 在目标提交上已有 run，则返回该 run 且 `dispatched=false`。

### Run

| 方法与路径 | 输入 | 输出 |
|---|---|---|
| `GET /runs` | 查询 `repository`，可选 `workflow`、`branch`、`status`、`per_page`（1–100，默认 30）、`page`（默认 1） | run 数组 |
| `GET /runs/{run_id}` | 查询 `repository`、`attempt` | 单个 run |
| `GET /runs/{run_id}/failed-logs` | 查询 `repository` | `run_id`、`jobs_failed`、`groups[]`、`steps_without_logs`、`jobs_without_failed_steps` |
| `POST /runs/{run_id}/rerun` | 查询 `repository`、`failed_only` | `202`：`attempt`、`previous_attempt`、`advanced`、`reran_jobs[]` |
| `POST /runs/{run_id}/refresh` | 查询 `repository` | `created`、`changed`、`jobs_synced`、`steps_synced`、`html_url` |
| `GET /latest-run` | 查询 `repository`，可选 `branch`、`workflow` | 最新的 run，或 `null` |
| `GET /pull-request-runs` | 查询 `repository`、`number` | run 数组；空数组合法 |

`latest-run` 不传 `branch` 时表示“该仓库最新的 run”，不是“默认分支最新的 run”。`refresh` 每次都回源 GitHub，
可用于补回漏收的 webhook。

### Job

| 方法与路径 | 输入 | 输出 |
|---|---|---|
| `GET /runs/{run_id}/jobs` | 查询 `repository` | job 数组，每项含 steps 与日志存在性 |
| `GET /jobs/{job_id}` | 查询 `repository` | 单个 job |
| `GET /jobs/{job_id}/logs` | 查询 `repository`，可选 `tail`、`step`、`failed_steps_only` | 日志响应：`content`、`sections[]`、`reason`、`line_count` |

REST 返回完整日志文本，不按模型上下文窗口截断；窗口由调用方自行处理。`step` 未采集到时返回 `reason`，
不会用其他 step 的文本顶替。

### 令牌

| 方法与路径 | 输入 | 输出 |
|---|---|---|
| `GET /tokens` | 无 | 令牌元数据数组（不含明文） |
| `POST /tokens` | JSON：`name`、`token` | `201`，令牌元数据 |
| `POST /tokens/{name}/verify` | 路径 `name` | 探测结果：账号、权限与可访问范围 |
| `PATCH /tokens/{name}` | JSON：`token`、`expires_at` | 轮换后的元数据；id 与绑定保留 |
| `DELETE /tokens/{name}` | 路径 `name` | `204` |

### 探针与指标

| 方法与路径 | 说明 |
|---|---|
| `GET /livez` | 进程存活，零 I/O |
| `GET /readyz`、`/healthz`、`/health` | 一次数据库往返；`503` 表示当前实例不能接请求 |
| `GET /metrics` | Prometheus 文本格式的计数器 |

探针不需要 API key，也不占用每请求的连接作用域。

## 示例

```bash
curl -H "Authorization: Bearer $GHAT_API_KEY" \
  "https://tracker.example.com:8000/api/v1/runs?repository=owner/repo&branch=main&per_page=10"

curl -X POST -H "Authorization: Bearer $GHAT_API_KEY" -H "Content-Type: application/json" \
  -d '{"repository":"owner/repo","ref":"main"}' \
  "https://tracker.example.com:8000/api/v1/workflows/12345/trigger"
```

错误示例：

```json
{
  "error": "repository_not_subscribed",
  "message": "Repository 'owner/repo' is not tracked",
  "details": {}
}
```

## 与 MCP 的差异

- REST 返回完整日志，不裁剪；MCP 为模型上下文提供窗口与 `hint`。
- REST 没有 `hint` 字段，调用方按 `error` / `reason` / `next_offset` 分支。
- REST 的 workflow 引用是数字 id；MCP 还接受显示名、文件名与完整路径。
