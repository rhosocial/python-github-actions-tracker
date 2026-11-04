# src/rhosocial/github_ci_tracker/adapters/rest/routes/runs.py
"""Run routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query, Request, status

from ...common.errors import NotFoundError
from ..schemas.responses import (
    FailureGroupResponse,
    FailureStepRefResponse,
    JobRefResponse,
    JobSummary,
    ReranJob,
    RerunRunResponse,
    RunFailuresResponse,
    RunRefreshResponse,
    RunStatusResponse,
    TimingsResponse,
)

if TYPE_CHECKING:
    from ....views import RunFailuresView, RunRecord, RunRefreshView

router = APIRouter()


def get_query_service(request: Request):
    """Dependency: the query controller this application serves from."""
    return request.app.state.container.query


def get_trigger_service(request: Request):
    """Dependency: the trigger controller -- the only path that spends CI minutes."""
    return request.app.state.container.trigger


def _run_response(run: RunRecord) -> RunStatusResponse:
    """Project a facade ``RunRecord`` onto the REST response model.

    The mapping stays explicit field by field: the facade already speaks in
    ``run_id`` / ``html_url``, and a renamed or dropped facade field then
    fails loudly here instead of silently vanishing from the payload.
    """
    return RunStatusResponse(
        run_id=run.run_id,
        run_number=run.run_number,
        attempt=run.attempt,
        is_latest=run.is_latest,
        attempts_total=run.attempts_total,
        attempts_cached=run.attempts_cached,
        cache_state=run.cache_state,
        status=run.status,
        conclusion=run.conclusion,
        branch=run.branch,
        workflow_name=run.workflow_name,
        workflow_path=run.workflow_path,
        commit_sha=run.commit_sha,
        commit_message=run.commit_message,
        commit_author=run.commit_author,
        commit_time=run.commit_time,
        display_title=run.display_title,
        head_repo=run.head_repo,
        base_ref=run.base_ref or None,
        pr_number=run.pr_number,
        actor=run.actor,
        event=run.event,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        stored_at=run.stored_at,
        html_url=run.html_url,
        duration_seconds=run.duration_seconds,
        timings=TimingsResponse(
            job_span_seconds=run.timings.job_span_seconds,
            queue_seconds=run.timings.queue_seconds,
            succeeded_job_span_seconds=run.timings.succeeded_job_span_seconds,
        ),
        jobs=[
            JobSummary(
                job_id=job.job_id,
                name=job.name,
                status=job.status,
                conclusion=job.conclusion,
            )
            for job in run.jobs
        ],
    )


def _refresh_response(view: RunRefreshView) -> RunRefreshResponse:
    """Project a facade ``RunRefreshView`` onto the REST response model."""
    return RunRefreshResponse(
        run_id=view.run_id,
        run_number=view.run_number,
        previous_status=view.previous_status,
        status=view.status,
        conclusion=view.conclusion,
        created=view.created,
        changed=view.changed,
        jobs_synced=view.jobs_synced,
        steps_synced=view.steps_synced,
        html_url=view.html_url,
    )


@router.get("/runs", response_model=list[RunStatusResponse])
async def list_runs(
    repository: str,
    workflow: str | None = None,
    branch: str | None = None,
    status: str | None = None,
    per_page: int = Query(30, ge=1, le=100),
    page: int = Query(1, ge=1),
    service=Depends(get_query_service),
) -> list[RunStatusResponse]:
    """List workflow runs."""
    runs = await service.list_runs(
        repository=repository,
        workflow=workflow,
        branch=branch,
        status=status,
        per_page=per_page,
        page=page,
    )
    return [_run_response(run) for run in runs]


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: int,
    repository: str,
    attempt: int | None = None,
    service=Depends(get_query_service),
) -> RunStatusResponse:
    """Get run status, for the newest attempt or the one named by ``attempt``.

    The core exceptions travel to the app-level handler unchanged: a missing run
    is a 404, an attempt number past the last one is a 404 that names the range,
    and a credential or GitHub failure keeps its own status instead of being
    flattened into "run not found".
    """
    run = await service.get_run_status(
        repository=repository,
        run_id=run_id,
        attempt=attempt,
    )
    return _run_response(run)


@router.get("/runs/{run_id}/failed-logs", response_model=RunFailuresResponse)
async def get_run_failed_logs(
    run_id: int,
    repository: str,
    service=Depends(get_query_service),
) -> RunFailuresResponse:
    """Read a run's failed-step logs, grouped by identical text.

    Only steps whose conclusion is a failure are read, and grouping is byte
    equality: a matrix that fails for one reason returns one group naming every
    job that printed it. Nothing here decides that two different texts mean the
    same thing. The text is returned whole -- bounding it is the caller's
    business over HTTP, whereas the MCP tool windows it for a model's context.
    """
    view = await service.get_run_failed_logs(repository=repository, run_id=run_id)
    return _run_failures_response(view)


def _run_failures_response(view: RunFailuresView) -> RunFailuresResponse:
    """Project a facade ``RunFailuresView`` onto the REST response model."""
    return RunFailuresResponse(
        run_id=view.run_id,
        jobs_failed=view.jobs_failed,
        groups=[
            FailureGroupResponse(
                content=group.content,
                occurrences=[_failure_ref(item) for item in group.occurrences],
            )
            for group in view.groups
        ],
        steps_without_logs=[_failure_ref(item) for item in view.steps_without_logs],
        jobs_without_failed_steps=[
            JobRefResponse(job_id=job_id, job_name=job_name)
            for job_id, job_name in view.jobs_without_failed_steps
        ],
    )


def _failure_ref(reference: Any) -> FailureStepRefResponse:
    return FailureStepRefResponse(
        job_id=reference.job_id,
        job_name=reference.job_name,
        step=reference.step,
        step_name=reference.step_name,
        conclusion=reference.conclusion,
    )


@router.post(
    "/runs/{run_id}/rerun",
    response_model=RerunRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rerun_run(
    run_id: int,
    repository: str,
    failed_only: bool = Query(
        False,
        description=(
            "Re-run only the failed jobs of the latest attempt (plus the jobs they "
            "depend on). The run must be completed and have at least one failed job; "
            "otherwise nothing is sent to GitHub and the call reports why."
        ),
    ),
    service=Depends(get_trigger_service),
) -> RerunRunResponse:
    """Re-run a run, whole or only its failed jobs.

    Not idempotent: a re-run spends CI minutes and repeats whatever the original
    run did. A re-run keeps the same run id and increments the attempt.

    ``failed_only`` asks GitHub's ``rerun-failed-jobs`` endpoint, whose
    preconditions are checked in the controller before anything is sent -- a run
    still in flight, or one whose latest attempt has no failed job, is refused
    there rather than answered 403 by GitHub (which would read like a permission
    problem the caller cannot act on).
    """
    result = await service.rerun_workflow(
        repository=repository, run_id=run_id, failed_only=failed_only
    )
    return RerunRunResponse(
        run_id=result.run_id,
        repository=repository,
        status=result.status,
        message=result.message,
        html_url=result.html_url,
        attempt=result.attempt,
        previous_attempt=result.previous_attempt,
        advanced=(result.attempt or 0) > (result.previous_attempt or 0),
        reran_jobs=[
            ReranJob(name=name, conclusion=conclusion) for name, conclusion in result.reran_jobs
        ],
    )


@router.post("/runs/{run_id}/refresh", response_model=RunRefreshResponse)
async def refresh_run(
    run_id: int,
    repository: str,
    service=Depends(get_query_service),
) -> RunRefreshResponse:
    """Force a sync of one run from GitHub.

    The service is fully passive: run state enters the database only through a
    GitHub webhook delivery or through this call. Unlike ``GET /runs/{run_id}``
    this endpoint never trusts a terminal local record, and it adopts a run the
    system has never seen -- so a missed webhook delivery can always be
    recovered here.
    """
    try:
        result = await service.refresh_run(
            repository=repository,
            run_id=run_id,
        )
    except Exception as e:
        raise NotFoundError("run", str(run_id)) from e
    return _refresh_response(result)


@router.get("/latest-run", response_model=RunStatusResponse | None)
async def get_latest_run(
    repository: str,
    branch: str | None = None,
    workflow: str | None = None,
    service=Depends(get_query_service),
) -> RunStatusResponse | None:
    """Get the newest run of a repository.

    ``branch`` and ``workflow`` narrow it; omitting them means "newest run
    overall", not "newest on the default branch". A run of another branch is
    still this repository's news, and answering with an older default-branch run
    while ignoring it reads as authoritative but wrong.
    """
    run = await service.get_latest_run(
        repository=repository,
        branch=branch,
        workflow=workflow,
    )
    if run is None:
        return None
    return _run_response(run)


@router.get("/pull-request-runs", response_model=list[RunStatusResponse])
async def get_pull_request_runs(
    repository: str,
    number: int,
    request: Request,
) -> list[RunStatusResponse]:
    """List the CI runs attributed to a pull request.

    GitHub has no PR-number filter on the runs endpoint, so the lookup reads
    the pull request for its head commit and then attributes the runs found for
    that commit. An empty list is a valid answer (a pull request with no runs,
    or one not visible to the bound token).
    """
    service = request.app.state.container.repository
    runs = await service.pull_request_runs(repository=repository, number=number)
    return [_run_response(run) for run in runs]
