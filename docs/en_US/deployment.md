# Deployment

## Shape one: web + mcp, external PostgreSQL

```bash
cp docker/stack.env.example docker/stack.env
mkdir -p docker/certs && cp <fullchain.pem> <privkey.pem> docker/certs/
docker compose --env-file docker/stack.env up -d
curl -k https://127.0.0.1:18080/readyz
```

`docker-compose.yml` defines three services: `init` (once, runs `./docker/init.d`
and exits), `web` (`serve all`, REST 8000 + webhook 18080) and `mcp`
(`serve mcp`, 8001). `web` and `mcp` start only after `init` succeeded.

## Shape two: bundled `vistart/postgres-exts:18`

```bash
docker compose -f docker-compose.yml -f docker-compose.bundled.yml --env-file docker/stack.env up -d
```

One database volume; `down` keeps it, `down -v` deletes it.

## First boot (`init.d`)

`10-schema.sh` creates the database and the tables (`CREATE DATABASE` +
`CREATE TABLE IF NOT EXISTS`), `20-pat.sh` ensures the credential. Both are
idempotent; **initialisation never clears existing data**.

Startup order is guaranteed by `depends_on`; the database does not have to sit
next to the application.

## Four decisions before you deploy

The skill `ops-deploy-image` walks through these one question at a time and runs a script
for each; the short version is here.

### 1. Where PostgreSQL comes from

Two shapes, and the second is where the extension question gets answered:

- **Self-hosted**, an image that already exists on Docker Hub:
  `vistart/postgres-exts:18` (build recipes:
  <https://github.com/vistart/Dockerfiles/tree/postgres>). **Mainland China users**
  should use the Alibaba Cloud mirror instead:
  `registry.cn-shanghai.aliyuncs.com/vistart_public/postgres-exts:18` — the same
  image under the same digest. Pair it with `docker-compose.bundled.yml`, which
  reads `${GHAT_DB_IMAGE:-vistart/postgres-exts:18}`, so swapping registries is
  one line in `stack.env` rather than an edit to the compose file.
- **Yours or a cloud provider's** — a server you run, or Supabase and friends. Both work;
  what differs is whether `pg_trgm` is available to you. The log tables' trigram index
  needs it, and most managed services have it, but only a check proves it.

Either way, the service asks PostgreSQL 16 or newer, `pg_trgm`, and `plpgsql`. A managed
instance that will not let you `CREATE DATABASE` is fine: create the database by hand and
the schema still arrives on first boot.

### 2. Is the database connection over a public network?

`GHAT_DB_SSLMODE=prefer` (the default) encrypts when the server offers it and does
not prove the server is who it claims. That is fine on a private link. Across the internet
— any managed database, anything in another datacentre — use `verify-full` with the CA
bundle and a host that is a *name* the certificate is issued for. An IP address cannot be
verified this way; say so honestly rather than shipping a configuration that looks
verified and is not.

Three things measured against a real managed instance, worth knowing before you try:

- **A provider's pooler often uses its own CA.** Supabase's pooler presents a certificate
  from `Supabase Intermediate 2021 CA`, so `verify-full` with a public trust store —
  including the machine's own — cannot verify it. The choices are the provider's CA bundle
  (`verify-full` + `sslrootcert`) or `require` (encrypted, server unauthenticated), and the
  second is what `prefer` degrades into anyway.
- **On Windows, libpq's `verify-full` without a CA file looks for
  `~/postgresql/root.crt`** and fails on a file nobody created. `sslrootcert=system` is
  the fix if you want the OS store.
- **A pooler terminates TLS.** The server can report `pg_stat_ssl.ssl = false` while the
  session is encrypted: the pooler handled TLS on the client side and reconnected to the
  database in the clear. `verify-full` is then verifying *the pooler's* certificate, which
  is the honest boundary of what the setting proves.

### 3. The webhook listener's certificate, and the token that will use it

GitHub's webhook settings require an **HTTPS** callback URL. It can be switched to HTTP in
the repository's settings, and doing so sends every delivery — secret header included —
in the clear, so: mount a certificate for the public name and keep HTTPS on. The service
checks the pair before it binds it and **warns without blocking** (see below); a
self-signed pair is acceptable for a listener only GitHub can reach, and not acceptable if
a human will open the URL in a browser.

The GitHub **fine-grained PAT is the operator's to create**; nothing here can mint one.
Create at <https://github.com/settings/personal-access-tokens/new> (fine-grained), and see
<https://github.com/settings/tokens?type=beta> for tokens you already own. Per tracked
repository, select:

| Permission | Why |
|---|---|
| Actions: Read | runs, jobs, logs |
| Actions: Write | trigger, re-run, cancel |
| Contents: Read | resolving a ref's head when `commit_sha` is not passed |
| Administration: Read and write, repository hooks | only if the CLI will manage the webhook |
| Metadata: Read | automatic and required |

Inject it from a file (`GHAT_BOOTSTRAP_PAT_FILE`, mounted read-only) so the token
never reaches `docker inspect` or a process list, then verify it with the skill's
`check_pat.py`: reads are observed, Actions *write* stays unproven — proving it means
spending CI minutes, and a green check that lies is worse than an honest gap.

### 4. Who can reach the MCP listener

- **Local or on a private network**: no certificate, no API key. Leave
  `GHAT_API_KEY` empty; the listener warns at startup and serves.
- **Public, or shared inside an organisation**: set `GHAT_API_KEY` — every MCP and
  REST call then needs `Authorization: Bearer <key>` — and put TLS in front of port 8001
  the same way the webhook listener has it. An open MCP endpoint is an open
  `trigger_workflow`: every caller spends **your** CI minutes.

## TLS

TLS material lives outside the image: mount a directory at `/certs` and point
`GHAT_WEBHOOK_SSL_CERTFILE` / `_KEYFILE` at the two files inside it. GitHub
requires an HTTPS callback for webhooks.

Each listener terminates TLS on its own name, so a deployment that gives REST
and MCP hostnames of their own can encrypt them too — they carry the API key and
one of them can trigger CI, which plain HTTP does not suit. The three pairs are
`GHAT_WEBHOOK_SSL_*`, `GHAT_REST_SSL_*` and `GHAT_MCP_SSL_*`; the certificate
file is the same one in all three slots in the common case, because a
multi-name certificate covers every hostname the deployment publishes.

Give `certfile` the **full chain** (leaf first, then intermediates): a client
should not have to fetch the middle itself.

### A public MCP hostname is allowlisted by name

`GHAT_RESOURCE_URL` is the URL this MCP server is published as. It feeds two
things: the allowlist the SDK enforces on `Host` and `Origin` (a DNS-rebinding
guard, and loopback-only until someone fills it), and the OAuth `resource`
identifier when a verifier is configured. Without it, a public MCP deployment
answers **`421 Invalid Host header`** on every request while `/readyz` stays
green — a failure that logs on neither side. `GHAT_MCP_ALLOWED_HOSTS` and
`GHAT_MCP_ALLOWED_ORIGINS` override the derivation for reverse proxies and
multi-entry deployments.

### GitHub will not deliver to an IPv6-only hostname

Measured, not assumed: a hook pointed at a pure-AAAA public service failed with
`failed to connect to host` in 0.01s, while the same address answered a client
on the public internet. So the webhook's name needs **both** an `A` record and an
`AAAA` record; the REST and MCP names can be AAAA-only if nothing else needs
IPv4 for them. This decides the DNS, not the code — and it decides it before the
first hook is registered, because a hook that cannot be delivered to is a hook
that looks broken.

### The certificate is checked before a boot binds it

`cert` (and every startup) reads the pair and reports what it found. All
of it is **advisory**: nothing here stops a boot, because a check that can is
worth less than the reason it would have given.

| code | what it means | what to do |
|---|---|---|
| `cert_unreadable` | the file is missing, empty, or unreadable | uvicorn will fail to bind; fix the path first |
| `cert_not_a_certificate` | neither PEM nor DER — often a key or a CSR in that slot | point at the certificate |
| `key_unreadable` / `key_not_a_key` | the key file is absent or unparseable; an *encrypted* key says so by name | decrypt it as part of the deployment |
| `key_mismatch` | the key and the certificate carry different public keys | the commonest accident: a renewed certificate with the old key |
| `cert_expired` / `cert_not_yet_valid` | outside the validity window (`not_yet_valid` names the clock) | renew, or check the machine's time |
| `cert_expiring` | within `GHAT_WEBHOOK_SSL_EXPIRY_WARN_DAYS` (default 30) | renew on your schedule |
| `cert_hostname_mismatch` | the names the certificate covers do not include the configured host | issue for the host, or set `GHAT_WEBHOOK_SSL_HOSTNAME` |
| `cert_chain_unverified` | no path to a trusted root — the wording separates self-signed from a missing intermediate | serve the full chain. The check builds it out of the intermediates in the file, so a chain ending in a *cross-signed* root verifies against the parent the machine already trusts; setting `GHAT_WEBHOOK_SSL_CAFILE` is the workaround for a deployment that cannot wait for that, at the cost of trusting the served root as its own anchor |
| `cert_revoked` / `cert_revocation_unknown` | a responder said revoked / could not be reached | reissue; `unknown` never blocks |
| `cert_weak_algorithm` | RSA under 2048, ECC under 256, or a SHA-1 signature | renew; this one is a note, not a warning |

Hostname checking needs a hostname: `GHAT_WEBHOOK_SSL_HOSTNAME` first, then
the host in `GHAT_PUBLIC_BASE_URL`, then `GHAT_WEBHOOK_URL`. With none
of the three, the check is skipped rather than guessing one.

Revocation is off by default — it is the only check that touches the network
from the startup path. `GHAT_WEBHOOK_SSL_CRLFILE` reads a local list;
`GHAT_WEBHOOK_SSL_CHECK_OCSP=1` asks the certificate's responder with a
3-second bound and soft-fails.

```bash
rhosocial-github-actions-tracker cert             # human-readable
rhosocial-github-actions-tracker cert --json      # for a rotation script
```

The exit code is `0` when nothing was found and `2` when something was — which
is what a cron job or an on-call rotation branches on, and says nothing about
the service.

## Metrics

`GET /metrics` on the REST listener answers with the Prometheus text format:
counters only, counted on the request path, and no database read.

The families, and what each answers: how many webhook deliveries arrived and
were stored, how many run rows were written, every outbound GitHub call by
operation and status code, every tool call by tool and outcome, and how long
each took. A wildcard path (`/runs/{id}`) is a label shape, not a repository
name, so the series stay aggregatable.

Three facts about the shape:

* **process-local, summed by the scraper.** Counters restart at zero on every
  boot, which is what makes them free to increment; `rate()` handles it, and
  `sum()` across instances is the deployment-wide view.
* **no gauges here.** A gauge describes shared state, and the shared state is the
  database: `ci_status.storage` (size, rows, partitions) and `ci_status.health`
  (pool) read it for whoever asks. A scrape should not pay for a read.
* **probes and `/metrics` itself are not counted.** Counting the scraper would
  turn monitoring into load, and counting a probe would read as traffic.

```yaml
scrape_configs:
  - job_name: github-actions-tracker
    static_configs:
      - targets: ["tracker-web:8000"]
```
