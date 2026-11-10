"""Is the ``(repository_id, path)`` lookup of ``ghat_workflows`` actually a key?

``RunController.store_workflow`` resolves a stored workflow two ways: by
``(repository_id, workflow_id)`` when GitHub's id is known, and by
``(repository_id, path)`` when only the path is. Both go through
``find_one_or_create``, whose contract is "the lookup columns carry a unique
constraint; without one several rows can match, and which row is returned is
undefined" -- the library states that obligation in
``BaseActiveRecord.find_one_or_create``'s docstring and deliberately does not
inspect the schema for it.

The schema declares only ``(repository_id, workflow_id)`` unique
(``schema/model.py``). So the by-path half rests on nothing, and this project
makes duplicates reachable on purpose: ``workflow_id = 0`` marks a row whose
GitHub id is not learned yet, so a path can hold one unresolved row and one
resolved row at the same time.

This script answers the questions that decide the fix, and only those:

1. how many workflow rows exist, and how many are unresolved (``workflow_id``
   is ``0``);
2. which ``(repository_id, path)`` groups hold more than one row, and whether
   those rows disagree about the workflow id -- the shape that makes a lookup
   answer depend on the planner;
3. whether a unique constraint or index on ``(repository_id, path)`` exists
   today, and exactly what ``(repository_id, workflow_id)`` is declared as.

Read-only: every statement is a ``SELECT`` or a catalogue read. It connects
through the project's own backend layer (``AsyncPostgresBackend`` over
``Database.connection_config``), so the connection coordinates are the same
ones the service uses and there is no second vocabulary to drift from.

Usage
-----
    python scripts/audit_workflow_keys.py                       # GHAT_DB_NAME
    python scripts/audit_workflow_keys.py --database ghat_test  # the test schema
    python scripts/audit_workflow_keys.py --json                # machine-readable

Exit code
---------
``0`` when the by-path key is already safe: no duplicate groups and a unique
constraint in place. ``1`` when it is not -- either duplicates exist today, or
the constraint is missing, so a migration has something to do. The point of a
non-zero answer is to make this usable as a pre-migration gate::

    python scripts/audit_workflow_keys.py || echo "fix before adding the constraint"
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from rhosocial.activerecord.backend.impl.postgres.async_support import run_coroutine
from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

#: The table and the two keys under audit, in one place because the SQL below
#: names them repeatedly and a typo there would audit the wrong table.
TABLE = "ghat_workflows"

QUERIES: dict[str, str] = {
    "total": f"SELECT count(*) AS rows FROM {TABLE}",
    "unresolved": f"SELECT count(*) AS rows FROM {TABLE} WHERE workflow_id = 0",
    # The groups, with the disagreement made explicit: a group whose rows all
    # carry the same workflow_id is a duplicate that a constraint would refuse;
    # a group with ``workflow_id = 0`` beside a real id is the shape the code
    # produces on purpose, and the reason a plain de-duplicate would lose data.
    "duplicates": f"""
        SELECT repository_id,
               path,
               count(*) AS rows,
               count(*) FILTER (WHERE workflow_id = 0) AS unresolved_rows,
               count(DISTINCT workflow_id) AS distinct_workflow_ids,
               array_agg(workflow_id ORDER BY workflow_id) AS workflow_ids,
               min(id) AS lowest_id,
               max(id) AS highest_id
          FROM {TABLE}
         GROUP BY repository_id, path
        HAVING count(*) > 1
         ORDER BY rows DESC, repository_id, path
    """,
    # What the database itself will enforce, as opposed to what the code assumes.
    "constraints": f"""
        SELECT conname,
               pg_get_constraintdef(oid) AS definition
          FROM pg_constraint
         WHERE conrelid = '{TABLE}'::regclass
           AND contype IN ('p', 'u')
         ORDER BY conname
    """,
    "indexes": f"""
        SELECT indexname,
               indexdef
          FROM pg_indexes
         WHERE tablename = '{TABLE}'
         ORDER BY indexname
    """,
}


async def _one(backend: AsyncPostgresBackend, sql: str) -> list[dict[str, Any]]:
    """Run one read statement and return its rows as mappings."""
    result = await backend.execute(sql)
    return list(result.data or [])


async def audit(database: str) -> dict[str, Any]:
    """Read the four facts, and nothing else, from one database."""
    from rhosocial.github_actions_tracker.config import get_settings
    from rhosocial.github_actions_tracker.database import Database

    config: PostgresConnectionConfig = Database.connection_config(get_settings())
    backend = AsyncPostgresBackend(connection_config=config.clone(database=database))
    await backend.connect()
    try:
        answers: dict[str, Any] = {name: await _one(backend, sql) for name, sql in QUERIES.items()}
    finally:
        await backend.disconnect()

    totals = answers["total"][0]["rows"] if answers["total"] else 0
    unresolved = answers["unresolved"][0]["rows"] if answers["unresolved"] else 0
    duplicates = answers["duplicates"]
    constraints = answers["constraints"]

    path_key_declared = any(
        "repository_id" in row["definition"] and "path" in row["definition"] for row in constraints
    )

    return {
        "database": database,
        "table": TABLE,
        "rows": totals,
        "unresolved_rows": unresolved,
        "duplicate_groups": duplicates,
        "unique_constraints": constraints,
        "indexes": answers["indexes"],
        #: The two conditions that make the by-path lookup safe: the key is
        #: declared, and no existing group contradicts it. A migration adding
        #: the constraint would fail while either is untrue.
        "key_is_safe": not duplicates and path_key_declared,
        "path_key_declared": path_key_declared,
    }


def _report(result: dict[str, Any]) -> None:
    """The human answer: what the key is, and what stands in the way."""
    print(f"database : {result['database']}")
    print(f"table    : {result['table']}")
    print(
        f"rows     : {result['rows']}  (unresolved, workflow_id = 0: {result['unresolved_rows']})"
    )

    print("\nunique constraints the database enforces:")
    for row in result["unique_constraints"]:
        print(f"  {row['conname']}: {row['definition']}")
    if not result["unique_constraints"]:
        print("  (none)")

    print("\n(repository_id, path) groups holding more than one row:")
    if not result["duplicate_groups"]:
        print("  none")
    for row in result["duplicate_groups"]:
        print(
            f"  repository {row['repository_id']}  path {row['path']}\n"
            f"    rows={row['rows']} unresolved={row['unresolved_rows']} "
            f"distinct_workflow_ids={row['distinct_workflow_ids']} "
            f"ids={row['lowest_id']}..{row['highest_id']}\n"
            f"    workflow_ids={row['workflow_ids']}"
        )

    verdict = "SAFE" if result["key_is_safe"] else "NOT SAFE"
    print(f"\nverdict  : {verdict}")
    if not result["key_is_safe"]:
        if not result["path_key_declared"]:
            print("  - no unique constraint covers (repository_id, path), so a by-path")
            print("    find_one_or_create can match several rows and the answer is undefined")
        if result["duplicate_groups"]:
            print("  - existing duplicate groups must be resolved before such a constraint")
            print("    can be added; the groups above are the work list")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
    )
    parser.add_argument(
        "--database",
        default=None,
        help="Database to audit (default: GHAT_DB_NAME, the service's own)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args(argv)

    from rhosocial.github_actions_tracker.config import get_settings

    database = args.database or get_settings().database.database
    # ``run_coroutine`` and not ``asyncio.run``: psycopg refuses the
    # ProactorEventloop that asyncio installs by default on Windows, and the
    # project's own entry points import this runner for exactly that reason.
    result = run_coroutine(audit(database))

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _report(result)
    return 0 if result["key_is_safe"] else 1


if __name__ == "__main__":
    sys.exit(main())
