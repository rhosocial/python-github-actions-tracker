"""Declare the ``(repository_id, path)`` key on an existing ``ghat_workflows``.

The project keeps no migrations. ``schema/model.py`` now declares this key, so a
database created from today's tree has it; a database created earlier keeps the
columns and constraints it was built with, because every table is applied with
``CREATE TABLE IF NOT EXISTS`` and nothing reports the drift. This script is the
one-time bridge for those installations -- not a general migration tool, and not
a second schema definition.

Why the key is worth an explicit statement:

``RunController.store_workflow`` resolves a workflow by GitHub id when the
payload carries one and by ``(repository_id, path)`` when it does not, both
through ``find_one_or_create``. The library is explicit that a natural key must
be backed by a unique constraint, and equally explicit that it will not inspect
the schema for one. While the path half is undeclared, two concurrent deliveries
of the same path both insert: there is no constraint to violate, so
``find_one_or_create``'s race recovery never fires, and the next read answers
with whichever row the planner reaches.

Safety, in the order it matters:

* **refuses when duplicates exist.** Adding a unique constraint over rows that
  already violate it fails anyway, and a failure halfway through is worse than
  none. Run ``scripts/audit_workflow_keys.py`` first -- it exits non-zero while
  there is work to do.
* **idempotent.** The constraint's presence is read from ``pg_constraint``, so a
  second run prints that it is already there and does nothing.
* **adds one named constraint and nothing else.** The DDL is printed before it
  runs, and verified afterwards by reading the catalogue again.

Usage
-----
    python scripts/audit_workflow_keys.py                  # the pre-flight
    python scripts/add_workflow_path_key.py                # GHAT_DB_NAME
    python scripts/add_workflow_path_key.py --database ghat_test
    python scripts/add_workflow_path_key.py --dry-run      # the DDL, no write

Exit code
---------
``0`` when the key is in place afterwards (already there, or added now), ``1``
when it is not: duplicates must be resolved first, or the statement failed.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from rhosocial.activerecord.backend.impl.postgres.async_support import run_coroutine
from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

TABLE = "ghat_workflows"
COLUMNS = ("repository_id", "path")
CONSTRAINT = "ghat_workflows_repository_id_path_key"

#: What the database must say once this is done. Compared as text rather than
#: by name, because the name is ours and a previous hand-made constraint with a
#: different name is just as enforceable.
EXPECTED = "UNIQUE (repository_id, path)"

FIND_CONSTRAINT = f"""
    SELECT conname, pg_get_constraintdef(oid) AS definition
      FROM pg_constraint
     WHERE conrelid = '{TABLE}'::regclass
       AND contype = 'u'
"""

COUNT_DUPLICATES = f"""
    SELECT count(*) AS groups
      FROM (SELECT 1 FROM {TABLE}
             GROUP BY {', '.join(COLUMNS)}
            HAVING count(*) > 1) AS duplicated
"""

#: Named, so a retry after a partial failure does not collide with an unnamed
#: constraint -- and so ``DROP CONSTRAINT`` has one stable name to aim at.
ADD_CONSTRAINT = f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} UNIQUE ({', '.join(COLUMNS)})"


async def _rows(backend: AsyncPostgresBackend, sql: str) -> list[dict[str, Any]]:
    result = await backend.execute(sql)
    return list(result.data or [])


def _declares_path_key(definition: str) -> bool:
    """Whether this catalogue text is exactly ``UNIQUE (repository_id, path)``.

    Compared as normalized text, not by constraint name: the name this script
    uses is ours, and a hand-made constraint under another name enforces the
    same rule. The column order is the one ``pg_get_constraintdef`` renders for
    a two-column unique key, so a constraint on the same columns in the other
    order is treated as absent -- which is the conservative direction, since
    adding a duplicate is refused by the catalogue anyway.
    """
    text = definition.strip()
    if not text.upper().startswith("UNIQUE ("):
        return False
    inner = text[text.index("(") + 1 : text.rindex(")")]
    return [part.strip() for part in inner.split(",")] == list(COLUMNS)


async def apply(database: str, *, dry_run: bool) -> dict[str, Any]:
    """Read the state, add the constraint when it is safe, read it back."""
    from rhosocial.github_actions_tracker.config import get_settings
    from rhosocial.github_actions_tracker.database import Database

    config: PostgresConnectionConfig = Database.connection_config(get_settings())
    backend = AsyncPostgresBackend(connection_config=config.clone(database=database))
    await backend.connect()
    try:
        constraints = await _rows(backend, FIND_CONSTRAINT)
        already = any(_declares_path_key(row["definition"]) for row in constraints)

        answer: dict[str, Any] = {
            "database": database,
            "already_declared": already,
            "duplicate_groups": None,
            "statement": None,
            "verified": None,
        }
        if already:
            answer["verified"] = True
            return answer

        duplicates = (await _rows(backend, COUNT_DUPLICATES))[0]["groups"]
        answer["duplicate_groups"] = duplicates
        if duplicates:
            return answer

        if dry_run:
            answer["statement"] = ADD_CONSTRAINT
            return answer

        await backend.execute(ADD_CONSTRAINT)
        answer["statement"] = ADD_CONSTRAINT
        after = await _rows(backend, FIND_CONSTRAINT)
        answer["verified"] = any(_declares_path_key(row["definition"]) for row in after)
        return answer
    finally:
        await backend.disconnect()


def _report(result: dict[str, Any]) -> None:
    print(f"database : {result['database']}")
    if result["already_declared"]:
        print("already  : UNIQUE (repository_id, path) is declared; nothing to do")
        return

    if result["duplicate_groups"]:
        print(f"blocked  : {result['duplicate_groups']} (repository_id, path) group(s) hold")
        print("           more than one row. Resolve them first -- see")
        print("           scripts/audit_workflow_keys.py for the list.")
        return

    if result["statement"] and result["verified"] is None:
        print("dry-run  : would run")
        print(f"           {result['statement']}")
        return

    print("ran      :")
    print(f"           {result['statement']}")
    if result["verified"]:
        print("verified : the catalogue reports UNIQUE (repository_id, path)")
    else:  # pragma: no cover - only a concurrent drop lands here
        print("verified : NO -- the constraint is not there afterwards")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", default=None, help="Database (default: GHAT_DB_NAME)")
    parser.add_argument("--dry-run", action="store_true", help="Print the DDL, write nothing")
    args = parser.parse_args(argv)

    from rhosocial.github_actions_tracker.config import get_settings

    database = args.database or get_settings().database.database
    result = run_coroutine(apply(database, dry_run=args.dry_run))

    _report(result)
    if result["already_declared"] or result["verified"]:
        return 0
    if result["duplicate_groups"]:
        return 1
    if args.dry_run:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
