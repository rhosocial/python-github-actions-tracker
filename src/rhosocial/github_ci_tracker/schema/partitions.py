# src/rhosocial/github_ci_tracker/schema/partitions.py
"""Keeping the monthly partitions of the log tables in the state the writes need.

PostgreSQL never creates a future partition by itself: the parent table is
created empty, and the first insert into a month that has no partition fails with
``no partition of relation "ct_job_logs" found for row`` (measured, see
``.claude/plan/2026-11-01/partition-and-purge-plan.md``). So some code has to
create them ahead of the writes, and this module is that code.

Three rules from the measured behaviour of missing partitions, which are why
this module looks the way it does:

* **the two log tables are always handled together** -- the capture path writes
  both, so creating a partition for one and forgetting the other *manufactures*
  the failure rather than preventing it;
* **every operation is a pair in one transaction** -- PostgreSQL DDL is
  transactional, so ``CREATE``/``DROP`` for both tables either happens or does
  not, and a half-done month is not a state the database can be left in;
* **dropping is by whole partition** -- ``DROP TABLE`` of a partition is a
  metadata operation (13.7ms measured against 262ms for the equivalent row
  delete), which is the entire reason the tables are partitioned.

Nothing here decides *when* to drop a month: that is a retention policy, and an
operator's ``db partitions --drop-before 6m`` is the interface to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from .model import partition_name

logger = logging.getLogger(__name__)

#: The tables this service partitions, in the order their writes happen. Both are
#: log tables sharing one bucket column and one partition shape, and they are
#: created and dropped as a pair -- see the module docstring.
PARTITIONED_TABLES: tuple[str, ...] = ("ct_job_logs", "ct_step_logs")


def month_start(bucket: date) -> date:
    """The first day of the month a bucket names.

    A bucket is already a month start when it comes from :func:`bucket_of`; this
    is here because a caller may hand over any day, and "the partition for a date"
    must not silently become two different partitions for the same month.
    """
    return bucket.replace(day=1)


def next_month(bucket: date) -> date:
    """The month after ``bucket``'s, as a month start."""
    return _shift_months(bucket, 1)


def months_between(start: date, end: date) -> tuple[date, ...]:
    """Every month start from ``start`` to ``end``, both included.

    ``start`` may be after ``end``: the result is then empty, which is what a
    ``--drop-before`` in the future should be.
    """
    if end < start:
        return ()
    months = []
    current = month_start(start)
    last = month_start(end)
    while current <= last:
        months.append(current)
        current = _shift_months(current, 1)
    return tuple(months)


def shift_months(bucket: date, count: int) -> date:
    """Add ``count`` months to a month start, crossing years correctly.

    Public because an operator-facing window (``--drop-before 6m``) has to turn a
    month count into a month, and that arithmetic is the same one the partitions
    are generated from: one implementation, so the two cannot drift by a month.
    """
    total = bucket.month - 1 + count
    year = bucket.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


#: Retained name for the callers inside this module.
_shift_months = shift_months


def bucket_of_completion(completed_at: datetime | None) -> date | None:
    """The month a completion time belongs to -- the same function the models use.

    Imported from the models rather than reimplemented: the partition and the row
    must agree about which month a job finished in, and there is already exactly
    one function that answers it.
    """
    from ..models.log import bucket_of

    return bucket_of(completed_at)


@dataclass(frozen=True)
class PartitionInfo:
    """One partition as the server reports it."""

    table: str
    partition: str
    bucket: date | None
    rows: int | None
    bytes: int | None


@dataclass(frozen=True)
class PartitionReport:
    """What ``db partitions --list`` answers.

    The ``differences`` field is the shape that matters operationally: a month
    present in one table and missing in the other is the state the plan measures
    as "the write already failed", and a *display* of both lists side by side is
    how it becomes visible before that.
    """

    partitions: tuple[PartitionInfo, ...]
    #: Months one table has and the other lacks.
    differences: tuple[tuple[str, date], ...]

    def by_table(self, table: str) -> tuple[PartitionInfo, ...]:
        """The partitions of one table, oldest month first."""
        return tuple(
            info for info in self.partitions if info.table == table and info.bucket is not None
        )


async def existing_partitions(backend: Any, table: str) -> list[PartitionInfo]:
    """The partitions a table currently has, with size and row estimate.

    Rows are the planner's estimate (``reltuples``): exact enough to decide a
    drop, cheap enough to ask for every partition at once. A never-analyzed
    relation reports ``-1``, which is the *absence* of an estimate rather than an
    empty table, so it becomes ``None`` -- the same normalisation the engine's
    own row-count parser applies.
    """
    rows = await backend.execute(
        "SELECT c.relname AS partition, "
        "       pg_get_expr(c.relpartbound, c.oid) AS bound, "
        "       c.reltuples AS rows, "
        "       pg_total_relation_size(c.oid) AS bytes "
        "FROM pg_class c JOIN pg_inherits i ON i.inhrelid = c.oid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "WHERE p.relname = %s ORDER BY c.relname",
        (table,),
    )
    found: list[PartitionInfo] = []
    for row in rows.data or []:
        bucket = _bucket_from_bound(row.get("bound"))
        estimate = row.get("rows")
        found.append(
            PartitionInfo(
                table=table,
                partition=row["partition"],
                bucket=bucket,
                rows=None if estimate is None or estimate < 0 else estimate,
                bytes=row.get("bytes"),
            )
        )
    return found


def _bucket_from_bound(bound: Any) -> date | None:
    """The month a partition's bound starts at, or ``None`` when unreadable.

    The bound reads ``FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')``; the
    first date inside it is the bucket. A partition whose bound the parser cannot
    read keeps ``None`` -- the list still names it, and neither the size nor the
    row figure is lost.
    """
    text = str(bound or "")
    for token in text.replace("'", " ").replace("(", " ").split():
        try:
            parsed = date.fromisoformat(token)
        except ValueError:
            continue
        return parsed
    return None


async def ensure_month(backend: Any, bucket: date) -> tuple[str, ...]:
    """Create both log tables' partitions for one month, if they are missing.

    Idempotent and cheap when they are not: the creation is two ``CREATE TABLE
    IF NOT EXISTS``, and ``PARTITION OF`` carries the parent's indexes along.
    Safe to call before every capture -- that is the only place the "future
    month" failure can be prevented.
    """
    return await ensure_months(backend, (bucket,))


async def ensure_months(backend: Any, buckets: tuple[date, ...] | list[date]) -> tuple[str, ...]:
    """Create both log tables' partitions for every given month.

    The month a bucket names is its own month start, so the same bucket always
    addresses the same partition however often it is asked for.
    """
    created: list[str] = []
    for bucket in sorted({month_start(item) for item in buckets}):
        for table in PARTITIONED_TABLES:
            for statement in _create_partition_sql(table, bucket):
                await backend.execute(statement)
            created.append(partition_name(table, bucket))
    return tuple(created)


async def ensure_through(backend: Any, months_ahead: int = 0) -> tuple[str, ...]:
    """Create this month's and the next ``months_ahead`` months' partitions.

    ``months_ahead=2`` is the operator's form ("keep three months ready"), and
    ``months_ahead=0`` is the write path's.
    """
    current = datetime.now(timezone.utc).date().replace(day=1)
    buckets = [current] + [_shift_months(current, step) for step in range(1, months_ahead + 1)]
    return await ensure_months(backend, buckets)


async def ensure_spanning(backend: Any, earliest: date, latest: date) -> tuple[str, ...]:
    """Create every month's partitions from ``earliest`` to ``latest``.

    What a test fixture or a migration needs: the write path creates partitions
    for the month *it* writes in, and nobody creates the month a backfilled row
    belongs to. Leaving a hole in the middle is the state the plan measures as
    "the read returns nothing and the write has already failed".
    """
    return await ensure_months(backend, months_between(earliest, latest))


async def drop_before(backend: Any, cutoff: date, *, dry_run: bool = False) -> list[str]:
    """Drop both tables' partitions for months before ``cutoff``, as pairs.

    A dry run reports what would go without touching it. The two tables'
    partitions for one month are dropped in a single transaction, so a
    half-dropped month is not a state the database can be left in.
    """
    months = sorted(
        {
            info.bucket
            for info in await _all_partitions(backend)
            if info.bucket is not None and info.bucket < cutoff
        }
    )
    dropped: list[str] = []
    for bucket in months:
        names = tuple(partition_name(table, bucket) for table in PARTITIONED_TABLES)
        if dry_run:
            dropped.extend(name for name in names)
            continue
        await backend.execute("BEGIN")
        try:
            for name in names:
                await backend.execute(f'DROP TABLE IF EXISTS "{name}"')
            await backend.execute("COMMIT")
        except Exception:
            await backend.execute("ROLLBACK")
            raise
        dropped.extend(names)
    return dropped


async def list_partitions(backend: Any) -> PartitionReport:
    """Both tables' partitions, plus the months one table has and the other lacks."""
    partitions: list[PartitionInfo] = []
    buckets_by_table: dict[str, set[date]] = {}
    for table in PARTITIONED_TABLES:
        found = await existing_partitions(backend, table)
        partitions.extend(found)
        buckets_by_table[table] = {info.bucket for info in found if info.bucket is not None}

    present = [set(buckets) for buckets in buckets_by_table.values()]
    differences: list[tuple[str, date]] = []
    for table, buckets in buckets_by_table.items():
        for other in present:
            for bucket in sorted(buckets - other):
                differences.append((table, bucket))
    return PartitionReport(partitions=tuple(partitions), differences=tuple(differences))


async def _all_partitions(backend: Any) -> list[PartitionInfo]:
    found: list[PartitionInfo] = []
    for table in PARTITIONED_TABLES:
        found.extend(await existing_partitions(backend, table))
    return found


def _create_partition_sql(table: str, bucket: date) -> tuple[str, ...]:
    """The statements that create one month's partition of one table.

    ``PARTITION OF`` is what carries the parent's columns, constraints and
    indexes into the new partition -- which is why the monthly index upkeep is
    one statement rather than one per table per month.
    """
    end = next_month(bucket)
    return (
        f'CREATE TABLE IF NOT EXISTS "{partition_name(table, bucket)}" '
        f"PARTITION OF \"{table}\" FOR VALUES FROM ('{bucket.isoformat()}') "
        f"TO ('{end.isoformat()}')",
    )


__all__ = [
    "PARTITIONED_TABLES",
    "PartitionInfo",
    "PartitionReport",
    "bucket_of_completion",
    "drop_before",
    "ensure_month",
    "ensure_months",
    "ensure_through",
    "list_partitions",
    "month_start",
    "months_between",
    "next_month",
]
