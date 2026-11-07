# src/rhosocial/github_actions_tracker/schema/__init__.py
"""Database schema management.

One definition (:mod:`schema.model`) and one rendering
(:mod:`schema.ddl_postgres`), which is also what the service executes -- so a
reviewer reads the deployed shape, not a parallel text that can only ever be
nearly the same schema. The plain-SQL renderer that used to sit beside it was
deleted with partitioning: it could not express ``PARTITION BY``, which made it
a second, wrong answer to "what is this schema".

:func:`create_all` and :func:`drop_all` are the entry points; ``create_all``
renders through the expression system, and the drop path emits plain
``DROP TABLE`` text because a drop needs no builder.

This package does not import ``credentials``: the provider tables are part of
:data:`SCHEMA` as data, and the provider reads its own table from here. The
dependency runs one way -- provider -> schema -- which is what keeps the core
unaware of any provider's columns.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from . import ddl_postgres, partitions
from .model import (
    CORE_SCHEMA,
    PAT_SCHEMA,
    SCHEMA,
    Column,
    ColumnType,
    ForeignKey,
    Index,
    OnDelete,
    Schema,
    Table,
    UniqueConstraint,
)

logger = logging.getLogger(__name__)

#: Every table, dependants first: the order a teardown must delete in.
TABLES: tuple[str, ...] = SCHEMA.drop_order()

#: Retired table names, dropped on reset so an upgraded database keeps no
#: orphans. This project has no migrations.
LEGACY_TABLES: tuple[str, ...] = SCHEMA.legacy_tables

#: Provider-owned table names, appended after the core ones.
PROVIDER_TABLES: tuple[str, ...] = tuple(table.name for table in SCHEMA.provider_tables)

#: Extensions this service enables rather than assumes. A PostgreSQL cluster
#: ships these as *available* (``pg_available_extensions``) and *installed*
#: (``pg_extension``) are two different lists -- "the server has it" and "this
#: database can use it". The difference is exactly where a deployment fails with
#: "operator error: type trigram does not exist" instead of a degraded feature,
#: so the enabling is ours to do.
#:
#: ``IF NOT EXISTS`` makes this idempotent: ``create_all`` runs on every boot
#: and on every `db init`, and a server that already enabled the extension
#: answers in microseconds.
REQUIRED_EXTENSIONS: tuple[str, ...] = ("pg_trgm",)


async def ensure_extensions(backend) -> tuple[str, ...]:
    """Enable the extensions this service needs; return the ones now in use.

    Never raises: an engine that refuses the extension (a managed plan without
    it, a privilege we lack) still gets its tables. Losing the accelerator is a
    degraded search, not a failed boot -- and the caller can report it through
    the introspector rather than discovering it in an operator error.
    """
    enabled: list[str] = []
    for name in REQUIRED_EXTENSIONS:
        try:
            await backend.execute(f"CREATE EXTENSION IF NOT EXISTS {name}")
        except Exception as exc:  # noqa: BLE001 - boot must not hang on a feature
            logger.warning("extension %s could not be enabled: %s", name, exc)
            continue
        enabled.append(name)
    return tuple(enabled)


async def create_all(backend) -> None:
    """Create every extension, table and index, through the expression system.

    A partitioned table with no partition is a table that accepts no insert --
    PostgreSQL allows that state, and it fails at the first write with ``no
    partition of relation ... found for row``, which is an error no read path
    reports. So "the tables exist" is not the state this function leaves: it
    also creates the current month and the next one, because a run that finishes
    just after midnight rolls into a month this boot did not prepare.

    ``db init`` therefore produces a schema that is writable the moment it
    returns, and the write path's own ``ensure_month`` covers the months after
    that -- each side does its own job rather than assuming the other's.
    """
    await ensure_extensions(backend)
    await ddl_postgres.create_all(backend, SCHEMA)
    await ensure_accelerator_indexes(backend)
    await partitions.ensure_through(backend, months_ahead=1)


#: The accelerators this schema wants that the expression system cannot yet
#: render. A GIN index needs an *operator class* per column
#: (``log_content gin_trgm_ops``) to answer substring matches, and the DDL
#: renderer emits plain column names only -- so the statement is written here
#: rather than derived, with the table and column taken from the schema model so
#: the two cannot drift.
#:
#: See ``.claude/plan/2026-11-02/partitioned-model-support.md`` for the general
#: shape of the gap; this is the one place in this schema where it bites.
_TRGM_INDEXES: tuple[tuple[str, str, str], ...] = (
    # (table, column, index name)
    ("ghat_job_logs", "log_content", "idx_job_logs_trgm"),
)


async def ensure_accelerator_indexes(backend) -> tuple[str, ...]:
    """Create the operator-class indexes this schema wants; return their names.

    Never raises: an engine that refuses (a managed plan without ``pg_trgm``, a
    privilege we lack) loses the accelerator and nothing else, which is the same
    contract :func:`ensure_extensions` has. The index is declared on the
    *partitioned parent*, so PostgreSQL builds it on every existing partition and
    keeps it in step as months attach and detach -- there is no monthly index
    upkeep to forget.
    """
    created: list[str] = []
    for table, column, name in _TRGM_INDEXES:
        statement = (
            f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" '
            f"USING gin ({column} gin_trgm_ops)"
        )
        try:
            await backend.execute(statement)
        except Exception as exc:  # noqa: BLE001 - boot must not hang on a feature
            logger.warning("index %s could not be created: %s", name, exc)
            continue
        created.append(name)
    return tuple(created)


async def drop_all(backend) -> None:
    """Drop every table of this schema, legacy names included."""
    for statement in ddl_postgres.drop_statements(SCHEMA):
        await backend.execute(statement)


async def reset_all(backend) -> None:
    """Drop and recreate the whole schema.

    This project keeps no migrations: a schema change is applied by rebuilding
    the database, which is what ``rhosocial-github-actions-tracker-db reset`` does.
    """
    await drop_all(backend)
    await create_all(backend)


async def missing_tables(backend) -> tuple[str, ...]:
    """Return the tables of this schema that the connected database lacks.

    This is the first-boot test used by the container's init.d runner and by
    ``rhosocial-github-actions-tracker-db is-initialized``: a database counts as
    initialized once every table exists. Partial schemas are reported in full,
    so a re-run of ``create_all`` is what repairs them.
    """
    result = await backend.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )
    present = {
        (next(iter(row.values())) if isinstance(row, Mapping) else row[0])
        for row in (result.data or [])
    }
    return tuple(name for name in TABLES if name not in present)


def render_sql() -> str:
    """Render the schema as one script, for a human reviewing a change.

    The statements come from the same renderer the service executes: a second,
    plain-text rendering of the same tables could only ever be *nearly* the same
    schema, and the one construct that proves it -- partitioning -- it cannot
    express at all.
    """
    return ddl_postgres.render_schema_script(SCHEMA)


__all__ = [
    "CORE_SCHEMA",
    "LEGACY_TABLES",
    "PAT_SCHEMA",
    "PROVIDER_TABLES",
    "SCHEMA",
    "TABLES",
    "Column",
    "ColumnType",
    "ForeignKey",
    "Index",
    "OnDelete",
    "Schema",
    "Table",
    "UniqueConstraint",
    "create_all",
    "ddl",
    "ddl_postgres",
    "drop_all",
    "missing_tables",
    "render_sql",
    "reset_all",
]
