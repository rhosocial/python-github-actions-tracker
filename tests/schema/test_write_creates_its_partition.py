# tests/schema/test_write_creates_its_partition.py
"""Each flow leaves the state it needs, and assumes nothing about the other's.

A partitioned table with no partition accepts no insert, and PostgreSQL allows
that state: ``PARTITION BY RANGE`` creates the parent and nothing else, so a
schema can look complete -- tables present, every probe green -- and fail at the
first capture with ``no partition of relation "ghat_step_logs" found for row``.
That reached the surface as ``IntegrityError`` in the ledger's "capture failed".

Two duties follow, and each is pinned here on its own:

* :func:`create_all` -- what ``db init`` calls -- leaves a schema that is
  writable *now*, so a fresh deployment's first capture cannot fail on a
  partition;
* the write path creates the month *it* writes in, because a log can belong to a
  month no boot prepared (a backfill, or a deployment created long ago).

The month is read off the execution's **trigger time**
(``ghat_run_attempts.github_created_at``), which is what the log rows carry as
``attempt_created_at`` -- not the moment the capture happened.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from rhosocial.github_actions_tracker.schema import create_all, partitions

#: A month no boot would prepare: months behind "now", so it can only exist
#: because a write created it.
OLD_MONTH = date(2026, 3, 1)
OLD_TIME = datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc)


async def _backend():
    """The backend the ``bound`` fixture bound the models to."""
    from rhosocial.github_actions_tracker.models import AsyncJob

    return AsyncJob.__backend__


@pytest.fixture()
async def freshly_initialized(bound):
    """The state ``db init`` leaves, minus nothing.

    Re-running ``create_all`` proves the function itself is what produces a
    writable schema, rather than the suite's own scaffolding.
    """
    backend = await _backend()
    for table in ("ghat_step_logs", "ghat_job_logs"):
        await backend.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    await create_all(backend)
    return backend


async def _execution(triggered_at: datetime):
    """A run, its attempt, one job and one step -- the chain a log hangs off."""
    from rhosocial.github_actions_tracker.models import (
        AsyncJob,
        AsyncRepository,
        AsyncRunAttempt,
        AsyncStep,
        AsyncWorkflow,
        AsyncWorkflowRun,
    )

    repository = AsyncRepository(github_repo_id=1, full_name="acme/demo", owner="acme", name="demo")
    await repository.save()
    workflow = AsyncWorkflow(
        repository_id=repository.id or 0,
        workflow_id=77,
        name="ci",
        path=".github/workflows/ci.yml",
    )
    await workflow.save()
    run = AsyncWorkflowRun(
        workflow_id=workflow.id or 0,
        github_run_id=99,
        run_number=1,
        event="push",
        branch="main",
        commit_sha="a" * 40,
    )
    await run.save()
    attempt = AsyncRunAttempt(
        run_id=run.id or 0,
        attempt_number=1,
        status="completed",
        conclusion="success",
        github_created_at=triggered_at,
    )
    await attempt.save()
    job = AsyncJob(attempt_id=attempt.id or 0, github_job_id=11, name="build", status="completed")
    await job.save()
    step = AsyncStep(
        job_id=job.id or 0, github_step_id=1, number=1, name="test", status="completed"
    )
    await step.save()
    return attempt, job, step


async def _write_a_step(backend, triggered_at: datetime, text: str = "step text"):
    """Write one step log for the month of ``triggered_at``."""
    from rhosocial.github_actions_tracker.controllers.run import RunData

    attempt, _job, step = await _execution(triggered_at)
    await RunData().store_step_log(step, text, attempt)
    return attempt, step


class TestInitLeavesAWritableSchema:
    async def test_a_fresh_schema_has_the_current_month(self, bound, freshly_initialized) -> None:
        """``db init`` on an empty database used to leave ``No partitions exist``
        -- tables, and no way to write a row."""
        report = await partitions.list_partitions(freshly_initialized)

        this_month = datetime.now(timezone.utc).date().replace(day=1)
        assert this_month in {info.bucket for info in report.partitions}

    async def test_a_fresh_schema_also_has_next_month(self, bound, freshly_initialized) -> None:
        """A run that finishes just after midnight rolls into a month this boot
        did not prepare; the boundary is where the failure used to land."""
        report = await partitions.list_partitions(freshly_initialized)

        this_month = datetime.now(timezone.utc).date().replace(day=1)
        next_month = partitions.next_month(this_month)
        assert next_month in {info.bucket for info in report.partitions}

    async def test_a_fresh_schema_accepts_a_write_immediately(
        self, bound, freshly_initialized
    ) -> None:
        """The state the init promises, stated as the operation it protects."""
        this_month = datetime.now(timezone.utc).date().replace(day=1)
        await _write_a_step(freshly_initialized, this_month)

        rows = await freshly_initialized.execute("SELECT 1 AS ok")
        assert (rows.data or [{}])[0].get("ok") == 1


class TestTheWriteCreatesItsOwnMonth:
    async def test_a_write_into_a_month_no_boot_prepared_succeeds(
        self, bound, freshly_initialized
    ) -> None:
        """Before the fix this raised ``IntegrityError: no partition of relation
        "ghat_step_logs" found for row``, and the ledger recorded it as
        "capture failed" -- true, and useless to whoever read it."""
        await _write_a_step(freshly_initialized, OLD_TIME)

        rows = await freshly_initialized.execute(
            "SELECT log_content FROM ghat_step_logs WHERE attempt_created_at = %s",
            (OLD_TIME,),
        )
        assert ((rows.data or [{}])[0]).get("log_content") == "step text"

    async def test_that_month_is_now_a_partition(self, bound, freshly_initialized) -> None:
        """The write leaves the month attached, so the read path finds it."""
        await _write_a_step(freshly_initialized, OLD_TIME)

        report = await partitions.list_partitions(freshly_initialized)
        assert OLD_MONTH in {info.bucket for info in report.partitions}

    async def test_a_second_write_into_the_same_month_still_works(
        self, bound, freshly_initialized
    ) -> None:
        """``IF NOT EXISTS`` is what makes the before-every-write shape safe."""
        attempt, step = await _write_a_step(freshly_initialized, OLD_TIME, "first")
        from rhosocial.github_actions_tracker.controllers.run import RunData

        await RunData().store_step_log(step, "second", attempt)

        rows = await freshly_initialized.execute(
            "SELECT log_content FROM ghat_step_logs WHERE attempt_created_at = %s",
            (OLD_TIME,),
        )
        assert ((rows.data or [{}])[0]).get("log_content") == "second"
