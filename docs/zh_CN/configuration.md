# 配置

所有设置都通过环境变量提供，服务没有配置文件。时区固定为 UTC：每个连接作用域都会执行
`SET TIME ZONE 'UTC'`。相关列的类型是 `timestamptz`，返回值取自会话时区，而会话时区来自服务器配置。

| 变量 | 默认 | 说明 |
|---|---|---|
| `GHAT_DB_HOST/PORT` | `localhost:5432`（compose 内为 `postgres:5432`） | PostgreSQL 地址与端口 |
| `GHAT_DB_USER/PASSWORD/NAME` | `root` / `password` / `test_db` | 数据库账号 |
| `GHAT_DB_SSLMODE` | `prefer` | libpq 的 sslmode |
| `GHAT_POOL_MIN/MAX` | `1` / `10` | 连接池。每个正在处理的请求占用一条连接；`wait_for_run` 在等待期间持续占用 |
| `GHAT_API_KEY` | 空 | REST / MCP 的 Bearer 凭据。为空表示不鉴权，启动时会告警；面向公网时必须设置 |
| `GHAT_REST_HOST/PORT` | `0.0.0.0:8000` | REST 监听地址 |
| `GHAT_MCP_HOST/PORT` | `0.0.0.0:8001` | MCP 监听地址 |
| `GHAT_WEBHOOK_HOST/PORT` | `0.0.0.0:18080` | webhook 监听地址 |
| *(webhook secret)* | — | **不是环境变量。** 每个仓库的 secret 保存在 `ghat_repo_webhook_secrets` 中；使用 `webhook rotate-secret --name owner/repo` 生成或轮换，该命令会同时推送到仓库的 hook。没有 secret 的仓库会被拒绝，原因是 `secret_not_configured` |
| `GHAT_WEBHOOK_URL` | 空 | 创建 webhook 时使用的默认回调 URL |
| `GHAT_PUBLIC_BASE_URL` | 空 | MCP action URL 的绝对地址前缀 |
| `GHAT_WEBHOOK_SSL_CERTFILE/KEYFILE` | 空 | webhook TLS 证书与私钥。为空表示明文 HTTP |
| `GHAT_REST_SSL_CERTFILE/KEYFILE` | 空 | REST TLS 证书与私钥。为空表示明文 HTTP |
| `GHAT_MCP_SSL_CERTFILE/KEYFILE` | 空 | MCP TLS 证书与私钥。为空表示明文 HTTP |
| `GHAT_WEBHOOK_SSL_HOSTNAME` | 空 | 校验证书时使用的主机名。为空时从 `GHAT_PUBLIC_BASE_URL` 或 `GHAT_WEBHOOK_URL` 推导；两者都未设置则跳过校验 |
| `GHAT_WEBHOOK_SSL_CAFILE` | 空 | 附加信任锚，用于私有 CA。当证书链顶端是交叉签名根、本机信任库没有该根时，也可以把它指向同一份 fullchain，使服务下发的根进入信任锚集合 |
| `GHAT_WEBHOOK_SSL_CRLFILE` | 空 | 需要查询的吊销清单，仅本地读取，不联网 |
| `GHAT_WEBHOOK_SSL_EXPIRY_WARN_DAYS` | `30` | 距离到期多少天时开始告警 |
| `GHAT_WEBHOOK_SSL_CHECK_OCSP` | `0` | 设为 `1` 时询问 OCSP 响应者。查询有超时上限，失败时不判定证书无效 |
| `GHAT_RESOURCE_URL` | 空 | 本 MCP 服务对外发布的完整 URL，例如 `https://tracker-mcp.example.com:8001/mcp`。**MCP 需要公网访问时必须设置**：SDK 强制的 Host/Origin 白名单由该地址推导，没有其他来源 |
| `GHAT_MCP_ALLOWED_HOSTS` | 空 | 额外允许的 `Host`，用逗号分隔，`example.com:*` 表示通配端口。默认取 resource 的主机名与 loopback |
| `GHAT_MCP_ALLOWED_ORIGINS` | 空 | 额外允许的 `Origin`。默认取 resource 的 origin；仅当请求携带 `Origin` 头时才校验 |
| `GHAT_DEFAULT_EVENTS` | `workflow_run,workflow_job` | 订阅的事件 |
| `GHAT_WAIT_MAX_SECONDS` | `300` | 一次 `wait_for_run` 调用的上限 |
| `GHAT_LOG_CAPTURE` | `1` | 是否采集 step 日志。设为 `0` 时全部退回整 job 合并文本 |
| `GHAT_LOG_CAPTURE_WINDOW_MINUTES` | `60` | run 结束后多久之内仍然尝试采集。实测 GitHub 在 56–70 分钟之间压平分片 |
| `GHAT_LOG_CAPTURE_RETRIES` | `6` | 归档仍在写入期间的重试次数 |
| `GHAT_LOG_CAPTURE_RETRY_DELAY_SECONDS` | `60` | 重试间隔，单位为秒 |
| `GHAT_INITDB` | `auto` | 可选 `auto` / `force` / `skip` |
| `GHAT_INITDB_DIR` | `/docker-entrypoint-init.d` | init.d 目录位置 |
| `GHAT_LEGACY` | `0` | 设为 `1` 等价于 `GHAT_INITDB=skip` |
| `GHAT_BOOTSTRAP_PAT` | 空 | 首次初始化时注入的 PAT |
| `GHAT_BOOTSTRAP_PAT_FILE` | 空 | 从文件读取 PAT，避免其出现在 `docker inspect` 中 |
| `GHAT_BOOTSTRAP_PAT_NAME` | `default` | 凭据别名 |
| `GHAT_BOOTSTRAP_PROBE_REPO` | 空 | 用于验证 Actions 写权限的 `owner/repo` |
| `GHAT_IMAGE` | 发布镜像 | compose 使用的镜像 |
| `GHAT_WEBHOOK_PORT_HOST` | `18080` | 宿主发布的 webhook 端口 |
| `GHAT_MCP_PORT_HOST` | `8001` | 宿主发布的 MCP 端口 |
| `GHAT_BUILD_REVISION` | 空 | 镜像构建时写入；`/readyz` 与 `ci_status` 会回报该值 |
| `GHAT_BUILD_NUMBER` | 空 | 发布工作流的构建号 |
| `GHAT_BUILD_BUILT_AT` | 空 | 构建时间，ISO 8601 UTC |
