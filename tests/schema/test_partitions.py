# tests/schema/test_partitions.py
"""The partition vocabulary: which month is which, and what a name means.

These are the database-free half of :mod:`schema.partitions`. The month
arithmetic and the naming are where a subtle error would be invisible -- a
partition created for the wrong month, or named differently by two callers, both
look like a database that simply has no data -- so they are pinned here rather
than left to the integration tests that exercise a live server.

The measured behaviour behind these rules lives in
``.claude/plan/2026-11-01/partition-and-purge-plan.md``: the two log tables are
always handled as a pair, because creating one month for one table and forgetting
the other *manufactures* the failure the pair exists to prevent.
"""

from __future__ import annotations

from datetime import date

import pytest

from rhosocial.github_ci_tracker.schema.model import SCHEMA, partition_name
from rhosocial.github_ci_tracker.schema.partitions import (
    PARTITIONED_TABLES,
    _bucket_from_bound,
    month_start,
    months_between,
    next_month,
)


class TestTheMonthVocabulary:
    def test_a_month_is_addressed_by_its_first_day(self) -> None:
        """A bucket is a month start, and any day inside the month resolves to
        the same one -- otherwise "the partition for this log" could be two
        different partitions for the same month."""
        assert month_start(date(2026, 9, 1)) == date(2026, 9, 1)
        assert month_start(date(2026, 9, 30)) == date(2026, 9, 1)

    def test_a_next_month_crosses_the_year(self) -> None:
        """December's successor is January of the next year; an implementation
        that only changed the month field would stop at December."""
        assert next_month(date(2026, 11, 1)) == date(2026, 12, 1)
        assert next_month(date(2026, 12, 1)) == date(2027, 1, 1)

    def test_the_months_between_are_inclusive(self) -> None:
        """Both ends are created: the range that serves ``--ensure 2`` is this
        month plus the next two, and an exclusive end would leave the operator
        one month short of what the command promised."""
        assert months_between(date(2026, 9, 15), date(2026, 11, 3)) == (
            date(2026, 9, 1),
            date(2026, 10, 1),
            date(2026, 11, 1),
        )

    def test_a_reversed_range_is_empty_not_an_error(self) -> None:
        """A ``--drop-before`` in the future is a request to drop nothing, which
        is what it answers -- not a crash and not a silent drop of everything."""
        assert months_between(date(2026, 11, 1), date(2026, 9, 1)) == ()

    def test_a_single_month_is_its_own_range(self) -> None:
        assert months_between(date(2026, 9, 9), date(2026, 9, 9)) == (date(2026, 9, 1),)


class TestTheNames:
    def test_a_partition_is_named_after_its_table_and_month(self) -> None:
        """``<table>_YYYY_MM`` -- derived, never stored, so ``--ensure``,
        ``--list`` and the read path cannot disagree about which partition a
        month lives in."""
        assert partition_name("ct_job_logs", date(2026, 9, 1)) == "ct_job_logs_2026_09"
        assert partition_name("ct_step_logs", date(2026, 12, 1)) == "ct_step_logs_2026_12"

    def test_a_name_is_stable_across_repeated_calls(self) -> None:
        assert partition_name("ct_job_logs", date(2026, 9, 1)) == partition_name(
            "ct_job_logs", month_start(date(2026, 9, 20))
        )


class TestThePair:
    def test_both_log_tables_are_partitioned_together(self) -> None:
        """The capture path writes both tables, so one without the other
        *manufactures* the failure rather than preventing it."""
        assert PARTITIONED_TABLES == ("ct_job_logs", "ct_step_logs")

    def test_the_partitioned_tables_are_the_schema_s_log_tables(self) -> None:
        assert set(PARTITIONED_TABLES) <= {table.name for table in SCHEMA.all_tables()}

    def test_no_other_table_is_in_the_pair(self) -> None:
        """The delivery ledger is queried by time and dropped by age; a month of
        it is a metadata operation nobody needs yet."""
        assert "ct_webhook_delivery" not in PARTITIONED_TABLES
        assert "ct_jobs" not in PARTITIONED_TABLES


class TestTheBounds:
    def test_the_bound_reads_as_the_month_it_starts(self) -> None:
        """``FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')`` is the September
        partition: the first date inside the bound is the bucket."""
        assert _bucket_from_bound("FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')") == date(
            2026, 9, 1
        )


@pytest.mark.parametrize("bound", [None, "", "FOR VALUES FROM (INCLUSIVE)"])
def test_an_unreadable_bound_is_none_not_a_guess(bound: object) -> None:
    """A partition whose bound this parser cannot read keeps ``None``: the list
    still names it, and its size and row figure are not lost."""
    assert _bucket_from_bound(bound) is None
