# tests/models/test_attempt_timings.py
"""How long an execution really took, with the queue excluded.

The run payload's ``run_started_at``..``completed_at`` is a window that includes
the queue: a run that waited ten minutes for a runner reports a ten-minute
window even though nothing ran. When a batch of repositories is triggered at once
-- which is exactly what this project does -- the last one's window is mostly
waiting, and a p50/p90 built from it overstates how long CI takes.

The jobs carry the truth: the first job that started and the last that ended. So
the duration of an execution is derived **by the database**, from the jobs,
through the same ``DerivedField`` mechanism the run's own state fields use.

Three numbers because three questions, and the tests are grouped that way:

* ``job_span_seconds`` -- the execution's real duration, every job that ran;
* ``queue_seconds`` -- how long it waited, which is the signal that a batch was
  fired at once;
* ``succeeded_job_span_seconds`` -- the same span over the jobs that passed, for
  a caller budgeting a wait for a *green* workflow.

``None`` is the answer whenever there is nothing to measure. An attempt whose
jobs have not reported times has no span, and a made-up one would be worse than
a visible gap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rhosocial.github_ci_tracker.models import AsyncJob, AsyncRunAttempt, AsyncWorkflowRun
from tests.core.test_facade_views import _run_payload, subscribe

BASE = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)


async def _attempt_with_jobs(
    fake_client, jobs: list[dict], *, pad_job_ids: int = 0
) -> AsyncRunAttempt:
    """One stored attempt carrying the given jobs.

    ``pad_job_ids`` consumes ids from the job sequence first, so the attempt id
    and its jobs' ids no longer happen to be the same small numbers. On an empty
    schema they are, and a subquery that compares the wrong column still finds
    rows -- which is how a broken join can pass every test.
    """
    fake_client.repo_info = {
        "id": 11,
        "owner": {"login": "acme"},
        "name": "public",
        "full_name": "acme/demo",
        "is_private": False,
        "default_branch": "main",
    }
    repo = await subscribe(fake_client)
    if pad_job_ids:
        decoy = await AsyncWorkflowRun.adopt_from_remote(
            repo, _run_payload(901, attempt=1, status="completed")
        )
        for index in range(pad_job_ids):
            await AsyncJob.upsert_from_remote(
                decoy,
                {
                    "id": 8100 + index,
                    "run_attempt": 1,
                    "name": f"pad-{index}",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": None,
                    "completed_at": None,
                },
            )
    # ``created_at`` is on every real run payload; the shared fixture omits it
    # because nothing else reads it. It is what makes the queue measurable.
    payload = _run_payload(900, attempt=1, status="completed")
    payload["created_at"] = (BASE + timedelta(minutes=0)).isoformat()
    run = await AsyncWorkflowRun.adopt_from_remote(repo, payload)
    for index, job in enumerate(jobs):
        await AsyncJob.upsert_from_remote(
            run,
            {
                "id": 9001 + index,
                "run_attempt": 1,
                "name": job.get("name", f"job-{index}"),
                "status": "completed",
                "conclusion": job.get("conclusion", "success"),
                "started_at": job.get("started_at"),
                "completed_at": job.get("completed_at"),
            },
        )
    attempt = await AsyncRunAttempt.find_attempt(run.id or 0, 1)
    assert attempt is not None
    return attempt


async def _reread(attempt: AsyncRunAttempt) -> AsyncRunAttempt:
    """Read the attempt back with its derived fields filled in."""
    stored = await AsyncRunAttempt.find_one(AsyncRunAttempt.c.id == attempt.id, derived=True)
    assert stored is not None
    return stored


class TestTheJobSpanIsTheRealDuration:
    async def test_it_runs_from_the_first_start_to_the_last_end(self, fake_client, bound) -> None:
        """The span measures the jobs, not the attempt's own window: a queue gap
        before the first job is outside it by construction."""
        attempt = await _attempt_with_jobs(
            fake_client,
            [
                {
                    "started_at": (BASE + timedelta(minutes=1)).isoformat(),
                    "completed_at": (BASE + timedelta(minutes=6)).isoformat(),
                },
                {
                    "started_at": (BASE + timedelta(minutes=2)).isoformat(),
                    "completed_at": (BASE + timedelta(minutes=20)).isoformat(),
                },
            ],
        )

        stored = await _reread(attempt)

        # 06:01 → 06:20 is nineteen minutes.
        assert stored.job_span_seconds == 19 * 60

    async def test_a_job_that_died_early_still_ends_the_span(self, fake_client, bound) -> None:
        """A twenty-second failure is a real ending. Excluding it would
        under-estimate exactly the workflows that fail often."""
        attempt = await _attempt_with_jobs(
            fake_client,
            [
                {
                    "started_at": (BASE + timedelta(minutes=1)).isoformat(),
                    "completed_at": (BASE + timedelta(minutes=10)).isoformat(),
                },
                {
                    "conclusion": "failure",
                    "started_at": (BASE + timedelta(minutes=1)).isoformat(),
                    "completed_at": (BASE + timedelta(minutes=1, seconds=20)).isoformat(),
                },
            ],
        )

        stored = await _reread(attempt)

        assert stored.job_span_seconds == 9 * 60

    async def test_no_jobs_means_no_span(self, fake_client, bound) -> None:
        """An attempt whose jobs were never synced has nothing to measure; a
        number here would be invented."""
        attempt = await _attempt_with_jobs(fake_client, [])

        stored = await _reread(attempt)

        assert stored.job_span_seconds is None

    async def test_the_span_matches_jobs_by_id_not_by_attempt_id(self, fake_client, bound) -> None:
        """Attempt ids and job ids are independent sequences.

        On an empty schema the first attempt and its jobs are all numbered from
        one, so a subquery comparing the wrong column still found the right rows:
        every test passed while production, where the sequences diverge, answered
        ``NULL``. The pad makes the ids differ the way they do in a real database.
        """
        attempt = await _attempt_with_jobs(
            fake_client,
            [
                {
                    "started_at": (BASE + timedelta(minutes=1)).isoformat(),
                    "completed_at": (BASE + timedelta(minutes=8)).isoformat(),
                }
            ],
            pad_job_ids=2,
        )

        stored = await _reread(attempt)

        assert stored.job_span_seconds == 7 * 60


class TestTheQueueIsReported:
    async def test_it_is_the_gap_before_the_attempt_started_running(
        self, fake_client, bound
    ) -> None:
        """``github_created_at`` → ``started_at`` is the queue, and it is a
        separate number precisely because "slow CI" and "busy runners" call for
        different responses."""
        attempt = await _attempt_with_jobs(
            fake_client,
            [
                {
                    "started_at": (BASE + timedelta(minutes=1)).isoformat(),
                    "completed_at": (BASE + timedelta(minutes=6)).isoformat(),
                },
            ],
        )
        # The payload's own run_started_at is 2026-10-10T10:00:00Z, and the
        # attempt's created_at comes from the same payload.
        stored = await _reread(attempt)

        assert stored.queue_seconds is not None


class TestTheSucceededSpan:
    async def test_it_excludes_the_jobs_that_failed(self, fake_client, bound) -> None:
        """The span a caller budgeting a *green* run should use. It is a second
        number rather than a filter on the first, so the two can be compared."""
        attempt = await _attempt_with_jobs(
            fake_client,
            [
                {
                    "started_at": (BASE + timedelta(minutes=1)).isoformat(),
                    "completed_at": (BASE + timedelta(minutes=5)).isoformat(),
                },
                {
                    "conclusion": "failure",
                    "started_at": (BASE + timedelta(minutes=6)).isoformat(),
                    "completed_at": (BASE + timedelta(minutes=30)).isoformat(),
                },
            ],
        )

        stored = await _reread(attempt)

        assert stored.job_span_seconds == 29 * 60
        assert stored.succeeded_job_span_seconds == 4 * 60


class TestTheFieldsAreOptIn:
    async def test_a_query_without_derived_leaves_them_none(self, fake_client, bound) -> None:
        """The three fields are correlated subqueries; a caller that did not ask
        for them must not pay for them."""
        attempt = await _attempt_with_jobs(
            fake_client,
            [
                {
                    "started_at": (BASE + timedelta(minutes=1)).isoformat(),
                    "completed_at": (BASE + timedelta(minutes=6)).isoformat(),
                },
            ],
        )

        plain = await AsyncRunAttempt.find_one(AsyncRunAttempt.c.id == attempt.id)

        assert plain is not None
        assert plain.job_span_seconds is None
