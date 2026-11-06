# tests/core/test_step_bucket_fill.py
"""A step that lost its bucket must not cost the capture its whole run.

The step-log row's key is ``(id, bucket)`` and the bucket is the caller's to
supply, so a step object saying ``None`` aborts the write path on the first such
step -- and the capture writes every step of every job in one pass, so one stale
copy loses the whole run's step logs while the archive is still warm.

Seen once: a run finished at 08:55, its steps were written at 08:53 while the
job was still running (no month yet) and healed at 08:55:17, and the capture at
08:55:27 held the pre-healing copy for one of them. Two step logs landed, the
third raised ``ValueError: Composite primary key field 'bucket' must be
provided``, and the rest of the run's per-step text was never fetched -- which
the delivery ledger recorded, because that is what the ledger is for.

The fix is to derive the month from the job at the one place every caller reads
steps through: the job is the authority, and it is already in hand.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rhosocial.github_actions_tracker.controllers import RunData
from rhosocial.github_actions_tracker.models import (
    AsyncJob,
    AsyncRepository,
    AsyncRunAttempt,
    AsyncStep,
    AsyncStepLog,
    AsyncWorkflow,
    AsyncWorkflowRun,
)
from rhosocial.github_actions_tracker.models.log import bucket_of

REPO = "acme/demo"
RUN_ID = 4242
NOW = datetime(2026, 11, 6, 8, 55, tzinfo=timezone.utc)


async def _finished_job() -> tuple[AsyncJob, AsyncWorkflowRun]:
    """A finished job whose steps were written while it was still running."""
    repo = AsyncRepository(github_repo_id=99, full_name=REPO, owner="acme", name="demo")
    await repo.save()
    workflow = AsyncWorkflow(
        repository_id=repo.id or 0,
        workflow_id=7,
        name="CI",
        path=".github/workflows/test.yml",
    )
    await workflow.save()
    run = AsyncWorkflowRun(
        workflow_id=workflow.id or 0,
        github_run_id=RUN_ID,
        run_number=1,
        run_attempt=1,
        status="completed",
        conclusion="success",
        branch="main",
        commit_sha="0" * 40,
        event="push",
    )
    await run.save()
    attempt = AsyncRunAttempt(
        run_id=run.id or 0,
        attempt_number=1,
        status="completed",
        conclusion="success",
        started_at=NOW - timedelta(minutes=4),
        completed_at=NOW,
    )
    await attempt.save()
    job = AsyncJob(
        github_job_id=5001,
        attempt_id=attempt.id or 0,
        name="tests",
        status="completed",
        conclusion="success",
        started_at=NOW - timedelta(minutes=3),
        completed_at=NOW,
        log_bucket=bucket_of(NOW),
    )
    await job.save()
    return job, run


async def _step(job: AsyncJob, number: int, *, with_bucket: bool) -> AsyncStep:
    step = AsyncStep(
        job_id=job.id or 0,
        github_step_id=number,
        number=number,
        name=f"step {number}",
        status="completed",
        conclusion="success",
        started_at=NOW - timedelta(minutes=2),
        completed_at=NOW,
        log_bucket=job.log_bucket if with_bucket else None,
    )
    await step.save()
    return step


class TestStepsCarryTheJobsMonth:
    async def test_a_step_missing_its_bucket_gets_the_jobs(self, bound) -> None:
        job, _ = await _finished_job()
        stale = await _step(job, 1, with_bucket=False)

        store = RunData()
        steps = await store.steps(job)

        assert [s.id for s in steps] == [stale.id]
        assert steps[0].log_bucket == job.log_bucket, (
            "the job is the authority for the month; a step row written while "
            "the job was still running cannot supply it"
        )

    async def test_the_row_is_healed_too(self, bound) -> None:
        """Not just the object in hand: the next reader must not need the fix."""
        job, _ = await _finished_job()
        stale = await _step(job, 1, with_bucket=False)

        store = RunData()
        await store.steps(job)

        reloaded = await AsyncStep.find_one(AsyncStep.c.id == stale.id)
        assert reloaded is not None
        assert reloaded.log_bucket == job.log_bucket

    async def test_a_step_that_already_has_one_is_left_alone(self, bound) -> None:
        """The common path: nothing to fill, nothing written."""
        job, _ = await _finished_job()
        good = await _step(job, 1, with_bucket=True)

        store = RunData()
        steps = await store.steps(job)

        assert steps[0].log_bucket == job.log_bucket
        reloaded = await AsyncStep.find_one(AsyncStep.c.id == good.id)
        assert reloaded is not None
        assert reloaded.log_bucket == job.log_bucket


class TestTheWriteSurvivesAStaleStep:
    async def test_storing_a_step_log_does_not_raise_on_a_stale_step(self, bound) -> None:
        """The regression: one stale step must not abort the capture."""
        job, _ = await _finished_job()
        await _step(job, 1, with_bucket=False)
        await _step(job, 2, with_bucket=False)

        store = RunData()
        steps = await store.steps(job)
        for step in steps:
            await store.store_step_log(step, f"text of step {step.number}")

        stored = await AsyncStepLog.query().all()
        assert len(stored) == 2, "both steps must land; the archive is still warm"
        assert {row.bucket for row in stored} == {job.log_bucket}
