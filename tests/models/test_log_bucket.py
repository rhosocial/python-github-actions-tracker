# tests/models/test_log_bucket.py
"""The bucket is one pure function, and every write and read agrees with it.

A log row's bucket answers "which month is this log from". It is the address the
read paths ask for, and it will become the unit a partition is dropped in, so
the property that matters is that the writer and the reader compute the same
value from the same input -- never that the value is pretty.

These tests pin three things:

* :func:`bucket_of` -- month granularity, UTC, and what it does with the two
  inputs that have no month (``None`` and a naive timestamp);
* the write paths -- a job takes its bucket from its own completion time, and a
  step takes its job's;
* the read paths -- a lookup that names the bucket finds the row, one that names
  a different bucket does not, and a parent that carries no bucket still reads.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from rhosocial.github_ci_tracker.models import (
    AsyncJob,
    AsyncJobLog,
    AsyncStep,
    AsyncWorkflowRun,
    bucket_of,
)


class TestBucketOf:
    def test_the_month_is_the_address(self) -> None:
        """The first of the month: a bucket compares by equality, so the day
        inside it must not leak into the value."""
        assert bucket_of(datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)) == date(2026, 9, 1)
        assert bucket_of(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)) == date(2026, 10, 1)

    def test_the_month_is_utc(self) -> None:
        """A timestamp that is the next month in its own zone is this month in
        UTC: the bucket follows the clock the rest of the service reads."""
        # 2026-10-01T01:00+09:00 is 2026-09-30T16:00Z.
        moment = datetime(2026, 10, 1, 1, 0, tzinfo=timezone(timedelta(hours=9)))
        assert bucket_of(moment) == date(2026, 9, 1)

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """The same assumption ``_past_deadline`` makes: a timestamp without a
        zone is the UTC wall clock, not the machine's local one."""
        assert bucket_of(datetime(2026, 9, 15, 12, 0)) == date(2026, 9, 1)

    def test_no_completion_time_has_no_bucket(self) -> None:
        """``None`` is the answer, not a guess: an unfinished job has no month,
        and inventing one would file its (nonexistent) log under a month."""
        assert bucket_of(None) is None


class TestTheWritePathsAgree:
    async def test_a_job_takes_the_bucket_of_its_own_completion(self, fake_client, bound) -> None:
        """The value is derived at write time, from the same completion time the
        row stores -- so a late completion cannot leave the two disagreeing."""
        from tests.core.test_facade_views import (
            _job_payload,
            _public_repo_info,
            _run_payload,
            subscribe,
        )

        fake_client.repo_info = _public_repo_info()
        repo = await subscribe(fake_client)
        run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(7001))
        job = await AsyncJob.upsert_from_remote(
            run,
            {
                **_job_payload(70010, "build"),
                "status": "completed",
                "conclusion": "success",
                "completed_at": "2026-09-15T12:00:00Z",
            },
        )

        assert job.log_bucket == bucket_of(job.completed_at) == date(2026, 9, 1)

    async def test_a_step_takes_the_bucket_of_its_job(self, fake_client, bound) -> None:
        """The step never computes it: it inherits, so a read that holds only the
        step still knows the month without fetching the job back."""
        from tests.core.test_facade_views import (
            _job_payload,
            _public_repo_info,
            _run_payload,
            subscribe,
        )

        fake_client.repo_info = _public_repo_info()
        repo = await subscribe(fake_client)
        run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(7002))
        job = await AsyncJob.upsert_from_remote(
            run,
            {
                **_job_payload(70020, "build"),
                "status": "completed",
                "completed_at": "2026-09-15T12:00:00Z",
                "steps": [{"number": 1, "name": "checkout", "status": "completed"}],
            },
        )
        step = (await AsyncStep.query().where(AsyncStep.c.number == 1).all())[0]

        assert step.log_bucket == job.log_bucket == date(2026, 9, 1)


class TestTheReadPathsAgree:
    async def test_a_log_is_found_by_the_bucket_it_was_written_with(
        self, fake_client, bound
    ) -> None:
        from tests.core.test_facade_views import (
            _job_payload,
            _public_repo_info,
            _run_payload,
            subscribe,
        )

        fake_client.repo_info = _public_repo_info()
        repo = await subscribe(fake_client)
        run = await AsyncWorkflowRun.adopt_from_remote(repo, _run_payload(7003))
        job = await AsyncJob.upsert_from_remote(
            run,
            {
                **_job_payload(70030, "build"),
                "status": "completed",
                "completed_at": "2026-09-15T12:00:00Z",
            },
        )
        await AsyncJobLog(job_id=job.id, log_content="merged\n", bucket=job.log_bucket).store()

        assert (await AsyncJobLog.find_for_job(job.id, job.log_bucket)) is not None
        # The wrong month is a real answer of "not here", not an error: once a
        # month is dropped, that is exactly what its readers must get.
        assert await AsyncJobLog.find_for_job(job.id, date(2026, 10, 1)) is None

    async def test_a_log_without_a_bucket_cannot_be_built(self) -> None:
        """The bucket is half of the row's key, so a log without one is a row
        that could not be addressed afterwards -- and the model says so at
        construction rather than letting the partitioned table refuse the write
        with a violation that names a constraint instead of the missing field.

        The corollary is the end of "a legacy row with a null bucket": such a row
        cannot exist, so the read path's ``bucket=None`` spelling now means
        "every bucket", which is what it says.
        """

        with pytest.raises(ValueError, match="bucket"):
            AsyncJobLog(job_id=1, log_content="legacy\n")
