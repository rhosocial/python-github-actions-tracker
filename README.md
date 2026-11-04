# python-ci-tracker

[![CI](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/test.yml/badge.svg)](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/test.yml)
[![Image build](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/images.yml/badge.svg)](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/images.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/vistart/citracker)](https://hub.docker.com/r/vistart/citracker)
[![Docker Image Size](https://img.shields.io/docker/image-size/vistart/citracker/latest)](https://hub.docker.com/r/vistart/citracker/tags)
[![Python](https://img.shields.io/badge/python-3.15-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-en_US_%2F_zh__CN-green.svg)](https://rhosocial.github.io/python-github-ci-tracker/)

**A self-hosted service that answers "what happened in CI" — over MCP, REST and a CLI.**

It reads GitHub Actions runs, jobs, steps and logs; it does not poll in the
background, does not keep a central token pool, and never treats a cached run as
the truth when GitHub can still answer.

English | [简体中文](README.zh-CN.md)

---

## What you get

| | |
|---|---|
| **Ask once, in any protocol** | `ci_status`, `find_runs`, `get_run`, `get_job`, `get_job_log`, `search_job_log`, `get_run_failed_logs`, `wait_for_run`, `trigger_workflow` — over MCP (Streamable HTTP), REST and a CLI. One flow, three shapes; a model or a script uses the same answers. |
| **Three layers, one step each** | `get_run` names the job that failed, `get_job` names the step, `get_job_log` returns its text. No call answers two layers, so no caller has to know which one it is looking at. A failed job read without a scope starts at its failed steps; `whole_job=true` asks for everything. |
| **One failure in twelve jobs, one group** | `get_run_failed_logs` groups a run's failed-step texts by byte equality: one copy plus the jobs that printed it. Same cause or twelve? One call, and no judgement about meaning. |
| **Step-level logs, kept** | GitHub splits a run into per-step files for about an hour; after that the same endpoint returns one merged document. The service captures the split while it exists, stores it as ordinary rows, and reads it forever. Missed capture is reported honestly — never replaced by another step's text. |
| **Windows that address, not interpret** | `tail`, an explicit line range, or your own regex. `search_job_log` shows context for the *last* matches by default, because a log ends with its conclusion; every position is still reported. No guessing at test summaries or teardown output, because a general CI tracker cannot know what any project prints. |
| **Trigger and wait, without burning minutes** | `trigger_workflow` dispatches on request; `if_absent=true` answers "this commit already ran" without starting a second run. `rerun_run(failed_only=true)` repeats only the jobs that broke, after a local check that there *are* failed jobs. `wait_for_run` is a bounded long poll whose timeout comes from the workflow's own history. |
| **PostgreSQL, and it says so** | Log tables are partitioned by month so old data is one `DROP TABLE`; a trigram index accelerates searching them (`pg_trgm`, ~1× the log text in disk); the schema is the service's own renderer; the test suite runs against a real server. |
| **A budget from real time, not the queue** | Every run read carries `timings`: the jobs' span, the queue, and the span of the jobs that passed — the numbers a wait is sized from, so a queue behind other repositories does not inflate the p50/p90. `db usage` and `ci_status.storage` report what the database actually holds. |
| **One image, two registries** | `images.yml` publishes the same multi-arch build to Aliyun (`vistart_public/citracker`) and Docker Hub (`vistart/citracker`), tagged `latest`. Each destination has its own secrets, so either can be absent. It runs only when asked for, and `build.revision` — not a tag — says which build is running. |
| **Metrics in the standard format** | `GET /metrics` on the REST listener answers with the Prometheus text format: webhook deliveries, GitHub calls by status code, tool calls by outcome, connection-pool gauges via `ci_status.health`. Process-local counters; sum across instances in the scraper. |

## Why you would use it

Three different things, and the third is the easiest to underestimate.

- **Context**: a 4,000-line job log becomes `grep` and a few lines of context; a
  12-job matrix becomes a 12-line roster. Some of that log does not fit in a
  context window at all, so the choice is not cheaper but possible.
- **Time**: `wait_for_run` is one bounded call instead of a polling loop;
  `cancel_run` and `if_absent=true` stop minutes you would otherwise spend, and
  `trigger_workflow` returns p50/p90 so you can decide what to wait for.
- **Wrong conclusions**: `attempt`/`is_latest` separates a capacity blip from a
  broken backend; `has_log` plus `reason` separates "no output" from "nothing was
  captured"; `ci_status.health.alerts` reports its own silent degradation as an
  executable list.

For the long version — including an operator's measured session and what we saw
using the service on itself — see [What it saves](docs/en_US/value.md).

## The advantage of a local cache

Once a log is stored, reading it is a local query — and a local query is cheap,
repeatable, and parallel:

- **Filters are local.** `find_runs` narrows by commit, pull request, branch,
  workflow file or status against stored rows; `recent_runs` answers for sixteen
  repositories in one call. None of that spends GitHub quota.
- **The text is local too.** Windows, `grep` and search run against one stored
  document, so partial reads are the normal case rather than an exception.
- **The pattern is yours.** Failure, error, warning, or a test conclusion — the
  service returns the lines a caller's pattern matched and their line numbers.
  Switch test frameworks and one string changes; the tracker does not need to
  relearn what your output means.
- **The database carries the load.** A read is one query, not a poll: no
  background jobs, no queue, and PostgreSQL's parallel reads and cascades
  absorb whatever the filter admits.

## Use it from your coding agent

The MCP endpoint is `http://127.0.0.1:8001/mcp` (Streamable HTTP). One line
per agent — the full configuration reference, including the shared-file shapes
and what each agent's approval gate does, is
[Coding agents over MCP](docs/en_US/mcp-agents.md):

```bash
claude mcp add --transport http citracker http://127.0.0.1:8001/mcp   # Claude Code
codex mcp add citracker --url http://127.0.0.1:8001/mcp               # Codex (TOML)
opencode:  "mcp": { "citracker": { "type": "remote", "url": "…", "oauth": false } }   # opencode.json
cmd mcp add --transport http citracker http://127.0.0.1:8001/mcp      # Command Code
```

With `CITRACKER_API_KEY` set, add `--header "Authorization: Bearer <key>"`
(Claude Code, Command Code), `bearer_token_env_var` (Codex) or an
`Authorization` header with `oauth: false` (OpenCode).

## Where the details live

Read it online (no checkout needed): <https://rhosocial.github.io/python-github-ci-tracker/> · [简体中文](https://rhosocial.github.io/python-github-ci-tracker/zh_CN/)

- [Documentation (English)](docs/en_US/README.md) — deployment, the API, the
  CLI, development
- [文档（简体中文）](docs/zh_CN/README.md)

## License

[Apache License 2.0](LICENSE).
