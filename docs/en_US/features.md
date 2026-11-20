# Features

## Three object layers, one tool each

There is exactly one path from a failing run to the failing step:

```
get_run(run_id)               → the run and its job roster
get_job(job_id)               → one job with every step (status, conclusion, has_log)
get_job_log(job_id, step=N)   → that step's text
get_job_log(job_id)           → the failed steps of a failed job, or the merged document
get_job_log(job_id, whole_job=true)   → the merged document, explicitly
get_run_failed_logs(run_id)   → the failed steps of every failed job, grouped by text
```

- Steps belong to `get_job` only: `get_run` has no `include_steps` and no failure block.
- `failed_steps_only=true` is a batch convenience, not a second way to decide which
  step failed.
- A failed job read without a scope starts at its failed steps (`scoped_to_failed: true`);
  the scope comes from the step conclusions, not from the text.
- A passing job is not pushed to read logs — `has_log` is still honestly `true`.

## A run explains itself

One commit often has two runs (`test.yml` and `changelog-check.yml`), so `find_runs` /
`recent_runs` / `get_run` all carry four groups of identity. Miss one and a caller
can make the wrong decision:

| Group | Fields | Answers |
|---|---|---|
| Which workflow | `workflow: {name, path}` | tells the two runs on one sha apart |
| Which commit | `commit: {sha, message, author, committed_at}` | `author` wrote the code; `actor` triggered the run |
| Which PR | `pr_number`, `base_ref` | trace a run back to its PR and target branch |
| Where the code came from | `head_repo`, `display_title` | a fork PR's code is not in this repo; the title is GitHub's one-line summary |

`base_ref` is backfilled, not native to a run: GitHub stops returning `base.ref`
with a run once the PR closes, so a run can show `pr_number` with a null `base_ref`.
Reading a single run queries that PR once and writes it back, so it is never fetched
again.

## Two log sources

| Source | Content | Availability |
|---|---|---|
| `GET /actions/jobs/{id}/logs` | a job's merged text | any time within 90 days, stored in `ghat_job_logs` |
| `GET /actions/runs/{id}/logs` | zip: merged file + `<job>/<step no>_<name>.txt` | GitHub serves the split for about an hour after the run finishes |

**The hour is a fetching deadline, not a retention limit.** Text fetched into
`ghat_step_logs` is ordinary data, readable forever.

## Naming a workflow: path, file name or id

A display name is not a reference. `name:` lives in the workflow file, so the same
file can carry a different name on every branch. Use the path or the id. For example,
`vistart/Dockerfiles`' `build_and_deploy.yml` has appeared as "php - Build and Deploy",
"vlmcsd - Build and Deploy" and "ubuntu - Build and Deploy".

`ghat_workflows.workflow_id = 0` means **unresolved**, never "all workflows": a
workflow filter triggers a source-of-truth lookup and writes the id back; if it
cannot be resolved, `workflow_not_found` is raised. Silently dropping the filter
would hand the caller everything while they believed it was filtered.

## Failure

Only `FAILED_CONCLUSIONS` count. `conclusion = null` is GitHub still materializing
metadata, not a failure.

## Webhooks lower latency; they are not the truth

A delivery can be lost and a run is often created outside this service, so every
run-level read asks GitHub again. When GitHub cannot answer, the service reports
that — it never degrades to a cached answer.
