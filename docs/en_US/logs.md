# Logs and windows

## Two reads, two scopes

```
get_job_log(job_id)                        the failed steps (or the merged document)
get_job_log(job_id, whole_job=true)        the job's merged document (ghat_job_logs)
get_job_log(job_id, step=N)                that step's own file (ghat_step_logs)
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

The partition column is `attempt_created_at`: the exact time the execution was
triggered, copied from `ghat_run_attempts.github_created_at` onto the log row.
It is deliberately **not** the time the log arrived. Arrival time is not
controllable — retries, backfill and a closing archive window all move it — so
filing by arrival would scatter one run's logs into months that have nothing to
do with the run. A read starts from the run, so the reader already holds the
value and does not have to derive it.

The column is **half of the primary key**. A partitioned table requires every
key to contain the partition column, so the log tables' key is
`(id, attempt_created_at)`: `id` is the server's (`GENERATED BY DEFAULT AS
IDENTITY`; declared as an identity key on the model too, which is why the ORM
leaves it out of the INSERT and reads it back through RETURNING) and
`attempt_created_at` is the caller's. Leave it out and the model refuses at
construction, naming the field rather than a constraint — the useful answer.

The write path takes the attempt explicitly:
`store_log(job, content, attempt)` and `store_step_log(step, content, attempt)`
ensure the month's partition first, then write. Both `Job` and `Step` also expose
`attempt_created_at` as a derived property, but the storage call receives the
attempt so the write and the read cannot disagree about which month the row
belongs to.

This column is a schema shape decision. Changing it means rebuilding the
database, because this project keeps no migrations: `db reset` rebuilds it,
partitions and `pg_trgm` included.
