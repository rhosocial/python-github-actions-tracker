# src/rhosocial/github_actions_tracker/schema/partitions.py
"""Keeping the monthly partitions of the log tables in the state the writes need.

PostgreSQL never creates a future partition by itself: the parent table is
created empty, and the first insert into a month that has no partition fails with
``no partition of relation "ghat_job_logs" found for row`` (measured, see
``.claude/plan/2026-11-01/partition-and-purge-plan.md``). So some code has to
create them ahead of the writes, and this module is that code.

**A partition is addressed by a month, and the month comes from the execution's
trigger time** (``ghat_run_attempts.github_created_at``, carried on each log row
as ``attempt_created_at``). Not from when the log arrived: a capture can be
retried, backfilled weeks later, or never happen at all (GitHub drops the
per-step files about an hour after the run ends), and the arrival time would file
one run's logs under months that have nothing to do with the run.

Four rules from the measured behaviour of missing partitions, which are why
this module looks the way it does:

* **the two log tables are always handled together** -- the capture path writes
  both, so creating a partition for one and forgetting the other *manufactures*
  the failure rather than preventing it;
* **every operation is a pair in one transaction** -- PostgreSQL DDL is
  transactional, so ``CREATE``/``DROP`` for both tables either happens or does
  not, and a half-done month is not a state the database can be left in;
* **dropping is by whole partition** -- ``DROP TABLE`` of a partition is a
  metadata operation (13.7ms measured against 262ms for the equivalent row
  delete), which is the entire reason the tables are partitioned;
* **the bounds are month-aligned instants with a named zone** -- the partition
  column is a ``timestamptz``, and a date literal cast in the session's zone
  would put a row created just after midnight UTC on the wrong side of the
  boundary.

Nothing here decides *when* to drop a month: that is a retention policy, and an
operator's ``db partitions --drop-before 6m`` is the interface to it.

**What this module is, now that the backend owns the work.** The plan grammar,
the catalogue query, the DDL rendering and the transaction unit used to be
written here by hand: a formatted ``CREATE TABLE ... PARTITION OF`` string, a
``BEGIN``/``COMMIT`` pair, and a ``pg_inherits`` join per table. All three now
live in ``python-activerecord-postgres``'s partition package
(:class:`PostgresPartitionPlan` +
:class:`AsyncPostgresPartitionMaintenance`), which the release line carries. What
is left here is what that package deliberately does not own: which tables, which
column, that a month is a month, and the report an operator reads. See
``.claude/plan/2026-11-09/partition-maintenance-migration.md``.

Two shapes of plan are used, and the difference matters:

* an **unbounded** policy (a month interval from an early anchor, no horizon) is
  how the catalogue is *read*. The library answers with the live tree and an
  ``issues`` entry saying there is no finite horizon, which is honest -- this
  module is not claiming to know which months should exist, only describing
  which do;
* a **bounded** policy (explicit bounds for known months) is how the catalogue is
  *changed*. It is used for creation, where the months are known, and for a
  drop, where the months to consider are read first.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from rhosocial.activerecord.backend.impl.postgres.partition import (
    AsyncPostgresPartitionMaintenance,
    PostgresPartitionPlan,
    RangePolicy,
)

from .model import partition_name

logger = logging.getLogger(__name__)

#: The tables this service partitions, in the order their writes happen. Both are
#: log tables sharing one bucket column and one partition shape, and they are
#: created and dropped as a pair -- see the module docstring.
PARTITIONED_TABLES: tuple[str, ...] = ("ghat_job_logs", "ghat_step_logs")

#: The column a partition is addressed by, on both tables. It carries the
#: execution's trigger time, so the month a log belongs to is a fact about the
#: run rather than about when the log was captured.
PARTITION_COLUMN = "attempt_created_at"

#: The anchor a read-only plan starts from. It only has to be early enough that
#: every partition this service could have created is above it; the plan makes
#: no claim about which months exist, it only frames the catalogue read.
_UNBOUNDED_ANCHOR = date(2020, 1, 1)


def month_start(bucket: date) -> date:
    """The first day of the month a bucket names.

    A bucket is already a month start when it comes from :func:`month_start_of`;
    this is here because a caller may hand over any day, and "the partition for a
    date" must not silently become two different partitions for the same month.

    A *datetime* is accepted and read as UTC: the engine hands a parsed bound
    over as an aware datetime, and the month it names is the same month whichever
    zone the server spells it in.
    """
    if isinstance(bucket, datetime):
        return month_start_of(bucket)
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
    """Deprecated alias of :func:`month_start_of`, kept for the CLI's surface.

    The completion time is no longer what a log is filed under -- the execution's
    trigger time is -- so this only remains where an operator's month-shaped
    question ("drop the months before this date") is answered.
    """
    return None if completed_at is None else month_start_of(completed_at)


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


def month_start_of(moment: datetime) -> date:
    """The month an instant belongs to, in UTC.

    The one place that turns a timestamp into a partition month. A naive
    timestamp is read as UTC, the same assumption the rest of this service makes
    of timestamps it did not offset itself.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date().replace(day=1)


# -- the plans the library is handed ------------------------------------------


def _instant(bucket: date, *, last: bool = False) -> datetime:
    """A month-aligned **UTC** instant -- the form the partition column compares to.

    A ``date`` is deliberately not used. The renderer writes a date literal as
    written, and the server then casts it in the *session's* zone: the tracker
    pins every connection to UTC, but a statement that only means the right thing
    because of a connection setting is one an operator copying it into ``psql``
    would get wrong. An aware datetime is written with its offset and means the
    same thing in any session.
    """
    start = month_start(bucket)
    return datetime(
        start.year,
        start.month,
        start.day,
        tzinfo=timezone.utc,
    )


def _bounds(months: Iterable[date]) -> tuple[tuple[datetime, datetime], ...]:
    """One ``[start, end)`` pair per month, as UTC instants."""
    return tuple(
        (_instant(bucket), _instant(_shift_months(bucket, 1))) for bucket in sorted(set(months))
    )


def _bounded_plan(table: str, months: Iterable[date]) -> PostgresPartitionPlan:
    """A plan that *claims* these months -- the shape creation and a drop need."""
    pairs = _bounds(months)
    if not pairs:
        raise ValueError("a partition plan needs at least one month")
    return PostgresPartitionPlan(
        parent=table,
        method="RANGE",
        columns=PARTITION_COLUMN,
        policy=RangePolicy(bounds=pairs, label_pattern="{year}_{month:02d}"),
    )


def _unbounded_plan(table: str) -> PostgresPartitionPlan:
    """A plan that claims no month at all -- the shape a catalogue read needs."""
    return PostgresPartitionPlan(
        parent=table,
        method="RANGE",
        columns=PARTITION_COLUMN,
        policy=RangePolicy(
            columns=(PARTITION_COLUMN,),
            start=_UNBOUNDED_ANCHOR,
            interval="month",
        ),
    )


def _plans(months: Iterable[date]) -> list[PostgresPartitionPlan]:
    """One plan per log table for the same months -- the pair the writes need."""
    wanted = sorted(set(months))
    return [_bounded_plan(table, wanted) for table in PARTITIONED_TABLES]


# -- reading the catalogue ----------------------------------------------------


def _row_of(table: str, info) -> PartitionInfo:
    """The library's catalogue row, in this service's vocabulary.

    The bucket is the month the row's own bound starts in. The library parses
    ``relpartbound`` for us and hands the result over in ``extras``, so the month
    comes from there; an unreadable bound still keeps ``None`` rather than being
    guessed, which is what the old hand-written parser did too.
    """
    start = (info.extras or {}).get("range_start")
    estimate = info.row_count
    return PartitionInfo(
        table=table,
        partition=info.name,
        bucket=month_start(start) if isinstance(start, (datetime, date)) else None,
        rows=None if estimate is None or estimate < 0 else estimate,
        bytes=info.size_bytes,
    )


def _table_of_side(side: str) -> str | None:
    """The table a pair side means, in the order the pair is declared."""
    return {"left": PARTITIONED_TABLES[0], "right": PARTITIONED_TABLES[1]}.get(side)


def _bucket_from_bound(bound: Any) -> date | None:
    """The month a bound's text names, or ``None`` when nothing in it does.

    This is the tracker's own convention, not a second copy of the engine's
    parser: the engine hands the parsed bound over in ``extras`` and that is what
    a partition's month normally comes from. What is left here reads the two
    spellings this service itself produces -- a bound literal
    ``('2026-11-01 00:00:00+00')`` and a partition name suffix ``2026_11`` --
    which is what a paired key falls back to when the engine could not parse.

    The month is taken, never the instant: the server spells a bound in whatever
    zone it is configured for, and the partition is a month all the same.
    """
    text = str(bound or "")
    for token in text.replace("'", " ").replace("(", " ").replace("_", "-").split():
        try:
            return date.fromisoformat(token[:10]).replace(day=1)
        except ValueError:
            continue
    return None


def _month_of_key(key: str) -> date | None:
    """The month a normalized bucket key names."""
    return _bucket_from_bound(key)


async def list_partitions(backend: Any) -> PartitionReport:
    """Both tables' partitions, plus the months one table has and the other lacks.

    One call, not two per table: the library reads both catalogues and compares
    them, which is where the paired drift it reports comes from.
    """
    maintenance = AsyncPostgresPartitionMaintenance(backend)
    plans = [_unbounded_plan(table) for table in PARTITIONED_TABLES]
    batch = await maintenance.list_many(pairs=[("log_tables", plans[0], plans[1])])

    partitions: list[PartitionInfo] = []
    for table, single in ((table, batch.reports.get(table)) for table in PARTITIONED_TABLES):
        if single is None:
            continue
        partitions.extend(_row_of(table, info) for info in single.partitions if info.level == 1)
    return PartitionReport(
        partitions=tuple(partitions),
        differences=tuple(
            (table, month)
            for table, month in (
                (_table_of_side(difference.present_in), _month_of_key(difference.key))
                for difference in batch.differences
            )
            if table is not None and month is not None
        ),
    )


async def existing_partitions(backend: Any, table: str) -> list[PartitionInfo]:
    """The partitions a table currently has, with size and row estimate.

    Rows are the planner's estimate (``reltuples``): exact enough to decide a
    drop, cheap enough to ask for every partition at once. A never-analyzed
    relation reports ``-1``, which is the *absence* of an estimate rather than
    an empty table, so it becomes ``None`` -- the same normalisation the engine's
    own row-count parser applies.
    """
    report = await AsyncPostgresPartitionMaintenance(backend).list(_unbounded_plan(table))
    return [_row_of(table, info) for info in report.partitions if info.level == 1]


async def _months_present(backend: Any) -> dict[str, list[date]]:
    """The months each table currently has, read once for both."""
    report = await list_partitions(backend)
    present: dict[str, list[date]] = {table: [] for table in PARTITIONED_TABLES}
    for info in report.partitions:
        if info.bucket is not None:
            present[info.table].append(info.bucket)
    return present


# -- the operations -----------------------------------------------------------


def _created_names(months: Sequence[date]) -> tuple[str, ...]:
    """Every name asked for, in table order -- see :func:`ensure_months`."""
    return tuple(partition_name(table, bucket) for bucket in months for table in PARTITIONED_TABLES)


async def ensure_for(backend: Any, moment: datetime) -> tuple[str, ...]:
    """Create both log tables' partitions for the month ``moment`` falls in.

    What the write path calls: it holds a timestamp (the execution's trigger
    time) and should not have to know that a partition is addressed by a month.
    See :func:`month_start_of`.
    """
    return await ensure_months(backend, (month_start_of(moment),))


async def ensure_month(backend: Any, bucket: date) -> tuple[str, ...]:
    """Create both log tables' partitions for one month, if they are missing.

    Idempotent and cheap when they are not: the creation is two ``CREATE TABLE
    IF NOT EXISTS``, and ``PARTITION OF`` carries the parent's indexes along.

    This is the month-addressed form, for the callers that think in months (the
    schema init, the operator's ``--ensure``). The write path holds a timestamp
    and uses :func:`ensure_for` -- the same guarantee, without it having to do
    the month arithmetic itself.
    """
    return await ensure_months(backend, (bucket,))


async def ensure_months(backend: Any, buckets: Iterable[date]) -> tuple[str, ...]:
    """Create both log tables' partitions for every given month.

    The month a bucket names is its own month start, so the same bucket always
    addresses the same partition however often it is asked for.

    Every requested name is returned, including one that already existed: the
    caller is asking "make sure of these months", and an answer listing only the
    new ones would read as "the others are missing". Both tables are created in
    one transaction, so a month cannot exist on one table and not the other.
    """
    wanted = sorted({month_start(item) for item in buckets})
    if not wanted:
        return ()
    await AsyncPostgresPartitionMaintenance(backend).ensure_many(plans=_plans(wanted))
    return _created_names(wanted)


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

    A dry run reports what would go without touching it. Both tables' partitions
    go in one transaction, so a half-dropped month is not a state the database
    can be left in -- the library commits the whole batch together, which is
    strictly stronger than dropping month by month.

    Only true leaves are considered, and a partition whose whole range ends
    before ``cutoff`` goes: ``range_end <= cutoff``, the same boundary semantics
    the library documents.
    """
    moment = _as_utc_moment(cutoff)
    present = await _months_present(backend)
    plans = [
        _bounded_plan(table, present.get(table, []))
        for table in PARTITIONED_TABLES
        if present.get(table)
    ]
    if not plans:
        return []
    batch = await AsyncPostgresPartitionMaintenance(backend).drop_before_many(
        plans=plans,
        cutoff=moment,
        dry_run=dry_run,
    )
    return [
        op.name
        for single in batch.reports.values()
        for op in single.operations
        if op.action == "drop"
    ]


def _as_utc_moment(cutoff: Any) -> datetime:
    """A cutoff the library can compare against its own bounds.

    It compares a leaf's ``range_end`` -- which this module always writes as a
    UTC instant -- against the cutoff, so the cutoff has to be an aware datetime
    itself. A naive one is read as UTC, and a mismatched type is a caller error
    rather than something to silently coerce into the wrong answer.
    """
    if not isinstance(cutoff, (date, datetime)):
        raise TypeError("cutoff must be a date or datetime")
    if isinstance(cutoff, datetime):
        return cutoff if cutoff.tzinfo is not None else cutoff.replace(tzinfo=timezone.utc)
    return _instant(cutoff)


__all__ = [
    "PARTITIONED_TABLES",
    "PartitionInfo",
    "PartitionReport",
    "bucket_of_completion",
    "drop_before",
    "ensure_for",
    "ensure_month",
    "ensure_months",
    "ensure_spanning",
    "ensure_through",
    "existing_partitions",
    "list_partitions",
    "month_start",
    "month_start_of",
    "months_between",
    "next_month",
]
