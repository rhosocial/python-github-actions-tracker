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
| Aliyun | `registry.cn-shanghai.aliyuncs.com/vistart_public/citracker` | `ALIYUN_REGISTRY_USERNAME` / `ALIYUN_REGISTRY_PASSWORD` |
| Docker Hub | `docker.io/vistart/citracker` | `DOCKERHUB_REGISTRY_USERNAME` / `DOCKERHUB_REGISTRY_PASSWORD` |

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
deployment still works: point `CITRACKER_IMAGE` at a registry address and let
`build.revision` say what you pulled.

For a local check no registry is involved:
`CITRACKER_IMAGE=citracker:local docker compose build`, then
`CITRACKER_IMAGE=citracker:local docker compose up -d`.

## Replacing a PAT

1. Edit the token file the compose stack mounts read-only (or the
   `CITRACKER_BOOTSTRAP_PAT` value).
2. Re-run init: `docker compose --env-file docker/stack.env run --rm -e CITRACKER_INITDB=force init`.

Prefer `CITRACKER_BOOTSTRAP_PAT_FILE` over `_PAT`: an inline value is visible in
`docker inspect`, and a leaked token should be revoked at GitHub immediately.

## Legacy / manual mode

```bash
docker compose --env-file docker/stack.env run --rm -e CITRACKER_INITDB=skip init
docker compose exec web rhosocial-github-ci-tracker-db init   # tables only, idempotent
docker compose exec web rhosocial-github-ci-tracker credential pat ensure --name work --token <PAT>
```

## Database maintenance

```bash
docker compose exec web rhosocial-github-ci-tracker-db is-initialized
docker compose exec web rhosocial-github-ci-tracker-db stats
docker compose exec web rhosocial-github-ci-tracker-db usage
docker compose exec web rhosocial-github-ci-tracker-db usage --json
docker compose exec web rhosocial-github-ci-tracker-db cleanup --runs-before 90d --logs-before 365d
docker compose exec web rhosocial-github-ci-tracker-db partitions --list
docker compose exec web rhosocial-github-ci-tracker-db partitions --ensure 2
docker compose exec web rhosocial-github-ci-tracker-db partitions --drop-before 6m           # dry run
docker compose exec web rhosocial-github-ci-tracker-db partitions --drop-before 6m --drop-old
docker compose exec web rhosocial-github-ci-tracker-db deliveries --unhandled --limit 20
docker compose exec web rhosocial-github-ci-tracker-db cleanup --deliveries-before 30d
docker compose exec web rhosocial-github-ci-tracker-db reset --yes
docker compose exec web rhosocial-github-ci-tracker-db drop --yes
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

`partitions` is the other half of capacity: the two log tables are partitioned by
the UTC month their job finished, so **a month is a unit**. `--ensure N` creates
this month and the next N (a partitioned table starts with *no* partitions and
refuses a write it cannot place, so this is what keeps the first capture of every
month working); `--drop-before 6m` names every month wholly before the cutoff and
-- dry by default, as dropping is irreversible -- needs `--drop-old` to act. Both
tables are always handled as a pair, in one transaction, because the capture path
writes both and a month that exists on one table only is the state where the
write has already failed. `--list` shows that pair-wise view, including the
asymmetries.

Order matters when reclaiming space: drop partitions first (cheap, one metadata
operation), then consider `cleanup --runs-before` for the run rows themselves.
`cleanup --logs-before` still works but is deprecated on log tables -- it deletes
rows a partition would have dropped whole, and leaves the space for a vacuum.

Remember: PostgreSQL does not return space to the filesystem on `DELETE` alone.
With the log tables partitioned, dropping a month is one `DROP TABLE` and needs
no vacuum at all.

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
