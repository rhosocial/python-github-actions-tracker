# src/rhosocial/github_actions_tracker/adapters/cli/db.py
"""Database management CLI (rhosocial-github-actions-tracker-db)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Coroutine, Mapping
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

from rhosocial.activerecord.backend.impl.postgres.async_support import run_coroutine
from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

from ...controllers.run import RunData
from ...schema import TABLES, create_all, missing_tables, reset_all
from ...schema.partitions import shift_months


def get_connection_config() -> PostgresConnectionConfig:
    """The connection coordinates, from the same settings the service reads.

    A maintenance tool that parsed ``GHAT_DB_*`` itself could drift from
    the service it is preparing a database for; there is one vocabulary of
    variables, and :class:`~rhosocial.github_actions_tracker.config.Settings` holds
    it.
    """
    from ...config import get_settings
    from ...database import Database

    return Database.connection_config(get_settings())


def _first_value(row: Any) -> Any:
    """Read the first column of a result row.

    Backends return rows either as tuples or as mappings; both are accepted.
    """
    if isinstance(row, Mapping):
        return next(iter(row.values()))
    return row[0]


def _run(coro: Coroutine[Any, Any, None]) -> None:
    """Run a coroutine on a loop psycopg accepts on Windows."""
    run_coroutine(coro)


async def cmd_init(args: argparse.Namespace) -> None:
    """Initialize database tables.

    By default the database itself is created first when the server does not
    have it: this project keeps no migrations, so a first deployment -- the
    container's init.d schema script included -- must be able to converge against
    a database name that does not exist yet. ``--no-create-database`` assumes it
    does.
    """
    config = get_connection_config()
    if not args.no_create_database:
        from ...database import PostgresServer

        created = await PostgresServer(config, args.maintenance_db).ensure()
        if created:
            print(f"Database '{config.database}' did not exist; created it.")
        else:
            print(f"Database '{config.database}' already exists.")
    backend = AsyncPostgresBackend(connection_config=config)
    try:
        await backend.connect()
        await create_all(backend)
        print("Database initialized successfully.")
    finally:
        await backend.disconnect()


async def cmd_reset(args: argparse.Namespace) -> None:
    """Drop and recreate every table.

    This project keeps no migrations: the DDL is the single source of truth, so
    a schema change is applied by rebuilding the database rather than by
    altering it in place.
    """
    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    try:
        await backend.connect()
        if not args.yes:
            print("This drops every ghat_* table and all stored CI history.")
            print("Re-run with --yes to confirm.")
            return
        await reset_all(backend)
        print("Database reset: all tables dropped and recreated.")
    finally:
        await backend.disconnect()


async def cmd_is_initialized(args: argparse.Namespace) -> None:
    """Exit 0 when the schema is present, 1 when this is a first boot.

    The container's init.d runner calls this before running any operational
    script. It has to answer without creating anything, so database existence is
    checked through the maintenance connection (connecting to an absent database
    fails at the driver) and then the tables are counted in the target database.
    """
    config = get_connection_config()

    from ...database import PostgresServer

    if not await PostgresServer(config, args.maintenance_db).exists():
        print(f"Database '{config.database}' does not exist; not initialized.")
        raise SystemExit(1)

    backend = AsyncPostgresBackend(connection_config=config)
    try:
        await backend.connect()
        missing = await missing_tables(backend)
        if missing:
            print(
                f"Database '{config.database}' is missing {len(missing)} table(s); "
                "not initialized."
            )
            raise SystemExit(1)
        print(f"Database '{config.database}' is initialized.")
    finally:
        await backend.disconnect()


async def cmd_drop(args: argparse.Namespace) -> None:
    """Drop the configured database after disconnecting its sessions.

    ``reset`` rebuilds the schema in place; this removes the database itself,
    which is what a teardown needs. The maintenance connection is separate, as
    PostgreSQL refuses to drop the database it is connected to.
    """
    config = get_connection_config()
    if not args.yes:
        print(f"This drops the database '{config.database}' and all stored CI history.")
        print("Re-run with --yes to confirm.")
        return

    from ...database import PostgresServer

    dropped = await PostgresServer(config, args.maintenance_db).drop()
    if dropped:
        print(f"Database '{config.database}' dropped.")
    else:
        print(f"Database '{config.database}' did not exist; nothing to drop.")


async def cmd_cleanup(args: argparse.Namespace) -> None:
    """Cleanup old data."""
    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    try:
        await backend.connect()

        # The cutoff is computed locally and interpolated as a literal: this
        # backend's parameter preparation only accepts positional parameters
        # against the Postgres dialect (a named dict raises TypeError), and the
        # value comes from a validated day count, never from user text.
        if args.runs_before:
            days = int(args.runs_before.rstrip("d"))
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            result = await backend.execute(
                f"DELETE FROM ghat_workflow_runs WHERE created_at < '{cutoff.isoformat()}'"
            )
            print(f"Deleted {result.affected_rows} old runs.")

        if args.logs_before:
            days = int(args.logs_before.rstrip("d"))
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            # Deprecated, and said so before the work starts: with the log tables
            # partitioned, a row-by-row delete is the expensive shape of the same
            # intent, and it leaves the space for a vacuum to reclaim later.
            print(
                "Note: --logs-before deletes rows; the log tables are now "
                "partitioned by month, so `partitions --drop-before "
                "<N>m` drops the same months in one metadata operation and "
                "needs no vacuum. Prefer it; this flag remains for pre-partition "
                "data."
            )
            # Both log tables: step logs are the preferred copy and job logs the
            # fallback, and a purge that forgot one would leave rows nobody can
            # reach through the read path.
            for table in ("ghat_step_logs", "ghat_job_logs"):
                result = await backend.execute(
                    f"DELETE FROM {table} WHERE created_at < '{cutoff.isoformat()}'"
                )
                print(f"Deleted {result.affected_rows} old rows from {table}.")

        if getattr(args, "deliveries_before", None):
            days = int(args.deliveries_before.rstrip("d"))
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            # Only the ledger rows go: the deliveries themselves are already
            # applied to the run/job rows, and GitHub keeps its own copy for 30
            # days if one needs to be inspected again.
            result = await backend.execute(
                f"DELETE FROM ghat_webhook_delivery WHERE received_at < '{cutoff.isoformat()}'"
            )
            print(f"Deleted {result.affected_rows} old webhook deliveries.")
    finally:
        await backend.disconnect()


async def cmd_partitions(args: argparse.Namespace) -> None:
    """List, create and drop the log tables' monthly partitions.

    The log tables are partitioned by the month their job finished, so a month is
    one unit: the write path needs it to exist, and cleanup drops it whole. This
    command is the operator's view of those two facts, and it never touches the
    parent tables.

    Every maintenance action is **dry by default** -- listing is not, and
    ``--ensure`` is idempotent so it needs no guard -- because dropping a month is
    irreversible and ``--drop-before`` is the one action here that deletes data.
    """
    from ...schema import partitions

    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    created: tuple[str, ...] = ()
    listed: list[Any] = []
    dropped: list[str] = []
    try:
        await backend.connect()

        if getattr(args, "ensure", None) is not None:
            created = await partitions.ensure_through(backend, args.ensure)

        if getattr(args, "drop_before", None):
            cutoff = _months_before_parse(args.drop_before)
            dropped = await partitions.drop_before(backend, cutoff, dry_run=not args.drop_old)

        if getattr(args, "list", False) or not (
            getattr(args, "ensure", None) is not None or getattr(args, "drop_before", None)
        ):
            # A bare ``partitions`` command answers with the list, which is the
            # question it exists for; an action that names itself answers with
            # what it did, and an empty result is a line about nothing to do
            # rather than a page of unchanged partitions.
            report = await partitions.list_partitions(backend)
            listed = list(report.partitions)
            differences = list(report.differences)
        else:
            differences = []
    finally:
        await backend.disconnect()

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "created": list(created),
                    "dropped": dropped,
                    "differences": [
                        {"table": table, "bucket": bucket.isoformat()}
                        for table, bucket in differences
                    ],
                    "partitions": [
                        {
                            "table": info.table,
                            "partition": info.partition,
                            "bucket": info.bucket.isoformat() if info.bucket else None,
                            "rows": info.rows,
                            "bytes": info.bytes,
                        }
                        for info in listed
                    ],
                },
                indent=2,
            )
        )
        return

    if created:
        print(f"Ensured {len(created)} partition(s) exist.")
    if getattr(args, "drop_before", None) and not dropped:
        print(
            f"No partition is wholly before {_months_before_parse(args.drop_before).isoformat()}; "
            "nothing to drop."
        )
    if dropped:
        verb = "Would drop" if not args.drop_old else "Dropped"
        print(f"{verb} {len(dropped)} partition(s):")
        for name in dropped:
            print(f"  {name}")
        if not args.drop_old:
            print("Nothing was dropped; re-run with --drop-old to do it.")
    if listed:
        print("Partitions:")
        print("-" * 68)
        current_table = None
        for info in listed:
            if info.table != current_table:
                current_table = info.table
                print(f"  {info.table}")
            size = _bytes(info.bytes)
            rows = "?" if info.rows is None else str(info.rows)
            bucket = info.bucket.isoformat() if info.bucket else "?"
            print(f"    {info.partition:<26} bucket={bucket} {size:>10} {rows:>9} rows")
    elif getattr(args, "list", False):
        # An empty answer is the important one: a partitioned table with no
        # partitions refuses every write, so "none" is a state an operator must
        # see rather than an empty line.
        print(
            "No partitions exist; a write into a new month will fail until "
            "some are created (--ensure N)."
        )
    if differences:
        print("Asymmetries (a month one table has and the other lacks):")
        for table, bucket in differences:
            print(f"  {table} has {bucket.isoformat()}, the other table does not")


def _months_before_parse(value: str) -> date:
    """``6m`` means "six months before this one", as a month start.

    A plain number of days is also accepted, because ``db cleanup`` spells its
    windows that way and an operator should not have to translate. Both spellings
    are ranges over months, which is what a partition is.
    """
    text = (value or "").strip().lower()
    this_month = datetime.now(timezone.utc).date().replace(day=1)
    if text.endswith("m"):
        return shift_months(this_month, -int(text[:-1] or "1"))
    if text.endswith("d"):
        days = int(text[:-1])
        return (datetime.now(timezone.utc).date() - timedelta(days=days)).replace(day=1)
    return shift_months(this_month, -int(text or "1"))


async def cmd_backfill_buckets(args: argparse.Namespace) -> None:
    """Give log rows written before the bucket column existed their address.

    The bucket is derived, so the backfill is a plain recomputation: a job's
    ``log_bucket`` comes from its own ``completed_at``, a step's from its job's,
    and each log row's from whichever parent it hangs off. Nothing is guessed --
    a parent with no completion time keeps ``NULL``, because an unfinished job
    has no month and never will.

    Idempotent by construction: running it twice recomputes the same values, so
    it is safe to leave in a cron and safe to interrupt. ``--dry-run`` reports
    what it would touch without touching it.

    This is the migration step that makes the addressed read paths in
    :mod:`controllers.run` see old rows: until it runs, a job written before the
    column carries no bucket and its log is read without one.
    """
    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    try:
        await backend.connect()

        # Parents first: the steps and the log rows copy from them, so the
        # values have to exist before anything derives from them.
        #
        # Each entry carries the count query beside the update, because a dry run
        # cannot reuse the statement's text: ``UPDATE ... FROM`` moves the join
        # into a clause a ``SELECT COUNT(*)`` has no place for, and a probe that
        # dropped it would count the wrong rows -- or fail to parse.
        statements = [
            (
                "ghat_jobs",
                "UPDATE ghat_jobs SET log_bucket = date_trunc('month', completed_at AT TIME ZONE "
                "'UTC')::date WHERE completed_at IS NOT NULL AND log_bucket IS DISTINCT FROM "
                "date_trunc('month', completed_at AT TIME ZONE 'UTC')::date",
                "SELECT COUNT(*) AS n FROM ghat_jobs WHERE completed_at IS NOT NULL AND log_bucket "
                "IS DISTINCT FROM date_trunc('month', completed_at AT TIME ZONE 'UTC')::date",
            ),
            (
                "ghat_steps",
                "UPDATE ghat_steps s SET log_bucket = j.log_bucket FROM ghat_jobs j "
                "WHERE s.job_id = j.id AND j.log_bucket IS NOT NULL AND s.log_bucket IS DISTINCT "
                "FROM j.log_bucket",
                "SELECT COUNT(*) AS n FROM ghat_steps s JOIN ghat_jobs j ON j.id = s.job_id "
                "WHERE j.log_bucket IS NOT NULL AND s.log_bucket IS DISTINCT FROM j.log_bucket",
            ),
            (
                "ghat_job_logs",
                "UPDATE ghat_job_logs l SET bucket = j.log_bucket FROM ghat_jobs j "
                "WHERE l.job_id = j.id AND j.log_bucket IS NOT NULL AND l.bucket IS DISTINCT FROM "
                "j.log_bucket",
                "SELECT COUNT(*) AS n FROM ghat_job_logs l JOIN ghat_jobs j ON j.id = l.job_id "
                "WHERE j.log_bucket IS NOT NULL AND l.bucket IS DISTINCT FROM j.log_bucket",
            ),
            (
                "ghat_step_logs",
                "UPDATE ghat_step_logs l SET bucket = s.log_bucket FROM ghat_steps s "
                "WHERE l.step_id = s.id AND s.log_bucket IS NOT NULL AND l.bucket IS DISTINCT FROM "
                "s.log_bucket",
                "SELECT COUNT(*) AS n FROM ghat_step_logs l JOIN ghat_steps s ON s.id = l.step_id "
                "WHERE s.log_bucket IS NOT NULL AND l.bucket IS DISTINCT FROM s.log_bucket",
            ),
        ]

        if getattr(args, "dry_run", False):
            print("Dry run: the rows each statement would change.")
            for table, _update, count_sql in statements:
                rows = (await _run_statement(backend, count_sql, table)).data or []
                count = rows[0].get("n", 0) if rows else 0
                print(f"  {table}: {count} rows")
            return

        for table, update_sql, _count in statements:
            result = await _run_statement(backend, update_sql, table)
            print(f"  {table}: {result.affected_rows} rows")
        print("Buckets backfilled.")
    finally:
        await backend.disconnect()


async def _run_statement(backend: AsyncPostgresBackend, statement: str, table: str) -> Any:
    """Execute one maintenance statement, reporting which one failed.

    The path runs four statements in sequence, and a failure without the
    statement's target leaves an operator guessing which update stopped.
    """
    try:
        return await backend.execute(statement)
    except Exception as exc:  # noqa: BLE001 - name the table, then re-raise
        raise RuntimeError(f"{table}: {exc}") from exc


async def cmd_stats(args: argparse.Namespace) -> None:
    """Show database statistics."""
    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    try:
        await backend.connect()

        print("Database Statistics:")
        print("-" * 40)
        for table in TABLES:
            result = await backend.execute(f"SELECT COUNT(*) AS row_count FROM {table}")
            rows = result.data or []
            count = _first_value(rows[0]) if rows else 0
            print(f"  {table}: {count} rows")
    finally:
        await backend.disconnect()


class _BackendConnection:
    """One open backend, spoken as a connection source.

    The advisor takes "something that opens a connection", which the process's
    :class:`Database` satisfies. A CLI command holds exactly one backend, so this
    adapter is the whole difference -- no pool, no lifecycle of its own.
    """

    def __init__(self, backend: AsyncPostgresBackend) -> None:
        self._backend = backend

    @asynccontextmanager
    async def connection(self):
        yield self._backend


def _bytes(value: int | None) -> str:
    """A byte count in the unit a person reads.

    ``None`` is printed as ``?`` rather than ``0``: the engine did not answer,
    and an operator reading ``0`` would conclude the table is empty.
    """
    if value is None:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


async def cmd_usage(args: argparse.Namespace) -> None:
    """What the database holds, and what the reader could not learn.

    ``stats`` answers "how many rows"; this answers "how full is the disk",
    which is the question that decides whether a deployment survives the next
    month. The two are different questions and the answers live in different
    places: a row count cannot be converted into bytes, because PostgreSQL
    measures storage the engine's way (TOAST, indexes, free space) and the
    introspector is the only party that knows it.

    Row counts are the planner's estimate, not a ``COUNT(*)``: on a table nobody
    has analyzed since it was created the estimate is the absence of one, and the
    row column then says nothing at all rather than printing a zero that reads as
    "this table is empty".
    """
    from ...introspection import CapacityAdvisor

    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    await backend.connect()
    try:
        advisor = CapacityAdvisor(_BackendConnection(backend), RunData())
        report = await advisor.advise()
    finally:
        await backend.disconnect()

    if getattr(args, "json", False):
        print(json.dumps(report.as_dict(), indent=2))
        return

    if not report.available:
        print(f"Storage is not available: {report.reason}")
        return

    print("Database storage:")
    print("-" * 56)
    if report.database_bytes is not None:
        print(f"  total: {_bytes(report.database_bytes)}")
    if report.runs_added_last_day is not None:
        print(f"  runs stored in the last day: {report.runs_added_last_day}")
    print(f"  pg_trgm: {'installed' if report.trgm_installed else 'not installed'}")
    print()
    for table in report.tables:
        rows = "" if table.rows is None else f"{table.rows} rows"
        print(f"  {table.name:<24} {_bytes(table.bytes):>10}  {rows}")


async def cmd_deliveries(args: argparse.Namespace) -> None:
    """Show webhook deliveries, newest first.

    The ledger is the only place a dropped delivery is visible: when storing
    what a delivery described fails, no run or job row is left behind to
    notice. Each row carries the GitHub ids, so a failing delivery can be
    looked up on GitHub (``delivery_id``) and traced to the run/job it
    described without storing the body.
    """
    conditions = []
    if getattr(args, "unhandled", False):
        conditions.append("handled = FALSE")
    if getattr(args, "stream", None):
        conditions.append(f"event_type = '{args.stream}'")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit = max(1, int(getattr(args, "limit", 20) or 20))

    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    try:
        await backend.connect()
        result = await backend.execute(
            "SELECT row_to_json(t) AS row FROM ghat_webhook_delivery t "
            f"{where} ORDER BY received_at DESC LIMIT {limit}"
        )
        rows = result.data or []
        data = [
            (
                _first_value(row)
                if isinstance(_first_value(row), dict)
                else json.loads(_first_value(row))
            )
            for row in rows
        ]
        if not data:
            print("No matching deliveries.")
            return
        for row in data:
            print(
                f"{row.get('received_at')}  {row.get('event_type')}  "
                f"{row.get('repository_full_name') or '-'}  "
                f"run={row.get('github_run_id') or '-'}  job={row.get('github_job_id') or '-'}  "
                f"handled={row.get('handled')}  reason={row.get('reason') or '-'}"
            )
            print(f"  delivery={row.get('delivery_id') or '-'}")
    finally:
        await backend.disconnect()


async def cmd_export(args: argparse.Namespace) -> None:
    """Export table data as JSON.

    ``row_to_json`` is used so column names come back from PostgreSQL itself
    instead of being reconstructed from a cursor description.
    """
    backend = AsyncPostgresBackend(connection_config=get_connection_config())
    try:
        await backend.connect()
        result = await backend.execute(f"SELECT row_to_json(t) AS row FROM {args.table} t")
        rows = result.data or []
        # psycopg returns the JSON column already parsed as a dict; a plain
        # driver would hand back the text, so both shapes are accepted.
        data = [
            (
                _first_value(row)
                if isinstance(_first_value(row), dict)
                else json.loads(_first_value(row))
            )
            for row in rows
        ]

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

        print(f"Exported {len(data)} rows to {args.output}")
    finally:
        await backend.disconnect()


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="rhosocial-github-actions-tracker-db",
        description="CI Tracker database management CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # init
    init_parser = subparsers.add_parser(
        "init", help="Initialize database tables (creates the database itself)"
    )
    init_parser.add_argument(
        "--no-create-database",
        action="store_true",
        help="Assume the database exists; never create it",
    )
    init_parser.add_argument(
        "--maintenance-db",
        default="postgres",
        help="Existing database to connect through when creating (default: postgres)",
    )

    # is-initialized
    is_initialized_parser = subparsers.add_parser(
        "is-initialized",
        help="Exit 0 when every ghat_* table exists, 1 when the schema is absent",
    )
    is_initialized_parser.add_argument(
        "--maintenance-db",
        default="postgres",
        help="Existing database to connect through when checking (default: postgres)",
    )

    # reset
    reset_parser = subparsers.add_parser(
        "reset", help="Drop and recreate every table (no migrations)"
    )
    reset_parser.add_argument(
        "--yes", action="store_true", help="Confirm dropping all ghat_* tables"
    )

    # drop
    drop_parser = subparsers.add_parser(
        "drop", help="Drop the configured database itself (teardown)"
    )
    drop_parser.add_argument("--yes", action="store_true", help="Confirm dropping the database")
    drop_parser.add_argument(
        "--maintenance-db",
        default="postgres",
        help="Existing database to connect through when dropping (default: postgres)",
    )

    # cleanup
    cleanup_parser = subparsers.add_parser("cleanup", help="Cleanup old data")
    cleanup_parser.add_argument(
        "--runs-before", type=str, help="Delete runs before N days (e.g., 90d)"
    )
    cleanup_parser.add_argument(
        "--logs-before", type=str, help="Delete logs before N days (e.g., 365d)"
    )
    cleanup_parser.add_argument(
        "--deliveries-before",
        type=str,
        help="Delete webhook delivery rows before N days (e.g., 30d); the applied "
        "state in runs/jobs is untouched",
    )

    # partitions: the monthly units the log tables are made of
    partitions_parser = subparsers.add_parser(
        "partitions", help="Inspect and maintain the log tables' monthly partitions"
    )
    partitions_parser.add_argument(
        "--ensure", type=int, metavar="N", help="Create this month and the next N months"
    )
    partitions_parser.add_argument(
        "--drop-before",
        type=str,
        metavar="NyM",
        help="Drop every month wholly before this one (e.g. 6m); dry by default",
    )
    partitions_parser.add_argument(
        "--drop-old",
        action="store_true",
        help="After --drop-before's dry run, actually drop",
    )
    partitions_parser.add_argument(
        "--list", action="store_true", help="List each table's partitions with size"
    )
    partitions_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")

    # backfill-buckets: give rows written before the column their address
    backfill_parser = subparsers.add_parser(
        "backfill-buckets",
        help="Compute log buckets on rows stored before the column existed",
    )
    backfill_parser.add_argument(
        "--dry-run", action="store_true", help="Report the rows each statement would change"
    )

    # stats
    subparsers.add_parser("stats", help="Show database statistics")

    # usage: what the database weighs, not how many rows it has
    usage_parser = subparsers.add_parser("usage", help="Show database storage usage")
    usage_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")

    # export
    export_parser = subparsers.add_parser("export", help="Export table data as JSON")
    export_parser.add_argument("--table", required=True, help="Table name")
    export_parser.add_argument("--output", required=True, help="Output file")

    # deliveries: the webhook ledger, where a dropped delivery is only visible.
    deliveries_parser = subparsers.add_parser("deliveries", help="Show webhook deliveries")
    deliveries_parser.add_argument(
        "--unhandled", action="store_true", help="Only deliveries this service did not store"
    )
    deliveries_parser.add_argument(
        "--stream", choices=["workflow_run", "workflow_job"], help="Only this stream"
    )
    deliveries_parser.add_argument("--limit", type=int, default=20, help="Rows to show")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "init":
        _run(cmd_init(args))
    elif args.command == "is-initialized":
        _run(cmd_is_initialized(args))
    elif args.command == "reset":
        _run(cmd_reset(args))
    elif args.command == "drop":
        _run(cmd_drop(args))
    elif args.command == "cleanup":
        _run(cmd_cleanup(args))
    elif args.command == "partitions":
        _run(cmd_partitions(args))
    elif args.command == "backfill-buckets":
        _run(cmd_backfill_buckets(args))
    elif args.command == "stats":
        _run(cmd_stats(args))
    elif args.command == "usage":
        _run(cmd_usage(args))
    elif args.command == "export":
        _run(cmd_export(args))
    elif args.command == "deliveries":
        _run(cmd_deliveries(args))


if __name__ == "__main__":
    main()
