# src/rhosocial/github_actions_tracker/adapters/rest/schemas/responses.py
"""Response models for REST API.

Every run/job/step model mirrors the corresponding facade view object
(``RunRecord``, ``JobRecord``, ``StepRecord``, ``RunView``,
``RunRefreshView``) field for field; the routes project facade output onto
these models explicitly so a facade rename fails loudly instead of silently
dropping a field from the payload.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, Field, PlainSerializer

#: A timestamp on the wire: always UTC, always with an explicit offset.
#:
#: Pydantic serializes a UTC datetime as ``...Z``; every other surface of this
#: service (MCP payloads, credential metadata, the CLI) spells it ``+00:00``.
#: Same zone, two spellings -- which is exactly the kind of difference a caller
#: ends up parsing twice. One annotated type fixes it here.
UtcDateTime = Annotated[
    datetime,
    PlainSerializer(
        lambda value: value.astimezone(timezone.utc).isoformat(),
        return_type=str,
    ),
]


class TriggerWorkflowResponse(BaseModel):
    """Trigger workflow response (mirrors facade ``RunView``)."""

    run_id: int = Field(..., description="GitHub run ID")
    status: str = Field(..., description="Run status")
    message: str = Field(..., description="Status message")
    html_url: str | None = Field(None, description="GitHub run page URL")
    conclusion: str | None = Field(None, description="Run conclusion, once known")
    attempt: int | None = Field(None, description="run_attempt of this run")
    dispatched: bool = Field(
        True, description="False when an existing run was reused and nothing was dispatched"
    )


class JobSummary(BaseModel):
    """Job summary in run response (subset of facade ``JobRecord``)."""

    job_id: int
    name: str
    status: str
    conclusion: str | None = None


class ReranJob(BaseModel):
    """One failed job a selective re-run selected."""

    name: str = Field(..., description="Job name as GitHub reports it")
    conclusion: str = Field(..., description="The failing conclusion that selected it")


class RerunRunResponse(BaseModel):
    """Re-run response (mirrors facade ``RunView``)."""

    run_id: int = Field(..., description="GitHub run ID, unchanged by a re-run")
    repository: str = Field(..., description="owner/repo the run belongs to")
    status: str = Field(..., description="Run status")
    message: str = Field(..., description="Status message")
    html_url: str | None = Field(None, description="GitHub run page URL")
    attempt: int | None = Field(None, description="run_attempt after the request")
    previous_attempt: int | None = Field(
        None, description="run_attempt before the request; None for a trigger"
    )
    advanced: bool = Field(False, description="True when GitHub already reports the new attempt")
    #: Filled only by a selective re-run; empty for the whole-run one, which is
    #: the whole-run answer rather than "nothing ran".
    reran_jobs: list[ReranJob] = Field(
        default_factory=list,
        description=(
            "Failed jobs this service selected, when failed_only was asked. GitHub "
            "also re-runs the jobs they depend on, which this list does not name."
        ),
    )


class TimingsResponse(BaseModel):
    """Where a run's seconds went.

    ``duration_seconds`` is the attempt's wall clock; these split it. ``None``
    means the service has not synced the jobs that would answer, never zero.
    """

    job_span_seconds: float | None = None
    queue_seconds: float | None = None
    succeeded_job_span_seconds: float | None = None


class RunStatusResponse(BaseModel):
    """Run status response (mirrors facade ``RunRecord``)."""

    run_id: int
    run_number: int
    #: Attempt ordinal of this run; a re-run reuses the id and increments it.
    attempt: int = 1
    is_latest: bool = True
    #: Attempts GitHub reports for this run, as of the last sync.
    attempts_total: int = 1
    #: How many of those attempts are stored locally.
    attempts_cached: int = 0
    #: ``cached`` / ``partial`` / ``stale`` -- see facade ``RunRecord``.
    cache_state: str = "partial"
    status: str
    conclusion: str | None = None
    branch: str = ""
    workflow_name: str = ""
    workflow_path: str = ""
    commit_sha: str = ""
    commit_message: str = ""
    commit_author: str = ""
    commit_time: UtcDateTime | None = None
    display_title: str = ""
    head_repo: str = ""
    base_ref: str | None = None
    pr_number: int | None = None
    actor: str = ""
    event: str = ""
    created_at: UtcDateTime | None = None
    started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None
    #: When this service stored the row (ingest time, not the run's age).
    stored_at: UtcDateTime | None = None
    html_url: str = ""
    duration_seconds: float | None = None
    #: The split of ``duration_seconds``: jobs' span, queue, passed jobs' span.
    timings: TimingsResponse | None = None
    jobs: list[JobSummary] = []


class RunRefreshResponse(BaseModel):
    """Run refresh response (mirrors facade ``RunRefreshView``)."""

    run_id: int
    run_number: int
    previous_status: str | None = None
    status: str
    conclusion: str | None = None
    created: bool = Field(False, description="True when the run was adopted (first seen)")
    changed: bool = Field(False, description="True when the stored state moved forward")
    jobs_synced: int = 0
    steps_synced: int = 0
    html_url: str = ""


class StepSummary(BaseModel):
    """Step summary in job response (mirrors facade ``StepRecord``)."""

    step_id: int
    number: int
    name: str
    status: str
    conclusion: str | None = None
    started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None
    duration_seconds: float | None = None
    #: Whether this step's text was captured. ``False`` means the fetch did not
    #: happen while GitHub still served per-step files; captured logs persist.
    has_log: bool = False
    #: Lines in that log; ``None`` means "no log", not "empty".
    log_lines: int | None = None


class JobDetailResponse(BaseModel):
    """Job detail response (mirrors facade ``JobRecord``)."""

    job_id: int
    run_id: int
    name: str
    status: str
    conclusion: str | None = None
    runner_name: str | None = None
    started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None
    duration_seconds: float | None = None
    steps: list[StepSummary] = []


class StepLogSection(BaseModel):
    """One step's captured log."""

    step: int
    name: str
    conclusion: str | None = None
    content: str
    lines: int


class JobLogsResponse(BaseModel):
    """Job logs response (facade ``JobLogsView`` plus a derived line count)."""

    job_id: int
    job_name: str
    log_content: str
    line_count: int
    #: ``"job"`` for the merged document (the historical answer) or ``"step"``
    #: when the caller asked for a step / the failed steps.
    source: str = "job"
    #: Per-step sections, in step order, in step mode.
    sections: list[StepLogSection] = []
    #: Steps with no captured log; their absence is an answer ("not fetched in
    #: time"), not an error, and not an expiry of the logs that were fetched.
    steps_without_logs: list[int] = []
    #: Why ``sections`` is empty, when it is.
    reason: str | None = None


class FailureStepRefResponse(BaseModel):
    """One failed step, named without its text."""

    job_id: int
    job_name: str
    step: int
    step_name: str
    conclusion: str | None = None


class FailureGroupResponse(BaseModel):
    """One distinct failed-step text and every step that printed it."""

    content: str
    occurrences: list[FailureStepRefResponse] = []


class JobRefResponse(BaseModel):
    """A job named by id and name, without unrolling it."""

    job_id: int
    job_name: str


class RunFailuresResponse(BaseModel):
    """A run's failed-step logs, one entry per distinct text."""

    run_id: int
    jobs_failed: int
    groups: list[FailureGroupResponse] = []
    steps_without_logs: list[FailureStepRefResponse] = []
    #: Failed jobs with no failed step at all (startup failure, or cancelled
    #: before its steps existed): part of ``jobs_failed``, absent from any group.
    jobs_without_failed_steps: list[JobRefResponse] = []


class RepositoryResponse(BaseModel):
    """Repository response."""

    id: int
    full_name: str
    owner: str
    name: str
    is_private: bool
    default_branch: str
    subscribed_at: datetime


class TokenMetaResponse(BaseModel):
    """Token metadata response.

    Security invariant: this model never carries the token value; only
    metadata safe to show in listings and mutation results is exposed.
    """

    id: int
    token_name: str
    scope: str
    is_active: bool
    expires_at: UtcDateTime | None = None
    created_at: UtcDateTime | None = None
    updated_at: UtcDateTime | None = None


class WorkflowResponse(BaseModel):
    """Workflow response (facade returns the ``AsyncWorkflow`` record)."""

    id: int = Field(..., description="Local database row id")
    workflow_id: int = Field(..., description="GitHub workflow ID")
    name: str
    path: str
    state: str
