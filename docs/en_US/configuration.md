# Configuration

Every setting comes from an environment variable; the service has no config file.
The time zone is fixed to UTC: every connection scope runs
`SET TIME ZONE 'UTC'`, because the columns are `timestamptz` and the returned
values follow the session time zone, which is the server's setting.

| Variable | Default | Meaning |
|---|---|---|
| `GHAT_DB_HOST/PORT` | `localhost:5432` (in compose: `postgres:5432`) | PostgreSQL coordinates |
| `GHAT_DB_USER/PASSWORD/NAME` | `root` / `password` / `test_db` | database account |
| `GHAT_DB_SSLMODE` | `prefer` | libpq sslmode |
| `GHAT_POOL_MIN/MAX` | `1` / `10` | pool; scoped **per database operation** — a controller wraps each SQL statement in `async with self.connection()`. `wait_for_run` holds no connection while waiting on GitHub; only concurrent SQL needs a slot |
| `GHAT_API_KEY` | empty | Bearer for REST / MCP; empty means no auth (warns at startup) |
| `GHAT_AUTH_CACHE_TTL_SECONDS` | `30` | lifetime of the auth snapshot. `0` disables the cache and falls back to the per-request lookup (a rollback switch); see the [probes section of operations](operations.md#probes) |
| `GHAT_AUTH_CACHE_MAX_ENTRIES` | `50000` | cap on repositories in the snapshot; exceeding it is reported, not silently dropped |
| `GHAT_AUTH_CACHE_REFRESH_BACKOFF_SECONDS` | `5` | how long a failed refresh waits before another request may retry |
| `GHAT_WEBHOOK_MAX_BODY_BYTES` | `26214400` | max delivery body (25 MB); larger is refused |
| `GHAT_WEBHOOK_READ_TIMEOUT_SECONDS` | `10` | timeout for reading one delivery body; overtime is refused |
| `GHAT_PAT_NAME` | `default` | default credential alias |
| `GHAT_REST_HOST/PORT` | `0.0.0.0:8000` | REST listener |
| `GHAT_MCP_HOST/PORT` | `0.0.0.0:8001` | MCP listener |
| `GHAT_WEBHOOK_HOST/PORT` | `0.0.0.0:18080` | webhook listener |
| *(webhook secret)* | — | **Not an environment variable.** Each repository
  has its own secret in `ghat_repo_webhook_secrets`; create or rotate one with
  `webhook rotate-secret --name owner/repo`, which also pushes it to the hook.
  A repository without one is refused with `secret_not_configured`. |
| `GHAT_WEBHOOK_URL` | empty | default callback URL |
| `GHAT_PUBLIC_BASE_URL` | empty | prefix for absolute action URLs |
| `GHAT_WEBHOOK_SSL_CERTFILE/KEYFILE` | empty | webhook TLS; empty means plain HTTP |
| `GHAT_REST_SSL_CERTFILE/KEYFILE` | empty | REST TLS; empty means plain HTTP |
| `GHAT_MCP_SSL_CERTFILE/KEYFILE` | empty | MCP TLS; empty means plain HTTP |
| `GHAT_WEBHOOK_SSL_HOSTNAME` | empty | hostname the certificate is checked against; empty derives it from `GHAT_PUBLIC_BASE_URL` / `GHAT_WEBHOOK_URL`, and skips the check when there is neither |
| `GHAT_WEBHOOK_SSL_CAFILE` | empty | extra trust anchor, for a private CA. Also the workaround for a chain that ends in a cross-signed root the machine's store does not carry: point it at the same fullchain and the served root joins the anchors |
| `GHAT_WEBHOOK_SSL_CRLFILE` | empty | revocation list to consult (local, no network) |
| `GHAT_WEBHOOK_SSL_EXPIRY_WARN_DAYS` | `30` | how near expiry is worth a warning |
| `GHAT_WEBHOOK_SSL_CHECK_OCSP` | `0` | `1` asks the responder about revocation, bounded and soft-fail |
| `GHAT_RESOURCE_URL` | empty | the full URL this MCP server is published as, e.g. `https://tracker-mcp.example.com:8001/mcp`. **Required when MCP is public**: it derives the Host/Origin allowlist the SDK enforces, and the check cannot derive it any other way |
| `GHAT_MCP_ALLOWED_HOSTS` | empty | extra `Host` values (comma-separated, `example.com:*` for a wildcard port). Defaults to the resource's host plus loopback |
| `GHAT_MCP_ALLOWED_ORIGINS` | empty | extra `Origin` values. Defaults to the resource's origin; only checked when a request carries an `Origin` |
| `GHAT_DEFAULT_EVENTS` | `workflow_run,workflow_job` | subscribed events |
| `GHAT_WAIT_MAX_SECONDS` | `300` | ceiling for one `wait_for_run` call |
| `GHAT_LOG_CAPTURE` | `1` | capture step logs at all; `0` falls back to merged job logs |
| `GHAT_LOG_CAPTURE_WINDOW_MINUTES` | `60` | how long after completion a capture is still attempted (measured: GitHub stops the split between 56 and 70 minutes) |
| `GHAT_LOG_CAPTURE_RETRIES` | `6` | attempts while the archive is still filling |
| `GHAT_LOG_CAPTURE_RETRY_DELAY_SECONDS` | `60` | seconds between them. These four are the **read path** budget; webhook capture is separate — 3 attempts × 5 seconds, see the `GHAT_CAPTURE_*` rows below |
| `GHAT_CAPTURE_CONCURRENCY` | `4` | max simultaneous downloads in the webhook capture executor |
| `GHAT_CAPTURE_MAX_PENDING` | `1000` | cap on queued captures; overflow drops the oldest, counts it, and leaves repair to a read inside the archive window |
| `GHAT_CAPTURE_DRAIN_TIMEOUT_SECONDS` | `5` | how long shutdown waits for in-flight captures before cancelling them, which is counted in `ci_status.health.capture` |
| `GHAT_CAPTURE_WEBHOOK_RETRIES` | `3` | retries for a webhook capture (distinct from the read path's 6) |
| `GHAT_CAPTURE_WEBHOOK_RETRY_DELAY_SECONDS` | `5` | seconds between webhook-capture retries |
| `GHAT_INITDB` | `auto` | `auto` / `force` / `skip` |
| `GHAT_INITDB_DIR` | `/docker-entrypoint-init.d` | where the init scripts live |
| `GHAT_LEGACY` | `0` | `1` = `GHAT_INITDB=skip` |
| `GHAT_BOOTSTRAP_PAT` | empty | PAT injected on first boot |
| `GHAT_BOOTSTRAP_PAT_FILE` | empty | read the PAT from a file, so it never appears in `docker inspect` |
| `GHAT_BOOTSTRAP_PAT_NAME` | `default` | credential alias |
| `GHAT_BOOTSTRAP_PROBE_REPO` | empty | `owner/repo` used to verify Actions write access |
| `GHAT_IMAGE` | published image | the image compose uses |
| `GHAT_WEBHOOK_PORT_HOST` | `18080` | host-published webhook port |
| `GHAT_MCP_PORT_HOST` | `8001` | host-published MCP port |
| `GHAT_BUILD_REVISION` | empty | written by the image build; reported by `/livez`, `/readyz` and `ci_status` |
| `GHAT_BUILD_NUMBER` | empty | the publishing workflow's run number |
| `GHAT_BUILD_BUILT_AT` | empty | build timestamp, ISO 8601 UTC |
