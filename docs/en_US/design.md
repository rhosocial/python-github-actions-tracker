# Design decisions

This page is written for a developer about to extend the project. Each section states a
decision, then explains why the alternative was rejected; finally it names the tests that
assert the decision.

## GitHub is the truth; the database is a cache

The service answers four questions **from GitHub every time**:

* does this run exist;
* what is its status;
* what runs are on this branch / workflow;
* which run is the latest one.

If GitHub cannot be reached, the call fails. It does not fall back to a cached answer and
call it current: a stale "success" is worse than an error, because the caller will act on
it. When a read succeeds, a response that differs from the local row is written back, so
the cache converges without a repair job.

This is what makes the two channels safe. A missed webhook is not a data loss event: the
next read repairs it. That is also why there is no background poller, no scheduler and no
queue in the service definition -- the read path is the reconciliation path.

Conditional requests keep "always read GitHub" affordable: `ETag` responses are cached on
GitHub's side and a `304` does not spend API quota.

## Pure ActiveRecord, and no unit of work

One `save()` is one complete write. There is no session, no flush, no "these models commit
together".

A webhook delivery is therefore a **write sequence**, not an atomic unit:

```text
verify signature → parse ids → store parent → store attempt → store job → store steps
```

If step 4 fails, the delivery stops there. That half-written state is a legitimate state:
the rows that were written are complete rows, and the next read (which goes to GitHub)
fills the gap. The delivery ledger records that this delivery was not handled, so the gap
is visible rather than silent.

The rejected alternative is a transaction spanning the cascade. It would promise rollback
for an event that can simply be re-read, and it would require the lock and the "pending"
state that go with it -- neither of which answers the question "what does GitHub say now".

The guard: `tests/core/test_no_unit_of_work.py` fails if a transaction, a write scope, a
delivery lock or a deferred post-commit task appears.

## One parent per level

```text
Repository → Workflow → WorkflowRun → RunAttempt → Job → Step
```

Every level takes only its **direct** parent. A job does not know about a run: it belongs
to an attempt, and an attempt belongs to exactly one run. Carrying `run_id` on the job as
well would create two foreign keys that are free to disagree, and a column set where one
non-key attribute is determined by part of a key -- a BCNF violation, and the reason
`ghat_jobs` has no `run_id`.

Resolving which parent an incoming payload names is a **controller** decision, not a model
decision: the payload knows the attempt number, the parent knows which attempts it has.

## Models take values; one service translates GitHub

`services/github/payloads.py` is the only class that reads a GitHub field name. `GithubPayloads`
returns values named after the **model columns**:

| GitHub writes | Model stores |
|---|---|
| `head_sha` | `commit_sha` |
| `run_attempt` | `attempt_number` |
| `run_started_at` | `started_at` |
| `id` on a run object | `github_run_id` |
| `id` on a job object | `github_job_id` |

The reason is not style. GitHub has never promised stable payload shape, and hard-coding
the parse into a controller couples business logic to one version of an interface. Keeping
the translation in one class makes an API rename one edit, testable without a database.

The models keep only value entries:

```python
Workflow.refresh(*, workflow_id=None, name=None, path=None, state=None)
WorkflowRun.refresh_metadata(*, run_number=None, ...
RunAttempt.record(run, attempt_number, *, status=None, ...
Job.record(attempt, *, github_job_id, ...
Step.record(job, *, number, ...
```

Row invariants are enforced where the row is built, by Pydantic:

* `IdentityMixin` -- identity numbers are never negative; `attempt_number >= 1`;
* `UtcTimestampMixin` -- a naive datetime is read as UTC, so stored and computed times
  compare equal;
* `Repository.full_name` must be `owner/name`; `RepoWebhookSecret.secret` cannot be empty
  and `state` is one of this project's three; `PatCredential.name` / `token` cannot be
  empty.

`status` and `conclusion` are **not** whitelisted by vocabulary. GitHub may add a value,
and a row that refuses to store an unknown conclusion loses the only record of the run.
The state machine is where an unknown status is rejected explicitly, at a transition.

The guard: `tests/models/test_no_payload_in_models.py` walks `models/` with AST and fails
on a `*_from_remote` name, a `Mapping` parameter on a public method, or a module-level
function.

## A run is identity; an attempt is history

`ghat_workflow_runs` stores what a run *is*: the workflow, the number, the branch, the
commit, the pull request. Its status and timestamps are not columns, because those are
facts about one execution.

`ghat_run_attempts` stores what each execution *did*. The run's "current state" is a query
result, computed by the database from the newest stored attempt
(`latest_attempt`, `latest_status`, `latest_conclusion`, ...). There is no second copy of
that fact to disagree with.

The state machine is forward-only: `queued → in_progress → completed`. A late `in_progress`
payload for an attempt that already finished is the past arriving out of order, so it is
logged and skipped. It never rewrites history.

Attempt numbers are sparse on purpose. "Attempt 1 skipped" can really mean GitHub reported
attempt 3 first; the table stores what was reported, and the field is named
`latest_attempt_number`, never `attempts_total`.

## Two logs, one merged text and one capture window

| Source | What it is | Availability |
|---|---|---|
| `GET /actions/jobs/{job_id}/logs` | the job's whole text | within 90 days, always |
| `GET /actions/runs/{run_id}/logs` | per-step archive plus a merged file | per-step files exist for about one hour after the run finishes |

The merged file is byte-identical to the job endpoint, so `ghat_job_logs` is unchanged.
The per-step files are a **capture deadline**, not a retention window: once copied into
`ghat_step_logs` a row lives as long as any other row. When the archive no longer offers
per-step files, the answer is `capture_closed` -- not "expired log", because nothing
stored has expired.

The partition key of both log tables is the time the execution was **triggered**
(`ghat_run_attempts.github_created_at`, copied onto the log row). A log is filed under the
run it belongs to, not under the hour it happened to be downloaded.

## Names say what is local

A reader must never mistake "what this service has" for "what GitHub has".

| Prefix / suffix | Means |
|---|---|
| `*_cached` | a count or existence answer, from local rows |
| `latest_*` | the newest thing GitHub has told us |
| `*_synced_at` | when a set was last listed from GitHub in full |

Bare `total` / `count` / `all` are not allowed for a local number, and the MCP tools carry
one shared `CACHE_NOTE` so the wording is written once rather than thirteen times.

## What this project deliberately does not do

* no background polling, no task queue, no scheduler;
* no cross-model transaction, and no lock that would imply one;
* no workflow snapshots on a run, and no check that a workflow file still exists;
* no `RawSQL` and no hand-built `SELECT`; the ORM's expression nodes carry the queries
  that need to exist anyway.
