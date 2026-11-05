# src/rhosocial/github_actions_tracker/introspection.py
"""What the deployment's database holds: size by table, growth, and extensions.

This is the read-only side of the log tables. The tables are the largest tables
in this service by two orders of magnitude, and the question "how full is the
database, and what can I drop?" has no answer until something asks the engine.

The answers come from the engine's own introspector, so nothing here is SQL text
and no table name is assumed: an engine that does not answer reports that, and
the caller is told which fact is missing rather than being handed a zero.

What it does **not** do: prune. Deletion is an operator decision with a
retention policy, not something a query can decide; see ``db partitions --drop-before``
and ``db cleanup``.

One report answers three surfaces (CLI ``db usage``, ``ci_status.storage``, and
the operator's own scripts), so the shape is defined here once rather than in
each adapter.

Every read happens inside one ``database.connection()`` scope, and nothing here
touches the model classes: the pool's connection context is what makes a model
query land on the connection this report opened, and it is per-coroutine. Writing
a class-level backend instead would let two concurrent reports overwrite each
other's connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: How far back "growth" looks. One day is the unit an operator thinks in
#: ("daily CI volume"), and it is the same window the ingest figures use.
GROWTH_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class TableUsage:
    """One table's footprint, as the engine reported it.

    ``None`` is a legal answer: an engine whose storage is shaped otherwise
    (managed byte storage, no per-table accounting) leaves the slots empty, and
    a zero would be a lie about a missing fact.
    """

    name: str
    rows: int | None = None
    bytes: int | None = None


@dataclass
class StorageReport:
    """The deployment's storage facts, and whether they could be asked."""

    #: False when the engine cannot answer; then ``reason`` says what is missing.
    available: bool = True
    reason: str | None = None
    #: Whole-database size in bytes.
    database_bytes: int | None = None
    #: Per-table facts, biggest first.
    tables: list[TableUsage] = field(default_factory=list)
    #: Runs stored in the last :data:`GROWTH_WINDOW`.
    runs_added_last_day: int | None = None
    #: Whether the trigram extension (used by log search) is installed.
    #: ``None`` when the engine does not answer.
    trgm_installed: bool | None = None

    @property
    def table_bytes(self) -> int | None:
        """Sum of the per-table figures, when at least one answered."""
        values = [table.bytes for table in self.tables if table.bytes is not None]
        return sum(values) if values else None

    def table(self, name: str) -> TableUsage | None:
        """One table's facts, or ``None`` when it is absent or unmeasured."""
        for table in self.tables:
            if table.name == name:
                return table
        return None

    def as_dict(self) -> dict[str, Any]:
        """The wire form shared by ``ci_status`` and ``db usage --json``."""
        return {
            "available": self.available,
            "reason": self.reason,
            "database_bytes": self.database_bytes,
            "runs_added_last_day": self.runs_added_last_day,
            "trgm_installed": self.trgm_installed,
            "tables": [
                {"name": table.name, "rows": table.rows, "bytes": table.bytes}
                for table in self.tables
            ],
        }


class CapacityAdvisor:
    """Reads the engine's storage facts through the process's own connection."""

    def __init__(self, database: Any, runs_data: Any) -> None:
        self._database = database
        self._runs_data = runs_data

    async def _growth(self) -> int | None:
        """Runs stored in the last day, or ``None`` when that cannot be asked.

        Its own failure path, deliberately: an ORM problem unrelated to the
        engine's storage would otherwise take the whole report down, and "how
        full is the database" is the question the report exists to answer.
        """
        try:
            return await self._runs_data.added_since(datetime.now(timezone.utc) - GROWTH_WINDOW)
        except Exception as exc:  # noqa: BLE001 - one fact missing, not the report
            logger.debug("growth unavailable: %s", exc)
            return None

    async def advise(self) -> StorageReport:
        """One report: per-table size, one-day growth, extension presence.

        Never raises: an engine that cannot answer is a fact about the
        deployment, not a failure of the caller's request, and a capacity
        question that 500s is worse than one that says "this engine does not
        answer".
        """
        if self._database is None:
            # No database at all (a CLI command, a bare container in a test):
            # that is a state, not an error to discover by exception.
            return StorageReport(available=False, reason="no database is configured")
        try:
            async with self._database.connection() as backend:
                inspector = getattr(backend, "introspector", None)
                if inspector is None:
                    return StorageReport(
                        available=False, reason="this engine exposes no introspector"
                    )
                # The models inside this scope resolve through the pool's
                # connection context, exactly as they do in a request scope:
                # ``Model.backend()`` reads the ContextVar the pool publishes
                # and only falls back to the class attribute when there is no
                # scope. Writing ``__backend__`` here would bind *class* state
                # for every coroutine in the process -- two concurrent capacity
                # reads would overwrite each other's connection.
                listing = await inspector.list_tables()
                storage = getattr(inspector, "status", None)
                info = None
                if storage is not None:
                    info = await _maybe_await(getattr(storage, "get_storage_info", None))
                extensions = await _maybe_await(getattr(inspector, "list_extensions", None))
                runs = await self._growth()
        except Exception as exc:  # noqa: BLE001 - a capacity read must not 500
            logger.debug("storage report unavailable: %s", exc)
            return StorageReport(available=False, reason=f"{type(exc).__name__}: {exc}")

        if listing is None:
            # An engine with no such question answers nothing at all; a database
            # with zero tables is a legal answer and must not be mistaken for it.
            return StorageReport(available=False, reason="the engine listed no tables")

        tables = [
            TableUsage(
                name=table.name,
                rows=getattr(table, "row_count", None),
                bytes=getattr(table, "size_bytes", None),
            )
            for table in listing
        ]
        ranked = sorted(
            [table for table in tables if table.bytes is not None],
            key=lambda table: -(table.bytes or 0),
        )
        rest = [table for table in tables if table.bytes is None]
        return StorageReport(
            available=True,
            database_bytes=getattr(info, "total_size_bytes", None) if info else None,
            tables=ranked + rest,
            runs_added_last_day=runs,
            trgm_installed=_extension_installed(extensions, "pg_trgm"),
        )


async def _maybe_await(attribute: Any) -> Any:
    """Call an optional accessor and await it if it is a coroutine."""
    if attribute is None:
        return None
    result = attribute()
    if hasattr(result, "__await__"):
        result = await result
    return result


def _extension_installed(extensions: Any, wanted: str) -> bool | None:
    """Whether ``wanted`` is in an extension listing, ``None`` when not asked."""
    if extensions is None:
        return None
    names = {getattr(item, "name", None) for item in extensions}
    return wanted in names


__all__ = [
    "GROWTH_WINDOW",
    "CapacityAdvisor",
    "StorageReport",
    "TableUsage",
]
