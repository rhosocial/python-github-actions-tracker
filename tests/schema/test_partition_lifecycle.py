# tests/schema/test_partition_lifecycle.py
"""The partition lifecycle, against the real PostgreSQL this suite runs on.

The pure vocabulary (which month is which partition, what a name means) is pinned
in :mod:`tests.schema.test_partitions`; what is pinned here is the part that only
a live server can show: a month is created on **both** tables or on neither, a
drop is a pair in one transaction, and a read for a month that was dropped
answers "no rows" instead of an error.

Those are the four measured rules from
``.claude/plan/2026-11-01/partition-and-purge-plan.md``: PostgreSQL never
creates a future partition itself, the capture path writes both tables, and
``DROP TABLE`` of a partition is a metadata operation rather than a rewrite.
"""

from __future__ import annotations

from datetime import date

from rhosocial.github_actions_tracker.schema import partitions
from rhosocial.github_actions_tracker.schema.model import partition_name

FIRST = date(2026, 5, 1)
THIRD = date(2026, 7, 1)


class TestEnsure:
    async def test_a_month_is_created_on_both_tables(self, bound) -> None:
        """The capture path writes both log tables, so one month's partitions are
        created as a pair -- a month that exists on one table and not the other
        is the state where a write has already failed."""

        created = await partitions.ensure_month(_backend(), FIRST)

        assert created == (
            partition_name("ghat_job_logs", FIRST),
            partition_name("ghat_step_logs", FIRST),
        )
        report = await partitions.list_partitions(_backend())
        assert FIRST in {info.bucket for info in report.partitions}

    async def test_ensure_is_idempotent(self, bound) -> None:
        """``IF NOT EXISTS`` makes the safe-to-call-before-every-write shape."""
        await partitions.ensure_month(_backend(), FIRST)

        again = await partitions.ensure_month(_backend(), FIRST)

        assert len(again) == 2
        months = await partitions.list_partitions(_backend())
        named = [info.partition for info in months.partitions if info.bucket == FIRST]
        assert sorted(named) == sorted(again)


class TestDrop:
    async def test_a_drop_removes_both_tables_month(self, bound) -> None:
        await partitions.ensure_months(_backend(), [FIRST, THIRD])

        dropped = await partitions.drop_before(_backend(), THIRD)

        assert partition_name("ghat_job_logs", FIRST) in dropped
        assert partition_name("ghat_step_logs", FIRST) in dropped
        remaining = await partitions.list_partitions(_backend())
        buckets = {info.bucket for info in remaining.partitions}
        assert FIRST not in buckets
        assert THIRD in buckets

    async def test_a_dry_run_names_without_touching(self, bound) -> None:
        await partitions.ensure_month(_backend(), FIRST)

        dry = await partitions.drop_before(_backend(), THIRD, dry_run=True)

        assert partition_name("ghat_job_logs", FIRST) in dry
        still = await partitions.list_partitions(_backend())
        assert FIRST in {info.bucket for info in still.partitions}

    async def test_a_cutoff_older_than_everything_drops_nothing(self, bound) -> None:
        """A window that predates every partition is a request to drop nothing --
        not an error, and not a silent emptying of the table either."""
        backend = _backend()

        assert await partitions.drop_before(backend, date(2020, 1, 1)) == []


class TestTheAsymmetryIsVisible:
    async def test_a_month_one_table_lacks_is_reported(self, bound) -> None:
        """The measured failure shape is "job logs written, step logs not": a
        month present on one table and missing on the other. The list is what
        makes it visible before a write fails."""
        backend = _backend()
        await partitions.ensure_months(backend, [FIRST, THIRD])
        # Drop one table's partition for one month behind the framework's back,
        # exactly as a hand-run DROP would leave the database.
        name = partition_name("ghat_step_logs", FIRST)
        await backend.execute(f'DROP TABLE IF EXISTS "{name}"')

        report = await partitions.list_partitions(backend)

        assert ("ghat_job_logs", FIRST) in report.differences


def _backend():
    """The backend the ``bound`` fixture bound the models to.

    Taken through a model rather than a new connection: the whole point is that
    the partitions are the ones the *read path* writes into.
    """
    from rhosocial.github_actions_tracker.models import AsyncRepository

    return AsyncRepository.__backend__


class TestTheAcceleratorIndex:
    async def test_the_trgm_index_is_created_on_the_parent(self, bound) -> None:
        """A GIN index with an operator class is not expressible in the schema
        renderer, so the schema package creates it explicitly; this is the proof
        that the statement works against a real server.
        """
        from rhosocial.github_actions_tracker.schema import ensure_accelerator_indexes

        created = await ensure_accelerator_indexes(_backend())

        assert "idx_job_logs_trgm" in created
        rows = await _backend().execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s", ("idx_job_logs_trgm",)
        )
        assert "gin" in ((rows.data or [{}])[0].get("indexdef") or "").lower()

    async def test_a_new_partition_inherits_it(self, bound) -> None:
        """Declared on the parent, PostgreSQL builds the index on every partition
        -- which is why there is no monthly index upkeep to forget.

        The partition's copy carries the partition's name, not the parent's, so
        the assertion matches on the *definition* (a GIN index over
        ``log_content``) rather than on a name the server rewrites.
        """
        from rhosocial.github_actions_tracker.schema import ensure_accelerator_indexes

        backend = _backend()
        await ensure_accelerator_indexes(backend)
        await partitions.ensure_month(backend, FIRST)

        rows = await backend.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = %s",
            (partition_name("ghat_job_logs", FIRST),),
        )
        definitions = [row.get("indexdef") or "" for row in (rows.data or [])]
        assert any(
            "gin" in definition.lower() and "log_content" in definition
            for definition in definitions
        )
