# Logs and windows

## Two reads, two scopes

```
get_job_log(job_id)                        the failed steps (or the merged document)
get_job_log(job_id, whole_job=true)        the job's merged document (ct_job_logs)
get_job_log(job_id, step=N)                that step's own file (ct_step_logs)
get_job_log(job_id, failed_steps_only=true)   the text of every failed step
search_job_log(job_id, query)              search the merged document
search_job_log(job_id, query, step=N)      search one step's document
get_run_failed_logs(run_id)                the failed steps of a run's failed jobs,
                                           one copy per distinct text
```

`search_job_log` and `get_job_log` take the same `step` argument: `source`
(`job` / `step`) in the answer says which scope the line numbers belong to. A step
with no captured log returns a `reason` (`capture_closed` /
`not_a_step_archive` …) — never `no_match`, because "no document" and "no match"
are different answers.

## A failed job is read from the steps that failed

Ask for a log without naming a scope and the job decides:

- a job that failed returns the text of the steps that did not pass, and the
  answer carries `scoped_to_failed: true` — an answer that narrowed on its own
  must not read like a complete document;
- `whole_job=true` asks for the merged document anyway; the default is a
  convenience, not a restriction;
- when no failed step's log was ever captured (the archive window passed), the
  merged document is returned with a `scope_note` saying so. That is the
  difference between "the failed text was never fetched" and "there is no log";
- a job that passed, or is still running, returns the merged document. A null
  conclusion is not a failure: only `failure` / `cancelled` / `timed_out` /
  `startup_failure` / `action_required` / `stale` are.

The choice reads step *conclusions*, never log text: no error markers, no owner
sniffing.

## One failure printed by N jobs is one group

`get_run_failed_logs(run_id)` reads the failed steps of every failed job in the
run and groups them by **byte-identical text**: one copy of the text, plus the
`steps` (job, step number, name, conclusion) that printed it. A matrix usually
fails for one reason in twelve jobs, and this answers "one cause or many" in one
call instead of twelve.

Grouping is exact on purpose. Texts that differ at all are separate groups —
deciding that two different texts mean the same thing would be an interpretation,
and a wrong one the day a project changes its output. Failed steps whose log was
never captured are listed in `steps_without_logs`, never substituted; a failed
job with no failed step at all (`startup_failure`) is named in
`jobs_without_failed_steps`, because it is one of `jobs_failed` and the count
must be accountable. Each group carries the tail of its text (`tail=`, default 60
lines, bounded); read one step in full with `get_job_log(job_id=..., step=N)`.

## Windows address, they do not interpret

| Argument | Meaning |
|---|---|
| none | the last 200 lines |
| `tail=N` | the last N lines |
| `from_line` / `to_line` | an explicit inclusive range |
| `grep` | your regex (invalid patterns fall back to a literal) plus `context` |
| `prefer="tail"` / `"head"` | which matches get context text in `search_job_log` |

A log is appended to while it runs: the conclusion is at the end and the setup
noise at the beginning. `search_job_log` therefore shows context for the *last*
matches by default, and every match position is still reported either way;
`prefer="head"` moves the text (not the list) to the other end, which is what
"when did this first appear" asks for.

No test-summary anchoring, no error markers, no trimming of a runner's teardown:
those are properties of one project's output, and a general tracker that guesses
at them is wrong for every other project.

## Reading a step never captures

`get_job_log(step=N)` answers what is stored. To fetch again, call
`get_job(job_id, refresh_logs=true)` — meaningful only while GitHub still serves
the split (before `capture.window_closes_at`).

## Capacity and cleanup

- `db usage` reports what the database holds;
- the log tables are partitioned by month, so `db partitions --drop-before <N>m`
  drops a whole month without locking the table or running a vacuum.

## Which month a log belongs to

Every log row carries the **bucket** it belongs to: the first day of the UTC
month its job finished. `ct_jobs.log_bucket` holds the same value, and
`ct_steps.log_bucket` copies it, so a read that has the job (or the step) in hand
knows the month without another query.

The bucket is derived, never independently stored: the write path computes it
from the job's own `completed_at` with one function (`bucket_of`), and the read
path looks the row up *by* it. That is what makes each month an addressable unit
-- and, once the tables are partitioned, a droppable one.

It is **half of the primary key.** A partitioned table requires every key to
contain the partition column, so the log tables' key is `(id, bucket)`: `id` is
the server's (`GENERATED BY DEFAULT AS IDENTITY`; declared as an identity key on
the model too, which is why the ORM leaves it out of the INSERT and reads it back
through RETURNING) and `bucket` is the caller's. Leave it out and the model refuses at construction --
`Composite primary key field 'bucket' must be provided` -- which names the field
that is missing instead of a constraint, the way PostgreSQL's `CheckViolation`
would. The corollary: a row with a null bucket *cannot exist*, so the read
path's `bucket=None` spelling now means "every bucket", exactly as it reads.

Callers holding the parent use `JobLog.store_for_job(job, text)` /
`StepLog.store_for_step(step, text)` and let the model derive the month; building
a row yourself means supplying the bucket.

**An existing database must be rebuilt.** `(id, bucket)` is a change of schema
shape, and this project keeps no migrations: `db reset` rebuilds it, partitions
and `pg_trgm` included. Reading an un-rebuilt database still works, but writing
fails while the key is absent -- so the rebuild is a required step, not an option.

