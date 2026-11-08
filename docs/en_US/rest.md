# REST interface

REST is the programmatic surface for scripts and clients other than MCP and the CLI. See
[MCP interface](mcp.md) and [The CLI](cli.md) for the other two.

## Endpoint, authentication and docs

- Base path: `/api/v1`; the webhook receiver is `/webhooks/github`, called by GitHub, not a user API.
- Authentication: with `GHAT_API_KEY` set, every request carries `Authorization: Bearer <key>`.
- Generated docs: `GET /docs` serves the interactive OpenAPI page and `GET /openapi.json` the machine
  schema. Parameters and response fields follow that schema.

## Response conventions

- Timestamps are UTC ISO-8601 with an explicit `+00:00` offset.
- Lists return a bare array; pagination uses `per_page` and `page`.
- Errors return `{"error": "<code>", "message": "<text>", "details": {...}}`; the HTTP status matches
  the `code`.
- Created: `201`; trigger and re-run: `202`; delete: `204`; not ready: `503`.

## Endpoints

### Repositories

| Method and path | Input | Output |
|---|---|---|
| `GET /repositories` | none | repository array: `id`, `full_name`, `owner`, `name`, `is_private`, `default_branch`, `subscribed_at` |
| `POST /repositories` | JSON: `full_name`, `token_name` | `201` with the repository object |
| `DELETE /repositories/{repo_id}` | path `repo_id` | `204` |

`token_name` accepts the CLI name (`work`) and the `kind/name` form other surfaces show (`pat/work`).

### Workflows

| Method and path | Input | Output |
|---|---|---|
| `GET /workflows` | query `repository` | workflow array: `id`, `workflow_id`, `name`, `path`, `state` |
| `GET /workflows/{workflow_id}` | query `repository` | one workflow |
| `POST /workflows/{workflow_id}/trigger` | JSON: `repository`, `ref`, `inputs`, `commit_sha`, `if_absent` | `202`: `run_id`, `status`, `message`, `html_url`, `conclusion`, `attempt`, `dispatched` |

`workflow_id` is the numeric id. With `if_absent=true`, an already existing run of that workflow at the
target commit is returned with `dispatched=false`.

### Runs

| Method and path | Input | Output |
|---|---|---|
| `GET /runs` | query `repository`; optional `workflow`, `branch`, `status`, `per_page` (1–100, default 30), `page` (default 1) | run array |
| `GET /runs/{run_id}` | query `repository`, `attempt` | one run |
| `GET /runs/{run_id}/failed-logs` | query `repository` | `run_id`, `jobs_failed`, `groups[]`, `steps_without_logs`, `jobs_without_failed_steps` |
| `POST /runs/{run_id}/rerun` | query `repository`, `failed_only` | `202`: `attempt`, `previous_attempt`, `advanced`, `reran_jobs[]` |
| `POST /runs/{run_id}/refresh` | query `repository` | `created`, `changed`, `jobs_synced`, `steps_synced`, `html_url` |
| `GET /latest-run` | query `repository`; optional `branch`, `workflow` | the newest run, or `null` |
| `GET /pull-request-runs` | query `repository`, `number` | run array; an empty array is valid |

Omitting `branch` from `latest-run` means “the newest run of this repository”, not “the newest on the
default branch”. `refresh` always reads GitHub and is the repair path for a missed webhook.

### Jobs

| Method and path | Input | Output |
|---|---|---|
| `GET /runs/{run_id}/jobs` | query `repository` | job array, each with steps and log presence |
| `GET /jobs/{job_id}` | query `repository` | one job |
| `GET /jobs/{job_id}/logs` | query `repository`; optional `tail`, `step`, `failed_steps_only` | log response: `content`, `sections[]`, `reason`, `line_count` |

REST returns the complete log text and does not cut it to a model context window; the caller decides how
to window it. A step whose log was not captured returns a `reason` and never substitutes another step's
text.

### Tokens

| Method and path | Input | Output |
|---|---|---|
| `GET /tokens` | none | token metadata array (no plaintext) |
| `POST /tokens` | JSON: `name`, `token` | `201` with token metadata |
| `POST /tokens/{name}/verify` | path `name` | probe result: account, permissions and reach |
| `PATCH /tokens/{name}` | JSON: `token`, `expires_at` | rotated metadata; id and bindings preserved |
| `DELETE /tokens/{name}` | path `name` | `204` |

### Probes and metrics

| Method and path | Meaning |
|---|---|
| `GET /livez` | process alive, zero I/O |
| `GET /readyz`, `/healthz`, `/health` | one database round trip; `503` means this instance cannot serve |
| `GET /metrics` | Prometheus text counters |

Probes need no API key and do not occupy a per-request connection scope.

## Examples

```bash
curl -H "Authorization: Bearer $GHAT_API_KEY" \
  "https://tracker.example.com:8000/api/v1/runs?repository=owner/repo&branch=main&per_page=10"

curl -X POST -H "Authorization: Bearer $GHAT_API_KEY" -H "Content-Type: application/json" \
  -d '{"repository":"owner/repo","ref":"main"}' \
  "https://tracker.example.com:8000/api/v1/workflows/12345/trigger"
```

Error shape:

```json
{
  "error": "repository_not_subscribed",
  "message": "Repository 'owner/repo' is not tracked",
  "details": {}
}
```

## Differences from MCP

- REST returns the complete log; MCP windows it for a model's context and adds `hint`.
- REST has no `hint` field; callers branch on `error`, `reason` and `next_offset`.
- REST identifies a workflow by numeric id; MCP also accepts a display name, file name and full path.
