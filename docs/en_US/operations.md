# Operations

## Probes

| Path | Question | Check | On failure |
|---|---|---|---|
| `/livez` | is the process alive | none (zero I/O) | restart the container |
| `/readyz` | can this instance serve | one database round trip | drain it, do not restart |
| `/health`, `/healthz` | aliases of `/readyz` | same | same |

Both carry the build stamp (`build.revision`, `build.number`, `built_at`), which is
the one thing that answers "is the deployment the commit I pushed?" — `version`
is a source constant and cannot tell two images apart. A `null` build means the
image was not stamped (locally built), not that it is old.

## Stop, restart, upgrade

| Action | Containers | init runs again | Data |
|---|---|---|---|
| `stop` / `start` | kept | yes, skips when tables exist | unchanged |
| `restart web` | kept | no (`INITDB=skip`) | unchanged |
| `down` → `up -d` | recreated | yes, skips | unchanged (bundled keeps its volume) |
| `down -v` | deleted | yes, creates for real | **data lost** |

Upgrading:

```bash
docker compose --env-file docker/stack.env pull
docker compose --env-file docker/stack.env up -d
docker compose --env-file docker/stack.env ps
curl -k https://127.0.0.1:18080/readyz
```

**A schema change means restarting `web` and `mcp`**: the pool runs server-side
prepared statements, and DDL from another process leaves cached plans that fail
with `cached plan must not change result type` until those connections are
replaced. `up -d` recreates the containers and is not affected.

## Rebuilding the image is always manual

A push to main runs the tests (`test.yml`) and never starts `images.yml`:
publishing a multi-arch image is a deliberate act, not a side effect of merging.
When a new image is needed -- after a change to `src/**`, `Dockerfile`,
`docker/**`, `pyproject.toml`, or after a sibling library merged, because the
image pins the siblings at **build** time with `pip install git+...@<ref>`:

```bash
gh workflow run images.yml --ref main
gh run watch    # multi-arch builds take roughly 7-8 minutes
docker compose --env-file docker/stack.env pull
docker compose --env-file docker/stack.env up -d
```

### Two registries, one build, one tag

`images.yml` pushes the **same** multi-arch build to both destinations, tagged
`latest` — one manifest, so the two `latest` tags are the same bytes:

| Destination | Image | Secrets (Settings -> Secrets and variables -> Actions) |
|---|---|---|
| Aliyun | `registry.cn-shanghai.aliyuncs.com/vistart_public/github-actions-tracker` | `ALIYUN_REGISTRY_USERNAME` / `ALIYUN_REGISTRY_PASSWORD` |
| Docker Hub | `docker.io/vistart/github-actions-tracker` | `DOCKERHUB_REGISTRY_USERNAME` / `DOCKERHUB_REGISTRY_PASSWORD` |

Each destination is independent: a missing pair is **skipped** (the run summary
says which), and the other still publishes. With neither pair present the
workflow still builds, so a build failure remains a failure, but pushes nothing.
A run on a branch or a tag **publishes nothing** and only proves the build --
feature code must not become what production pulls. A dispatch at a commit is
honoured, which is how a rollback works (`gh workflow run images.yml --ref
<older-sha>`): that commit's `latest` becomes what production pulls again.

"Which build is running" no longer comes from the tag — it comes from
`build.revision`, which `/readyz` and `ci_status` report from inside the
container, and from the stamp the workflow bakes in at build time. Pinning a
deployment still works: point `GHAT_IMAGE` at a registry address and let
`build.revision` say what you pulled.

For a local check no registry is involved:
`GHAT_IMAGE=github-actions-tracker:local docker compose build`, then
`GHAT_IMAGE=github-actions-tracker:local docker compose up -d`.

## Replacing a PAT

1. Edit the token file the compose stack mounts read-only (or the
   `GHAT_BOOTSTRAP_PAT` value).
2. Re-run init: `docker compose --env-file docker/stack.env run --rm -e GHAT_INITDB=force init`.

Prefer `GHAT_BOOTSTRAP_PAT_FILE` over `_PAT`: an inline value is visible in
`docker inspect`, and a leaked token should be revoked at GitHub immediately.

## Legacy / manual mode

```bash
docker compose --env-file docker/stack.env run --rm -e GHAT_INITDB=skip init
docker compose exec web rhosocial-github-actions-tracker-db init   # tables only, idempotent
docker compose exec web rhosocial-github-actions-tracker credential pat ensure --name work --token <PAT>
```

## Database maintenance

```bash
docker compose exec web rhosocial-github-actions-tracker-db is-initialized
docker compose exec web rhosocial-github-actions-tracker-db stats
docker compose exec web rhosocial-github-actions-tracker-db usage
docker compose exec web rhosocial-github-actions-tracker-db usage --json
docker compose exec web rhosocial-github-actions-tracker-db cleanup --runs-before 90d --logs-before 365d
docker compose exec web rhosocial-github-actions-tracker-db partitions --list
docker compose exec web rhosocial-github-actions-tracker-db partitions --ensure 2
docker compose exec web rhosocial-github-actions-tracker-db partitions --drop-before 6m           # dry run
docker compose exec web rhosocial-github-actions-tracker-db partitions --drop-before 6m --drop-old
docker compose exec web rhosocial-github-actions-tracker-db deliveries --unhandled --limit 20
docker compose exec web rhosocial-github-actions-tracker-db cleanup --deliveries-before 30d
docker compose exec web rhosocial-github-actions-tracker-db reset --yes
docker compose exec web rhosocial-github-actions-tracker-db drop --yes
```

`stats` counts rows; `usage` weighs the database. They answer different
questions and the answers live in different places: a row count cannot be
turned into bytes, because PostgreSQL measures storage the engine's way (TOAST,
indexes, free space) and the introspector is the only party that knows it. Row
counts are the planner's estimate rather than a `COUNT(*)`, so a table nobody
has analyzed since it was created prints no count at all — an absent number,
never a zero that reads as "this table is empty". `--json` emits the same
report the `ci_status.storage` field carries.

`init` also enables `pg_trgm` (`CREATE EXTENSION IF NOT EXISTS pg_trgm`).
A cluster ships the extension as *available* and a database uses it only once
it is *installed*: those are two different lists, and the gap between them is
where a deployment fails with `operator error: type trigram does not exist`
instead of degrading. Enabling is idempotent and never blocks a boot — a server
that refuses the extension (a managed plan without it, a privilege we lack)
still gets its tables, and `usage` reports `trgm_installed: false` so the
missing accelerator is a visible state rather than a surprise.

`partitions` is the other half of capacity. The two log tables are partitioned by
the UTC month in which the execution was **triggered** (`attempt_created_at`,
copied from `ghat_run_attempts.github_created_at`), so a month is a unit.
`--ensure N` creates this month and the next N. A partitioned table starts with
no partitions and refuses a write it cannot place, so this is what keeps the
first capture of every month working. `--drop-before 6m` names every month
wholly before the cutoff; it is a dry run by default, because dropping is
irreversible, and needs `--drop-old` to act. The two tables are handled as a
pair: a month is created for both before either is written, and `--list` shows
that pair-wise view, including the asymmetries.

Order matters when reclaiming space: drop partitions first (one metadata
operation), then consider `cleanup --runs-before` for the run rows themselves.
`cleanup --logs-before` still works but is deprecated on log tables -- it deletes
rows a partition would have dropped whole, and leaves the space for a vacuum.

Remember: PostgreSQL does not return space to the filesystem on `DELETE` alone.
With the log tables partitioned, dropping a month is one `DROP TABLE` and needs
no vacuum at all.

### The pool: one connection per request

A connection scope is per request (REST middleware and the MCP wrapper each open
one), so:

- every request in flight holds one of the `GHAT_POOL_MAX` slots, default 10;
- **long polls count too**: `wait_for_run` holds its connection for the whole
  wait, so a 300-second wait is a 300-second hold;
- `GHAT_POOL_MAX` must cover “concurrent long polls + concurrent normal
  requests”. Otherwise late arrivals queue on `acquire` and then fail after 30
  seconds with `Failed to acquire connection within 30.0 seconds`.

| `Pool stats` | Meaning | Action |
|---|---|---|
| `in_use` near `max_size` | saturation: concurrency exceeds the pool | raise `GHAT_POOL_MAX`, or reduce concurrent long polls |
| `available=0, in_use=0` | a pool permit leak | restart the container; fixed in `python-activerecord`'s `a55ebbd9` |

## Runtime proxy (mainland China only)

The image carries no proxy configuration. A deployment in mainland China injects
it at runtime:

```yaml
services:
  web:
    environment:
      HTTP_PROXY: http://proxy:7897
      HTTPS_PROXY: http://proxy:7897
      NO_PROXY: 127.0.0.1,localhost,::1
```

## Verify

```bash
curl -fsS https://<host>:18080/readyz     # one database round trip; 503 + checks when it fails
curl -fsS https://<host>:18080/livez      # the process, no database
docker compose logs -f init web mcp
docker compose exec web rhosocial-github-actions-tracker credential pat list
```

A question asked through an MCP client is one more probe: `ci_status` answers
about the service, the database and the pool without spending a GitHub call.

### The delivery ledger, and the push channel's health

Every delivery that reaches the receiver writes one row to
`ghat_webhook_delivery` — processed, ignored, or rejected for a bad signature,
it lands in the same table. `workflow_run` and `workflow_job` are two
independent streams on GitHub's side (different payloads, different lifetimes),
so the ledger carries an `event_type` and reports by stream. This is what
answers "is the push channel alive?" — and it answers it in three
distinguishable ways, which is the part worth knowing before there is a problem:

| what you see | what it means | what to do |
|---|---|---|
| no new rows at all | the request never reached the receiver | the URL, or the router's port mapping |
| `secret_not_configured` | it arrived for a repository that has no secret | `webhook rotate-secret --name owner/repo` |
| `signature_failed` | it arrived, and that repository's secret does not match | rotate it again, or fix the value in GitHub |
| `processed` | the channel works end to end | nothing |

```bash
# what the push channel has received lately
docker compose exec web rhosocial-github-actions-tracker-db stats
```

`ci_status`'s `health` field is the same answer as a structure, with names a
caller can branch on:

```json
{
  "ledger_available": true,
  "runs_tracked": 12,
  "last_webhook_at": "2026-11-05T07:37:33+00:00",
  "webhooks_in_last_hour": 13,
  "push_channel": "active",
  "alerts": [],
  "streams": {
    "workflow_run": {"last_at": "...", "in_last_hour": 4, "deliveries_total": 4,
                     "redelivered": 0, "unhandled": 0, "unhandled_reasons": {},
                     "write_failures": 0, "last_write_failure": null},
    "workflow_job": {"last_at": "...", "in_last_hour": 9, "deliveries_total": 9,
                     "redelivered": 0, "unhandled": 0, "unhandled_reasons": {},
                     "write_failures": 0, "last_write_failure": null}
  }
}
```

(The numbers are one live window, not a target: counters are process-local, so
they restart with the container.)

Reading it:

- `last_at` of `null` means **nothing has arrived**. That is normal for a fresh
  deployment and for a repository nobody has run anything in; a default route
  that treats it as a fault will page on a quiet repository.
- `redelivered` counts a second arrival of the same `X-GitHub-Delivery` value —
  someone pressed "Redeliver", or GitHub retried. Redelivery is idempotent by
  design: the same delivery applied twice leaves the same rows, so it costs
  time, never a double count.
- `unhandled` counts arrivals that changed no state: an unsubscribed repository,
  a `ping`, an event recorded but not acted on. Non-zero is not a fault, but a
  number that keeps growing usually means the subscription scope and the
  delivery scope disagree.
- `unhandled_reasons` is the breakdown of the last 24 hours; `unhandled` alone
  is a single number with nothing to act on.
- **`write_failures` and `alerts` are the ones to act on.**
  `ignored` / `unsupported_event` / `invalid_json` / `signature_failed` are
  "declined on purpose" and belong in `unhandled_reasons`. A *failure* is
  different: an `IntegrityError`, or a delivery that never reached the database
  at all, means a run, a job or a step is missing from the rows. The next read
  repairs it — GitHub is the source of truth — so this is an alert about the
  push channel, not data loss.

