# 配置

全部设置来自环境变量，服务没有配置文件。时区固定 UTC——连接作用域会执行
`SET TIME ZONE 'UTC'`，因为列是 `timestamptz`，返回值取自会话时区，而那是服务器的配置。

| 变量 | 默认 | 说明 |
|---|---|---|
| `GHAT_DB_HOST/PORT` | `localhost:5432`（compose 内 `postgres:5432`） | PostgreSQL 坐标 |
| `GHAT_DB_USER/PASSWORD/NAME` | `root` / `password` / `test_db` | 数据库账号 |
| `GHAT_DB_SSLMODE` | `prefer` | libpq sslmode |
| `GHAT_POOL_MIN/MAX` | `1` / `10` | 连接池。**每个在处理的请求占一条**，`wait_for_run` 等待期间一直握着 |
| `GHAT_API_KEY` | 空 | REST / MCP 的 Bearer；空表示不鉴权（启动告警），公网必须设置 |
| `GHAT_REST_HOST/PORT` | `0.0.0.0:8000` | REST 监听 |
| `GHAT_MCP_HOST/PORT` | `0.0.0.0:8001` | MCP 监听 |
| `GHAT_WEBHOOK_HOST/PORT` | `0.0.0.0:18080` | webhook 监听 |
| *(webhook secret)* | — | **不是环境变量。** 每个仓库在 `ghat_repo_webhook_secrets`
  里有自己的 secret；用 `webhook rotate-secret --name owner/repo` 生成/轮换，
  它会同时推送到该仓库的 hook。没有 secret 的仓库会被以 `secret_not_configured` 拒绝。 |
| `GHAT_WEBHOOK_URL` | 空 | 创建 webhook 时的默认回调 URL |
| `GHAT_PUBLIC_BASE_URL` | 空 | MCP action URL 的绝对前缀 |
| `GHAT_WEBHOOK_SSL_CERTFILE/KEYFILE` | 空 | webhook TLS；空表示明文 HTTP |
| `GHAT_REST_SSL_CERTFILE/KEYFILE` | 空 | REST TLS；空表示明文 HTTP |
| `GHAT_MCP_SSL_CERTFILE/KEYFILE` | 空 | MCP TLS；空表示明文 HTTP |
| `GHAT_WEBHOOK_SSL_HOSTNAME` | 空 | 校验证书用的主机名；空则从 `GHAT_PUBLIC_BASE_URL` / `GHAT_WEBHOOK_URL` 取，两者都无则跳过校验 |
| `GHAT_WEBHOOK_SSL_CAFILE` | 空 | 附加信任锚，用于私有 CA。也是"链顶是交叉签名根而本机 store 没有它"时的临时解：把它指向同一份 fullchain，被下发的根即进入锚点集 |
| `GHAT_WEBHOOK_SSL_CRLFILE` | 空 | 吊销清单（本地读，不联网） |
| `GHAT_WEBHOOK_SSL_EXPIRY_WARN_DAYS` | `30` | 还有多少天到期算"临期" |
| `GHAT_WEBHOOK_SSL_CHECK_OCSP` | `0` | `1` 询问 OCSP 响应者；有超时上限、失败不冤枉证书 |
| `GHAT_RESOURCE_URL` | 空 | 本 MCP 服务对外发布的完整 URL，如 `https://tracker-mcp.example.com:8001/mcp`。**MCP 要公网可达时必填**：Host/Origin 白名单由它推导，没有别的来源 |
| `GHAT_MCP_ALLOWED_HOSTS` | 空 | 额外放行的 `Host`（逗号分隔，`example.com:*` 通配端口）。默认取 resource 的主机名加 loopback |
| `GHAT_MCP_ALLOWED_ORIGINS` | 空 | 额外放行的 `Origin`。默认取 resource 的 origin；只在请求带 `Origin` 头时才校验 |
| `GHAT_DEFAULT_EVENTS` | `workflow_run,workflow_job` | 订阅的事件 |
| `GHAT_WAIT_MAX_SECONDS` | `300` | 一次 `wait_for_run` 的上限 |
| `GHAT_LOG_CAPTURE` | `1` | 是否采集 step 日志；`0` 全部退回整 job 合并文本 |
| `GHAT_LOG_CAPTURE_WINDOW_MINUTES` | `60` | run 结束后多久之内仍尝试采集（实测 GitHub 在 56–70 分钟之间压平） |
| `GHAT_LOG_CAPTURE_RETRIES` | `6` | 归档仍在写入时的重试次数 |
| `GHAT_LOG_CAPTURE_RETRY_DELAY_SECONDS` | `60` | 重试间隔（秒） |
| `GHAT_INITDB` | `auto` | `auto` / `force` / `skip` |
| `GHAT_INITDB_DIR` | `/docker-entrypoint-init.d` | init.d 目录 |
| `GHAT_LEGACY` | `0` | `1` 等价于 `INITDB=skip` |
| `GHAT_BOOTSTRAP_PAT` | 空 | 首次初始化注入的 PAT |
| `GHAT_BOOTSTRAP_PAT_FILE` | 空 | 从文件读 PAT，避免进入 `docker inspect` |
| `GHAT_BOOTSTRAP_PAT_NAME` | `default` | 凭据别名 |
| `GHAT_BOOTSTRAP_PROBE_REPO` | 空 | 验证 Actions 写权限用的 `owner/repo` |
| `GHAT_IMAGE` | 发布镜像 | compose 使用的镜像 |
| `GHAT_WEBHOOK_PORT_HOST` | `18080` | 宿主发布的 webhook 端口 |
| `GHAT_MCP_PORT_HOST` | `8001` | 宿主发布的 MCP 端口 |
| `GHAT_BUILD_REVISION` | 空 | 镜像构建时写入；`/readyz` 与 `ci_status` 回报 |
| `GHAT_BUILD_NUMBER` | 空 | 发布工作流的构建号 |
| `GHAT_BUILD_BUILT_AT` | 空 | 构建时间，ISO 8601 UTC |
