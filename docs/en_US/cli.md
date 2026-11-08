# The CLI

Two entry points: `rhosocial-github-actions-tracker` and
`rhosocial-github-actions-tracker-db`. This page lists each command's arguments, output and exit
codes. See [MCP interface](mcp.md) and [REST interface](rest.md) for the other two surfaces.

The CLI shares the same controllers as the other surfaces: queries read GitHub, writes land in order,
and there is no cross-model transaction. It talks to the database directly, so it needs no API key.

## Global flags

Global flags go **before** the subcommand.

| Flag | Meaning |
|---|---|
| `--format {text,json}` or `-f` | output format; `text` by default |
| `--verbose` or `-v` | write the libraries' DEBUG log lines to standard error; otherwise `WARNING` and above |
| `--api-key` | the key for the protocol endpoints; CLI commands do not use it, so it is normally unset |

```bash
rhosocial-github-actions-tracker --format json runs --repo owner/repo --limit 5
```

The ORM normally writes a debug line per query to standard error. The CLI now sets one level at
startup from `GHAT_LOG_LEVEL`: `WARNING` when unset, `DEBUG` when `--verbose` is given. Redirected
JSON or text therefore stays clean; add `--verbose` when diagnosing.

## Query and action commands

| Command | Required | Optional | Output |
|---|---|---|---|
| `status` | `--repo`, `--run-id` | none | one run's status, conclusion, branch and duration |
| `runs` | `--repo` | `--workflow`, `--branch`, `--status`, `--limit` (default 10) | recent run list |
| `latest` | `--repo` | `--branch`, `--workflow`, `--wait` | the newest run; without `--branch`, the newest run of the whole repository |
| `jobs` | `--repo`, `--run-id` | none | the run's jobs: id, name, status, conclusion, runner, start and end, duration |
| `logs` | `--repo`, `--job-id` | `--tail`, `--step`, `--failed-steps`, `--output` | log text; `--output` writes the complete text to a file |
| `failures` | `--repo`, `--run-id` | `--tail` (default 60) | failed steps grouped by byte-identical text |
| `pr` | `--repo`, `--pr` | none | the pull request's runs |
| `refresh` | `--repo`, `--run-id` | none | forced sync result: created/changed, jobs and steps synced |
| `trigger` | `--repo`, `--workflow`, `--ref` | `--input`, `--commit-sha`, `--if-absent`, `--wait` | trigger result; with `--wait`, waits to completion |
| `rerun` | `--repo`, `--run-id` | `--failed-only`, `--wait` | re-run result and whether the attempt advanced |
| `cert` | none | `--certfile`, `--keyfile`, `--hostname`, `--cafile`, `--crlfile`, `--json` | certificate check result |
| `serve {all,mcp,rest,webhook}` | the service | `--host`, `--port` | long-running processes; no ordinary output |

`status`, `jobs` and `failures` all need both the repository and the run id: a run id is globally
unique, but reading it from GitHub still requires knowing which repository it belongs to.

`--input` repeats, one `key=value` per flag:

```bash
rhosocial-github-actions-tracker trigger --repo owner/repo --workflow ci.yml --ref main \
  --input target=postgres --input python=3.12
```

`trigger` dispatches a run by default and spends CI minutes. To "make sure this commit has run"
rather than start another, pass `--if-absent`: the service asks GitHub for a run of that workflow at
that commit and returns it instead of dispatching. `--commit-sha` is what the `--if-absent` check
compares against; without it, the service reads `--ref`'s head, which needs Contents read on the
credential.

`jobs` lists jobs, not steps. Read a failed step's text directly with `logs --failed-steps`;
the step number for `logs --step N` comes from MCP's `get_job` or REST's `GET /jobs/{job_id}`.

`--format json` applies to commands that return data; `serve` returns no ordinary result, and
`cert`'s exit-code semantics are below.

## Credential commands

| Command | Required | Optional | Output |
|---|---|---|---|
| `credential pat add` | `--name`, `--token` | `--scope`, `--github-name`, `--probe-repo` | adds a credential; refuses an existing name |
| `credential pat ensure` | `--name`, and one of `--token` / `--token-file` / `--token-stdin` | `--scope`, `--github-name`, `--probe-repo` | adds or rotates the credential and verifies the token |
| `credential pat list` | none | none | credential list, no plaintext |
| `credential pat show` | `--name` | `--probe-repo` | metadata and probe result |
| `credential pat rotate` | `--name`, `--token` | `--probe-repo` | replaces the token, keeping id and bindings |
| `credential pat refresh` | `--name` | `--probe-repo` | probes again and updates the capability cache |
| `credential pat remove` | `--name` | none | removes the credential; refuses while repositories still reference it |

`--probe-repo` names an `owner/repo` repository and is used to measure Actions write permission and
webhook management permission. Both answers are repository-scoped; without one they stay unknown.

`add` takes `--token` on the command line only. For a file or standard input use `ensure`, which adds
a missing credential and rotates an existing one.

## Repository and webhook commands

| Command | Required | Optional | Output |
|---|---|---|---|
| `repo subscribe` | `--name` | `--credential`, `--webhook-secret-file`, `--create-hook` | subscription result; the secret is shown once when it was not pushed |
| `repo list` | none | none | subscribed repositories, each with its credential and write capability |
| `repo unsubscribe` | `--name` | none | removes the local subscription and cache |
| `repo bind` | `--name`, `--credential` | none | binding result |
| `repo unbind` | `--name` | none | removes the binding |
| `repo discover` | none | `--credential` | repositories the credential can reach on GitHub |
| `webhook create` | `--name` | none | creates the hook on GitHub |
| `webhook delete` | `--name` | none | deletes the GitHub hook |
| `webhook list` | `--name` | none | information about the GitHub hook |
| `webhook ping` | `--name` | none | sends one ping |
| `webhook status` | `--name` | none | whether the local secret is configured, its state and hook id |
| `webhook rotate-secret` | `--name` | `--create-hook` | rotates the secret; shown once when not pushed |

`repo discover` uses the deployment's ambient credential while no binding exists yet; `--credential`
selects an alias instead (for example `work`). The other `repo` commands name a credential as
`kind/name` (for example `pat/work`).

A secret is printed only once, when it was generated but not pushed to GitHub. To see it again,
rotate.

## Database commands

`rhosocial-github-actions-tracker-db` performs database-side maintenance.

| Command | Required | Optional | Output |
|---|---|---|---|
| `init` | none | `--no-create-database`, `--maintenance-db` | creates the database and tables; repeatable |
| `is-initialized` | none | none | exit `0` when initialized, `1` when not |
| `reset` | `--yes` | none | drops and rebuilds every `ghat_*` table |
| `drop` | `--yes` | `--maintenance-db` | drops the whole database |
| `cleanup` | at least one cleanup flag | `--runs-before`, `--logs-before`, `--deliveries-before` | time-based cleanup |
| `partitions` | none | `--list`, `--ensure N`, `--drop-before NyM`, `--drop-old`, `--json` | partition list, or creation and drop results |
| `backfill-log-times` | none | `--dry-run` | fills the trigger time into log rows stored before that column |
| `stats` | none | none | exact row count per table |
| `usage` | none | `--json` | total database size, per-table size, runs added in the last day, `pg_trgm` state |
| `export` | `--table`, `--output` | none | writes one table as JSON |
| `deliveries` | none | `--unhandled`, `--stream`, `--limit` | webhook delivery ledger |

`partitions --drop-before NyM` only reports what it would drop; add `--drop-old` to perform the drop.

`stats` uses `COUNT(*)` and returns exact counts. `usage` reports the query planner's estimates:
a table that was just created, or one that has not been `ANALYZE`d for a while, can differ visibly
from the real count. Use `usage` for disk space, not for reconciling rows.

`export` copies a table as-is, including plaintext columns such as `ghat_pat_credentials.token`.
A file produced from such a table is key material and must not be committed or shared.

`deliveries` prints timestamps with the database session's explicit offset, which may be `+08:00`;
timestamps returned by the interfaces are always UTC.

## Errors and exit codes

When a command fails, the CLI writes one sentence to standard error and exits with `1`:

```text
error: Run 12345 of 'owner/repo' has no failed job in its latest attempt (1); ...
```

A business failure the service reports -- an unsubscribed repository, a run with no failed job -- is one
sentence; it does not print a Python traceback. A database-driver exception or a defect still shows its
original exception; add `--verbose` for the logs when that happens.

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | general error: bad arguments, unsubscribed repository, GitHub refusal, database failure |
| `2` | `cert` found a certificate problem; the certificate may still be usable and the operator decides whether to block |

The `0/1` from `is-initialized` is a state, not a failure: `1` means "not initialized yet".

## Examples

```bash
rhosocial-github-actions-tracker --format json runs --repo owner/repo --limit 5
rhosocial-github-actions-tracker latest --repo owner/repo --branch main
rhosocial-github-actions-tracker status --repo owner/repo --run-id 12345
rhosocial-github-actions-tracker jobs --repo owner/repo --run-id 12345
rhosocial-github-actions-tracker logs --repo owner/repo --job-id 67890 --tail 80
rhosocial-github-actions-tracker logs --repo owner/repo --job-id 67890 --failed-steps
rhosocial-github-actions-tracker failures --repo owner/repo --run-id 12345 --tail 40
rhosocial-github-actions-tracker trigger --repo owner/repo --workflow ci.yml --ref main \
  --if-absent
rhosocial-github-actions-tracker-db usage --json
rhosocial-github-actions-tracker-db partitions --list
```
