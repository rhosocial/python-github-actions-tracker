---
name: ops-deploy-image
description: Deploy the tracker from the published image by asking one question at a time - PostgreSQL source, DB TLS, webhook certificate, GitHub PAT, MCP exposure - validating each answer with a standalone script before anything is started
license: Apache-2.0
compatibility: opencode
metadata:
  category: operations
  level: intermediate
  audience: operators
---

# Deploy the tracker from the image

Walks an operator through a first deployment: **one question at a time**, each answer
validated by a script before the next question is asked, and nothing started until every
step has passed. The order is deliberate — the database is the only part that cannot be
changed later without a rebuild, and the certificate and the token are the two things that
block GitHub from ever delivering a webhook.

Use with `ops-deploy`'s siblings: `docs/deployment.md` for the shapes, `docs/operations.md`
for day-two work.

## Rules of the flow

1. **Ask one question, then wait.** Do not batch questions, and do not guess an answer the
   operator has not given. A wrong guess here is a deployment that half-works.
2. **Record every answer** in `docker/stack.env`-shaped form as you go, so the last step is
   a `up -d` rather than a re-interview.
3. **Validate before moving on.** Each step names a script; a red result means fix it and
   re-run, not continue and hope.
4. **The two things only the operator can do** — minting a fine-grained PAT, and getting a
   certificate from a CA — are marked "wait for the operator". Stop there, say so plainly,
   and continue when they say it is done.
5. **Never write a secret to a file that will be committed.** PATs go to
   `docker/secrets/github-actions-tracker-pat` (git-ignored) and are referenced by `--token-file`.

## Step 0 — Pick the runtime, then check reachability

Ask the operator which of the two they want; every later step is the same, only the
"start it" step differs.

**A. Container** (what the image is built for): needs Docker, and the registry it will
pull from must be reachable. The two registries carry the same build — if Docker Hub is
slow from this network, pull the Aliyun one instead:

```bash
docker version >/dev/null && echo "docker ok"
docker pull registry.cn-shanghai.aliyuncs.com/vistart_public/github-actions-tracker:latest >/dev/null \
  && echo "aliyun reachable"
docker pull docker.io/vistart/github-actions-tracker:latest >/dev/null && echo "docker hub reachable"
```

Record which registry was used; it becomes `GHAT_IMAGE`.

**B. Native Python** (no Docker at all): needs Python 3.10 or newer (3.15 is what CI and
the image run), pip, and a route to the package — PyPI, or a local checkout to install
from:

```bash
python --version                       # 3.10+; 3.15 is what CI runs
pip install rhosocial-github-actions-tracker
rhosocial-github-actions-tracker --help     # both CLIs arrive from the same wheel
rhosocial-github-actions-tracker-db --help
```

Both entry points read the same `GHAT_*` environment variables the compose file would
have carried, so the answers collected in steps 1–5 feed either runtime unchanged.
Windows works: the CLI installs the selector event loop psycopg's async mode needs on
that platform. Validate the environment:

```bash
python .claude/skills/ops-deploy-image/scripts/check_native_env.py
```

## Step 1 — Where is PostgreSQL?

Ask which of the two the operator wants, then record it.

**A. Self-hosted from the image.** The repository publishes a PostgreSQL 18 image with the
extensions this service needs:
`registry.cn-shanghai.aliyuncs.com/vistart_public/postgres-exts:18`.
Details: <https://github.com/vistart/Dockerfiles/tree/postgres>. Pair it with
`docker-compose.bundled.yml`, which puts `postgres:18` beside the app.

**B. Operator-supplied.** Either a server they run themselves, or a managed one — Supabase
and friends. Their choice is fine, and it is also where the extension question gets
answered honestly: **the service needs `pg_trgm`** (the trigram index over the log tables)
and `plpgsql`. A managed instance usually has both; nothing but a check proves it.

Either way run:

```bash
python .claude/skills/ops-deploy-image/scripts/check_postgres.py \
  --host <host> --port <port> --user <user> --password <password> --database <db>
```

What it accepts: a reachable server, PostgreSQL 16 or newer, `pg_trgm` present, and the
ability to create the database (so `init.d` can do its job). What it reports as a
**warning rather than a failure**: no `CREATE DATABASE` right — that is normal on most
managed services. In that case the operator creates the database by hand, sets
`GHAT_DB_NAME` to it, and the schema is created on first boot.

Record: `GHAT_DB_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_NAME` / `_SSLMODE`.

## Step 2 — Does the database traffic cross a public network?

Ask plainly; the answer decides whether `sslmode` stays `prefer` or has to become
`verify-full`. The default (`prefer`) encrypts when the server offers it and does not
prove the server is who it claims — which is fine over a private link and not fine over the
internet.

- **No** (same host, or a private network): `GHAT_DB_SSLMODE=prefer`. Move on.
- **Yes** (Supabase, a database in another datacentre, anything public): `verify-full` plus
  the CA bundle, or the deployment is one stray router away from a credential in the clear.
  Re-run with the CA file, and give `--host` as the name the CA issued for — libpq does the
  hostname check on connect, so a connection that succeeds is the check:

```bash
python .claude/skills/ops-deploy-image/scripts/check_postgres.py \
  --host <the name the CA issued for> --port <port> --user <user> \
  --password-stdin --database <db> --sslmode verify-full --sslrootcert <ca.pem>
```

Three things a real Supabase instance taught us, all now handled:

- **The pooler presents its own private CA** (`Supabase Intermediate 2021 CA`), so
  `verify-full` with *any* public trust store fails. Use the provider's CA bundle, or
  `require`, and say which one the operator chose.
- **On Windows, `verify-full` without a CA file looks for `~/postgresql/root.crt`** and
  fails on a file nobody created; `--sslrootcert system` is the fix there.
- **A pooler terminates TLS**: the server can report `pg_stat_ssl.ssl = false` while libpq
  is encrypting the session. That is the pooler's architecture, not a broken setting —
  the script reports it as `tls_terminates_at_a_pooler` rather than "not encrypted".

When `verify-full` is refused, the script reads the certificate the server actually
presents (PostgreSQL needs its own eight-byte `SSLRequest` first, so a plain TLS connect
just says "wrong version number") and reports its subject, SAN, coverage and issuer — so
"certificate verify failed" becomes an instruction instead of a dead end.

## Step 3 — Certificate for the webhook listener

Ask where the listener will be reachable from, and whether they already have a certificate
for that name. Then:

```bash
python .claude/skills/ops-deploy-image/scripts/check_certificate.py \
  --certfile docker/certs/fullchain.pem \
  --keyfile docker/certs/privkey.pem \
  --hostname <the public hostname>
```

The pair is checked as a pair — key against certificate, the name, the validity window, the
chain to a trusted root, and the algorithm. The chain check uses the machine's trust store
plus `--cafile`, and a Let's Encrypt leaf verified against it in practice; a self-signed
pair reports the root, which is the honest answer for a listener only GitHub calls. The
findings map 1:1 to the codes the service's own `cert` command reports, so what the script
says and what the boot says agree.

Where no trust store exists (a Windows box without one, a slim image), the script says
`chain_check_unavailable` and explains that the chain could not be checked — it does not
report "no path to a trusted root", which is a different claim.

A self-signed certificate passes every check except `cert_chain_unverified`. That is
acceptable for a listener only GitHub can reach — GitHub does not verify the certificate
when it calls you — and not acceptable if humans will open the URL in a browser. Say which
of the two this is; do not decide for the operator.

Record: `GHAT_WEBHOOK_SSL_CERTFILE` / `_KEYFILE` / `_HOSTNAME`.

**Why this step is not optional:** GitHub's webhook settings require an HTTPS callback URL.
It can be switched to HTTP in the repository's webhook settings, and doing so means the
delivery that starts a workflow travels in the clear, with the secret in every request
header. The service warns loudly and still boots — the check is advisory by design — but
this step is where the operator is told, in words, what the trade is.

## Step 4 — The fine-grained PAT (wait for the operator)

**This is the operator's to create; nothing in this session can do it for them.** Give them
the link and the exact permission list, then stop:

- Create at <https://github.com/settings/personal-access-tokens/new> (fine-grained, beta UI
  is fine). Alternatively <https://github.com/settings/tokens?type=beta> lists existing ones
  so they can reuse a token they already own.
- Scopes to select on the token (per repository, for each repository they want tracked):
  - **Actions: Read** — runs, jobs, logs.
  - **Actions: Write** — trigger, re-run, cancel.
  - **Contents: Read** — without it a trigger that must resolve a ref's head fails with
    `invalid_argument` instead of dispatching, which is the correct failure but an
    unpleasant surprise.
  - **Administration: Read and write (repository hooks)** — only if the operator wants the
    CLI to create and manage the webhook itself.
  - **Metadata: Read** is automatic and required.
- Expiration: their call. A token that expires takes the whole service down with it, so a
  long expiry plus a calendar reminder beats a short one.

When the operator says it is done, save the token and validate it:

```bash
printf '%s' '<token>' > docker/secrets/github-actions-tracker-pat    # git-ignored
python .claude/skills/ops-deploy-image/scripts/check_pat.py \
  --token "$(cat docker/secrets/github-actions-tracker-pat)" --repo owner/repo
```

The script separates **verified** (the token works and each permission was observed doing
its job) from **unverifiable** — Actions *write* cannot be proven without spending CI
minutes, so it reports it as unproven and says so, rather than claiming a green it does not
have. A token that cannot read at all is a stop; a token that can only read is a warning
that lists exactly which tool will refuse.

Record: `GHAT_BOOTSTRAP_PAT_FILE=docker/secrets/github-actions-tracker-pat` (relative to the
compose file), `GHAT_BOOTSTRAP_PAT_NAME` (default `default`), and
`GHAT_BOOTSTRAP_PROBE_REPO` if they want the boot to verify Actions write against a
known repository.

## Step 5 — How will MCP be reached?

Two answers, two different deployments, and the second is the one people get wrong because
it works fine on the day they test it.

- **Local or on a private network** (`127.0.0.1`, a LAN address behind a firewall): no
  certificate, no API key. Leave `GHAT_API_KEY` empty — the listener warns at startup
  and serves, which is the honest default for a local tool.
- **Public, or shared inside an organisation**: set `GHAT_API_KEY` (every MCP and REST
  call then needs `Authorization: Bearer <key>`) and put TLS in front of the MCP listener
  the same way the webhook listener has it. An open MCP endpoint is an open `trigger_workflow`
  — every caller spends *your* CI minutes.

Ask; do not infer from the fact that the port is bound to `0.0.0.0`. Record
`GHAT_API_KEY`, and for the shared case the certificate paths once step 3 produced
them.

## Step 6 — Start it, then prove it started

**A. Container.**

```bash
docker compose --env-file docker/stack.env up -d
python .claude/skills/ops-deploy-image/scripts/check_deployment.py \
  --base-url http://127.0.0.1:8001 --api-key "<GHAT_API_KEY>" \
  --webhook-url http://127.0.0.1:18080 --expect-revision <short sha of the image>
```

**B. Native Python.** There is no `init.d` runner here: the same two operations are two
CLI calls, and the long-running parts are two foreground processes (each in its own
terminal, or under your service manager — systemd, NSSM, Task Scheduler, whatever the
platform offers):

```bash
# 1) schema — creates the database too when the user may; --no-create-database
#    for managed services where it may not:
rhosocial-github-actions-tracker-db init

# 2) credential — what 20-pat.sh did in the container; token from a file, so it
#    never reaches the process list:
rhosocial-github-actions-tracker credential pat ensure \
  --name default --token-file docker/secrets/github-actions-tracker-pat --probe-repo owner/repo

# 3) the two long-running processes:
rhosocial-github-actions-tracker serve all    # REST :8000 + webhook :18080
rhosocial-github-actions-tracker serve mcp    # MCP :8001, its own process
```

Then the same script as path A. It cannot tell a container from a native process, and
that is the point: the probes are about the service, not the packaging. The one
difference worth noticing: on the native path `/metrics`, `/readyz` and the MCP endpoint
are only up while the processes run — there is no restart policy to bring them back, so
the service manager is part of the deployment, not an optional extra.

The script checks, in order: `/livez` (zero I/O, must answer), which scheme the MCP
listener actually answers on, `/readyz` (one database round trip plus the schema state),
`build.revision` against the image that was pulled, `/metrics` answering Prometheus text
without touching the database, and a real MCP handshake (`initialize` then `tools/list`)
over the same socket a model would use. A passing probe with a broken tool loop cannot
hide, which is why the handshake is last.

It reports the MCP listener's scheme rather than failing on it: a listener behind TLS
and a listener on plain HTTP are both "reachable", and the second is a finding for a
public deployment, not for a local one. Read the report; do not read only the exit code.

Then the last mile, which no script can do: point a repository's webhook at
`https://<hostname>:18080/webhooks/github` with the same secret, and watch the delivery
land in `ci_status.health.streams`. A deployment whose first webhook has not arrived is
not finished, however green its probes are.

## Done means

- every script exited `0`, and the ones that warned did so in words the operator has read;
- the whole configuration lives in one place — `docker/stack.env` on the container path,
  an environment file or service unit on the native path — and no secret that belongs in
  `docker/secrets/` (or a token file) sits anywhere else;
- `/readyz` reports the revision of the build that was pulled or installed, not an older
  one;
- one webhook delivery has been received, or the operator has said the push channel will
  be armed later.

## If it went wrong

| Symptom | First thing to check |
|---|---|
| `init` exited non-zero | its log; the database user usually cannot create the database |
| containers `Up` but every request 500s | `readyz` says `database` — the credentials or the host, not the app |
| native: `serve` starts, then every request 500s | same answer: `readyz` says `database`; the env vars the two processes see must match what `db init` was given |
| `pool` shows `available: 0` with timeouts | restart the processes; the hint naming lost permits is about the process, not the data |
| webhook never arrives | the URL scheme (must be https), the secret match, and whether the repo is subscribed |

Rollback depends on the path, and both keep the database untouched (the schema is never
altered in place):

- **Container**: point `GHAT_IMAGE` at an older build and `up -d`. `sha-` tags no
  longer exist, so "an older build" means the registry digest of a previous `latest`
  (record it when you publish), or simply a re-run of the publish workflow at the older
  commit.
- **Native**: `pip install rhosocial-github-actions-tracker==<older version>` (or check out the
  older commit and install from it), then restart the two `serve` processes.
