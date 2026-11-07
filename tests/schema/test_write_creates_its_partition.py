# tests/schema/test_write_creates_its_partition.py
"""Each flow leaves the state it needs, and assumes nothing about the other's.

The failure this pins is one the whole system reported "capture failed" for
without naming a cause: PostgreSQL allows a ``PARTITION BY RANGE`` table to exist
with *no* partitions, and accepts no insert into one -- the server answers
``no partition of relation "ghat_step_logs" found for row``, which reaches the
surface as an ``IntegrityError`` in the delivery ledger's ``reason`` and costs
every step log of that month. ``db init`` created the tables and nothing created
a month, and the capture path never asked for one either -- which is why
``db partitions --list`` on the live NAS answered ``No partitions exist``.

Two duties follow, and each is pinned here on its own:

* :func:`create_all` -- what ``db init`` calls -- leaves a schema that is
  writable *now*, so a fresh deployment's first capture cannot fail on a
  partition;
* the write path creates the month *it* writes in, because a log can belong to
  a month no boot prepared (a backfill, or a deployment created long ago).

``ensure_month``'s docstring says "safe to call before every capture -- that is
the only place the 'future month' failure can be prevented". This file is the
test that both halves are true, rather than each waiting for the other.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from rhosocial.github_actions_tracker.schema import create_all, partitions

#: A month no boot would prepare: months behind "now", so it can only exist
#: because a write created it.
OLD_MONTH = date(2026, 3, 1)


async def _backend():
    """The backend the ``bound`` fixture bound the models to.

    Taken through a model rather than a new connection: the point is that the
    partitions are the ones the *read path* writes into.
    """
    from rhosocial.github_actions_tracker.models import AsyncJob

    return AsyncJob.__backend__


@pytest.fixture()
async def freshly_initialized(bound):
    """The state ``db init`` leaves, minus nothing.

    The suite's fixture already creates the schema; re-running ``create_all``
    proves the function itself is what produces a writable schema, rather than
    the suite's own scaffolding.
    """
    backend = await _backend()
    for table in ("ghat_step_logs", "ghat_job_logs"):
        await backend.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    await create_all(backend)
    return backend


async def _write_a_step(backend, month: date, text: str = "step text") -> None:
    """Write one step log for ``month``, through the controller the capture uses."""
    from rhosocial.github_actions_tracker.controllers.run import RunData

    step = await _step_in(month)
    await RunData().store_step_log(step, text)
    assert step.id is not None


async def _step_in(month: date):
    """A step row that exists, in a job that finished in ``month``.

    Built the way the capture path builds it: a finished run adopted from a
    payload, so the step's ``log_bucket`` is the month the model derives from
    the job's completion -- not a value a test chose. (A hand-made step fails
    the foreign key, which is how the first draft of this file failed.)
    """
    from conftest import FakeGithub, subscribe

    from rhosocial.github_actions_tracker.controllers.run import RunController, RunData
    from rhosocial.github_actions_tracker.models import AsyncStep
    from rhosocial.github_actions_tracker.services.github import GithubRuns

    finished_at = datetime.combine(month, datetime.min.time(), tzinfo=timezone.utc) + timedelta(
        hours=1
    )
    fake = FakeGithub()
    fake.repo_info = {
        "id": 91,
        "owner": {"login": "acme"},
        "name": "demo",
        "full_name": "acme/demo",
        "is_private": False,
        "default_branch": "main",
    }
    fake.jobs = [
        {
            "id": 4_200_001,
            "run_id": 9_100_001,
            "name": "build",
            "status": "completed",
            "conclusion": "success",
            "started_at": (finished_at - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "completed_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "steps": [
                {
                    "number": 1,
                    "name": "checkout",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": (finished_at - timedelta(minutes=5)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "completed_at": (finished_at - timedelta(minutes=4)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
            ],
        }
    ]
    fake.runs = [
        {
            "id": 9_100_001,
            "run_number": 1,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "event": "push",
            "head_branch": "main",
            "head_sha": "0" * 40,
            "created_at": (finished_at - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    ]
    repository = await subscribe(fake, "acme/demo")
    await RunController(runs=GithubRuns(fake), runs_data=RunData()).fetch(repository, 9_100_001)
    steps = await AsyncStep.find_all(AsyncStep.c.log_bucket == month)
    assert steps, "the adopted run should have left a step row in this month"
    return steps[0]


class TestInitLeavesAWritableSchema:
    async def test_a_fresh_schema_has_the_current_month(self, bound, freshly_initialized) -> None:
        """The half that was missing: ``db init`` on an empty database used to
        leave ``No partitions exist`` -- tables, and no way to write a row."""
        report = await partitions.list_partitions(freshly_initialized)

        buckets = {info.bucket for info in report.partitions}
        this_month = datetime.now(timezone.utc).date().replace(day=1)
        assert this_month in buckets

    async def test_a_fresh_schema_also_has_next_month(self, bound, freshly_initialized) -> None:
        """A run that finishes just after midnight rolls over into a month this
        boot did not prepare; the boundary is where the failure used to land."""
        report = await partitions.list_partitions(freshly_initialized)

        buckets = {info.bucket for info in report.partitions}
        next_month = (datetime.now(timezone.utc).date().replace(day=1)) + timedelta(days=32)
        assert next_month.replace(day=1) in buckets

    async def test_a_fresh_schema_accepts_a_write_immediately(
        self, bound, freshly_initialized
    ) -> None:
        """The state the init promises, stated as the operation it protects."""
        await _write_a_step(freshly_initialized, datetime.now(timezone.utc).date().replace(day=1))

        rows = await freshly_initialized.execute("SELECT 1 AS ok")
        assert (rows.data or [{}])[0].get("ok") == 1


class TestTheWriteCreatesItsOwnMonth:
    async def test_a_write_into_a_month_no_boot_prepared_succeeds(
        self, bound, freshly_initialized
    ) -> None:
        """The other half: the write does not rely on init having known this
        month. Before the fix this raised ``IntegrityError: no partition of
        relation "ghat_step_logs" found for row``, and the ledger recorded it as
        "capture failed" -- true, and useless to whoever read it."""
        backend = await _backend()
        await _write_a_step(backend, OLD_MONTH)

        rows = await backend.execute(
            "SELECT log_content FROM ghat_step_logs WHERE bucket = %s", (OLD_MONTH,)
        )
        assert ((rows.data or [{}])[0]).get("log_content") == "step text"

    async def test_that_month_is_now_a_partition(self, bound, freshly_initialized) -> None:
        """The write leaves the month attached, so the read path finds it."""
        backend = await _backend()
        await _write_a_step(backend, OLD_MONTH)

        report = await partitions.list_partitions(backend)
        assert OLD_MONTH in {info.bucket for info in report.partitions}

    async def test_it_creates_the_month_it_writes_not_every_month(
        self, bound, freshly_initialized
    ) -> None:
        """A partition is storage: creating a month per read would grow the table
        for months that will never hold a row."""
        backend = await _backend()
        await _write_a_step(backend, OLD_MONTH)

        rows = await backend.execute(
            "SELECT bucket FROM ghat_job_logs WHERE bucket <> %s", (OLD_MONTH,)
        )
        assert (rows.data or []) == []

    async def test_a_second_write_into_the_same_month_still_works(
        self, bound, freshly_initialized
    ) -> None:
        """``IF NOT EXISTS`` is what makes the before-every-write shape safe."""
        backend = await _backend()
        await _write_a_step(backend, OLD_MONTH, "first")
        await _write_a_step(backend, OLD_MONTH, "second")

        rows = await backend.execute(
            "SELECT log_content FROM ghat_step_logs WHERE bucket = %s", (OLD_MONTH,)
        )
        assert ((rows.data or [{}])[0]).get("log_content") == "second"
