# Installation

## Two installations: fresh and legacy

“Fresh” and “legacy” describe **the database**, not the package.

The database schema has not stabilized yet, and the tables may still be replaced wholesale.
A legacy installation is therefore appropriate only for a deployment that can manage its own schema;
when the data can be discarded, a fresh installation is preferable, because it keeps later schema
rebuilds free of the old table shapes.

| Type | Database state | Order |
|---|---|---|
| **Fresh installation** | The service is using this database for the first time | initialize the database first, then start the service |
| **Legacy installation** | The database exists, and you manage its schema and credentials yourself | do not initialize; start the service directly |

The distinction is simple: if the `ghat_*` tables do not exist, install fresh; if they already exist
and the data must be preserved, install as a legacy installation.

---

## Fresh installation

### Step 1: initialize the database

A fresh installation must have the database before the service starts. The initialization creates
the `ghat_*` tables and does not clear existing data.

Native:

```bash
# creates the database too when the user may; add --no-create-database for managed services
rhosocial-github-actions-tracker-db init
```

Container:

```bash
docker compose --env-file docker/stack.env up -d      # init runs first; web and mcp start after it succeeds
# or as a one-shot init container
docker compose run --rm -e GHAT_INITDB=auto init
```

Single container: mount the `init.d` directory, or the first boot fails explicitly:

```bash
docker run -d --name tracker-web --restart unless-stopped \
  -v "$PWD/docker/init.d:/docker-entrypoint-init.d:ro" \
  -v /path/to/certs:/certs:ro \
  -e GHAT_DB_HOST=<db-host> -e GHAT_DB_PORT=5432 \
  -e GHAT_DB_USER=<user> -e GHAT_DB_PASSWORD=<password> \
  -e GHAT_DB_NAME=github_actions_tracker \
  -e GHAT_API_KEY=<bearer> \
  -e GHAT_BOOTSTRAP_PAT=<fine-grained-pat> \
  -e GHAT_WEBHOOK_SSL_CERTFILE=/certs/fullchain.pem \
  -e GHAT_WEBHOOK_SSL_KEYFILE=/certs/privkey.pem \
  registry.cn-shanghai.aliyuncs.com/vistart_public/github-actions-tracker:latest   # domestic China
  # docker.io/vistart/github-actions-tracker:latest                               # everywhere else
```

`GHAT_INITDB` accepts three values: `auto` runs when the tables are absent, `force` always runs, and
`skip` never runs. `GHAT_LEGACY=1` is equivalent to `GHAT_INITDB=skip`.

### Step 2: register the credential

A native install registers the credential explicitly, which is the same step `20-pat.sh` performs
inside the container:

```bash
rhosocial-github-actions-tracker credential pat ensure \
  --name work --token-file /path/to/github-actions-tracker-pat --probe-repo owner/repo
```

A container install puts the PAT into `docker/stack.env`, and `20-pat.sh` reads it at initialization:

```dotenv
GHAT_BOOTSTRAP_PAT=github_pat_xxx
GHAT_BOOTSTRAP_PAT_NAME=work
GHAT_BOOTSTRAP_PROBE_REPO=owner/repo
```

To keep the token out of `docker inspect`, place it in a directory and mount that directory into
the `init` container:

```yaml
volumes:
  - ./docker/secrets:/run/secrets:ro
```

```dotenv
GHAT_BOOTSTRAP_PAT_FILE=/run/secrets/github-actions-tracker-pat
```

### Step 3: start the service

**Native:**

```bash
pip install rhosocial-github-actions-tracker                # if the package is not installed yet
rhosocial-github-actions-tracker serve all                  # REST :8000 + webhook :18080
rhosocial-github-actions-tracker serve mcp                  # MCP :8001
```

**Compose**, after initialization:

```bash
cp docker/stack.env.example docker/stack.env   # database, API key, webhook secret, PAT
mkdir -p docker/certs && cp <fullchain.pem> <privkey.pem> docker/certs/
docker compose --env-file docker/stack.env up -d
curl -k https://127.0.0.1:18080/readyz        # {"status":"ready",...}
```

#### Preparing a native environment

Docker is not required. The native install is the same service with the same behavior; the differences
are who triggers the first initialization, and who supervises the long-running processes.

Everything in the compose `environment:` block comes from `GHAT_*` environment variables, read by
both entry points: the database coordinates, the API key, the webhook callback address, the TLS paths
and the PAT name. There is no config file, no init directory and no data volume. `docker/stack.env.example`
is still a ready-made checklist.

| In the container | Natively |
|---|---|
| `init` runs once, then `web` and `mcp` start | run `db init`, then start both processes |
| `restart: unless-stopped` brings the containers back | supervise the two `serve` processes |

Check the environment first:

```bash
python .claude/skills/ops-deploy-image/scripts/check_native_env.py
```

Exit `0` is ready, `2` is usable with findings, `1` cannot run. The script reports the interpreter
version 3.10+, whether both commands are installed, whether psycopg 3 is importable, and the Windows
event-loop caveat.

**Windows**: psycopg's async mode needs `asyncio.SelectorEventLoop`, and the CLI installs it for its
own processes. Anything else driving the controllers (a custom runner, an IDE test harness) must do
the same:

```python
import asyncio, selectors
asyncio.set_event_loop(asyncio.SelectorEventLoop(selectors.SelectSelector()))
```

#### Running as a service

The two processes are independent and can be supervised separately:

```ini
# /etc/systemd/system/tracker-web.service
[Unit]
Description=github-actions-tracker — REST and webhook
After=network-online.target

[Service]
EnvironmentFile=/etc/github-actions-tracker/env
ExecStart=/opt/github-actions-tracker/venv/bin/rhosocial-github-actions-tracker serve all
Restart=unless-stopped

[Install]
WantedBy=multi-user.target
```

Use the same unit with `ExecStart=... serve mcp` for the MCP listener. `EnvironmentFile` is where the
`GHAT_*` values live, the native equivalent of `docker/stack.env`, and it belongs in the same
“not committed” category.

---

## Legacy installation

An installation that already manages its own schema and credentials must not initialize:

```dotenv
GHAT_INITDB=skip
```

`init` runs no scripts, and `web` / `mcp` neither create tables nor touch PATs.

A legacy installation therefore does these steps by hand:

```bash
# tables only; idempotent, and creates the database when it is missing
docker compose run --rm web rhosocial-github-actions-tracker-db init

# register the credential
docker compose exec web rhosocial-github-actions-tracker credential pat ensure \
  --name work --token <PAT> --probe-repo owner/repo
```

Initialization and these three deletion commands are different things: `db init` only performs
`CREATE DATABASE` when the database is missing and `CREATE TABLE IF NOT EXISTS`; the commands below
delete data and are unrelated to “first initialization”.

| Command | Effect |
|---|---|
| `rhosocial-github-actions-tracker-db reset --yes` | drop and rebuild every `ghat_*` table; credential rows go with them; the database remains |
| `rhosocial-github-actions-tracker-db drop --yes` | drop the whole database (refuses `--maintenance-db`) |
| `docker compose ... down -v` | in the bundled shape, deletes the data volume, which is dropping the database |

---

## The image

```text
registry.cn-shanghai.aliyuncs.com/vistart_public/github-actions-tracker:latest   # mainland China
docker.io/vistart/github-actions-tracker:latest                                  # everywhere else
```

One tag, `latest`: what main last published. Which build *that* was is answered from inside the
container by `build.revision` (`/readyz`, `ci_status`), stamped at build time. Pinning a deployment
still works — point `GHAT_IMAGE` at a registry address, then confirm with `build.revision`.

A dispatch at a commit is a deliberate re-publish — that is how a rollback works
(`gh workflow run images.yml --ref <older-sha>`). A run on a branch or a tag only builds: feature code
must not become what production pulls.

- Multi-arch: `linux/amd64`, `linux/arm64`.
- The image contains only the Python runtime, the service code and an entrypoint; **no database, no
  certificates, and no proxy / pip-mirror configuration**.
- The proxy is a **runtime** choice: users outside mainland China do not need it. A mainland user
  injects it in its own compose file or environment; see
  [runtime proxy](operations.md#runtime-proxy-mainland-china-only).
- Build it yourself: `docker buildx bake --load` for the current architecture,
  `docker buildx bake github-actions-tracker-all --push` for every platform.

### ARM64, including ARM-based NAS

The image is a real `linux/arm64` build, not an emulated one — the same manifest, two architectures, so
`docker pull` picks whichever the host is and nothing is translated at runtime. That makes a 64-bit ARM
NAS with a container manager a viable host: it is a small server that is already on, and CI status is
exactly the kind of thing you want answered from it.

Check the machine before planning around it:

```bash
uname -m                                    # aarch64 → the arm64 image
docker version --format '{{.Server.Arch}}'
docker pull docker.io/vistart/github-actions-tracker:latest       # resolves to the host's arch
docker image inspect docker.io/vistart/github-actions-tracker:latest --format '{{.Architecture}}'
```

Both of these families have provided container management on their 64-bit ARM models — **as examples,
not a compatibility list**: Synology's Container Manager on DSM 7.2 (DS223, DS423 and other `aarch64`
models; the low-end `j` series often ships without container support) and QNAP's Container Station on
QTS 5 (TS-233, TS-433 and other Cortex-A55 models). Model support moves with firmware, so the vendor's
own app centre is the authority: if it offers a container manager and `uname -m` says `aarch64`, this
image runs.

Two practical notes for a NAS:

- **Memory is the constraint, not CPU.** The service is two `serve` processes plus PostgreSQL; 2 GB is
  the comfortable floor for both in one box. If the NAS is the database host too, either give PostgreSQL
  a small `shared_buffers` or point `GHAT_DB_*` at a database somewhere else — the service does not care
  where the database is.
- **Do not put the log volume on a spinning disk you also use for backups.** The log tables are the
  biggest thing this service writes, and `db partitions --drop-before` is cheap only when the filesystem
  lets a table drop be a metadata operation.

## Ports and deployment shapes

| Port | Process | Meaning |
|---|---|---|
| 8000 | `serve all` / `serve rest` | REST API, plain HTTP, container-only by default |
| 18080 | `serve all` / `serve webhook` | GitHub webhook receiver; can terminate TLS |
| 8001 | `serve mcp` | MCP Streamable HTTP, plain HTTP |

`docker-compose.yml` splits the service into two long-running containers and one one-shot container:

- `web`: `serve all`, REST 8000 + webhook 18080; only 18080 is published.
- `mcp`: `serve mcp`, 8001; only 8001 is published.
- `init`: runs `./docker/init.d` on first boot (database, tables, PAT), then exits; `web` and `mcp`
  wait for it to succeed.

Both long-running containers use the same image and differ only in `command`. You can also start one
container directly with `docker run`, but you must mount the init.d directory; otherwise the first boot
fails explicitly instead of continuing uninitialized.

When several containers share one database, only one of them should initialize; the rest set
`GHAT_INITDB=skip`.

## Environment variables

`GHAT_PUBLIC_BASE_URL` is the external address used for the absolute action URLs MCP returns, and the
default prefix for the webhook callback URL.

| Variable | Default | Meaning |
|---|---|---|
| `GHAT_DB_HOST/PORT` | `localhost:5432` (`postgres:5432` in compose) | PostgreSQL coordinates |
| `GHAT_DB_USER/PASSWORD/NAME` | `root` / `password` / `test_db` | database account and name |
| `GHAT_DB_SSLMODE` | `prefer` | libpq sslmode |
| `GHAT_POOL_MIN/MAX` | `1` / `10` | pool; one connection per request in flight, long polls included; see [the pool](operations.md#the-pool-one-connection-per-request) |
| `GHAT_API_KEY` | empty | Bearer for REST / MCP; empty means no auth (warns at startup), mandatory on the public internet |
| `GHAT_REST_HOST/PORT` | `0.0.0.0:8000` | REST listener |
| `GHAT_MCP_HOST/PORT` | `0.0.0.0:8001` | MCP listener |
| `GHAT_WEBHOOK_HOST/PORT` | `0.0.0.0:18080` | webhook listener |
| *(webhook secret)* | — | **Not an environment variable.** Each repository stores its own secret in `ghat_repo_webhook_secrets`; create or rotate it with `webhook rotate-secret --name owner/repo`, which also pushes it to the hook. A repository without one is refused with `secret_not_configured` |
| `GHAT_WEBHOOK_URL` | empty | default callback URL for creating a webhook |
| `GHAT_PUBLIC_BASE_URL` | empty | absolute prefix for MCP action URLs |
| `GHAT_WEBHOOK_SSL_CERTFILE/KEYFILE` | empty | webhook fullchain and key paths; empty means plain HTTP |
| `GHAT_DEFAULT_EVENTS` | `workflow_run,workflow_job` | subscribed webhook events |
| `GHAT_WAIT_MAX_SECONDS` | `300` | ceiling for one `wait_for_run` call (`timeout_seconds` defaults to 45 seconds); reported `p50`/`p90` and `suggested_timeout_seconds` follow it |
| `GHAT_LOG_CAPTURE` | `1` | whether to capture step logs; `0/false/no/off` reads the merged job text instead |
| `GHAT_LOG_CAPTURE_WINDOW_MINUTES` | `60` | how long after completion a capture is still attempted. The value is **measured**, not promised by GitHub; a larger value only retrieves more |
| `GHAT_LOG_LEVEL` | `INFO` | log level; logs go to stdout/stderr |
| `GHAT_INITDB` | `auto` | whether first boot runs the init.d scripts: `auto` (when tables are missing) / `force` / `skip` |
| `GHAT_INITDB_DIR` | `/docker-entrypoint-init.d` | init.d directory; compose read-only mounts `./docker/init.d` |
| `GHAT_LEGACY` | `0` | `1` equals `GHAT_INITDB=skip` (legacy deployment) |
| `GHAT_BOOTSTRAP_PAT` | empty | fine-grained PAT injected on first boot, read by `20-pat.sh` |
| `GHAT_BOOTSTRAP_PAT_FILE` | empty | read the PAT from a file, so it stays out of `docker inspect` |
| `GHAT_BOOTSTRAP_PAT_NAME` | `default` | credential alias, shown as `pat/<name>` in the CLI |
| `GHAT_BOOTSTRAP_PROBE_REPO` | empty | `owner/repo` used to verify Actions write access |
| `GHAT_IMAGE` | published image | the image compose uses; set to `github-actions-tracker:local` for a local build |
| `GHAT_WEBHOOK_PORT_HOST` | `18080` | host-published webhook port (18080 in the container) |
| `GHAT_MCP_PORT_HOST` | `8001` | host-published MCP port (8001 in the container) |
| `GHAT_OAUTH_ISSUER` | empty | **Reserved**: issuer for an OAuth mode that is not available yet |
| `GHAT_RESOURCE_URL` | empty | the full URL this MCP server is published as, e.g. `https://tracker-mcp.example.com:8001/mcp`. **Required for a public MCP deployment**: it is both the resource identifier for an OAuth verifier, when one is configured, and the only basis for deriving the Host allowlist (DNS rebinding protection). Without it only a loopback `Host` passes and public requests receive `421 Invalid Host header` |
| `GHAT_MCP_ALLOWED_HOSTS` | empty | extra allowed `Host` values, comma-separated, `example.com:*` for a wildcard port. Defaults to the derived resource host plus loopback |
| `GHAT_MCP_ALLOWED_ORIGINS` | empty | extra allowed `Origin` values. Defaults to the derived resource origin; checked only when a request carries `Origin` |

## Credential shape

Only **static Bearer authentication** (`GHAT_API_KEY`) is provided today. MCP and REST share the same
key, and the CLI does not use it. `GHAT_RESOURCE_URL` is also the OAuth resource identifier, when a
verifier exists, but it is already a required variable for a public MCP deployment; the variable that
is still reserved is `GHAT_OAUTH_ISSUER` — this project ships no authorization server and no JWKS
source. Setting `GHAT_OAUTH_ISSUER` alone still starts the service in Bearer mode.

## TLS certificate directory

The image **ships no certificates**; the convention is to mount a directory at `/certs`:

```bash
mkdir -p docker/certs
cp <certificate directory>/fullchain.pem docker/certs/
cp <certificate directory>/privkey.pem  docker/certs/
```

Then point the environment at the in-container paths:

```dotenv
GHAT_WEBHOOK_SSL_CERTFILE=/certs/fullchain.pem
GHAT_WEBHOOK_SSL_KEYFILE=/certs/privkey.pem
```

`docker-compose.yml` already mounts `./docker/certs` read-only at `/certs`. After renewal, replace the
host files and restart `web` (`docker compose restart web`). If TLS terminates at a front proxy, set
these two variables to an explicit **empty string**, and `web` serves plain HTTP.
