# tests/schema/test_ddl_renderers.py
"""The schema has one definition and one rendering; it must express the whole
definition.

This test turns "the Postgres expression system can express this schema" from a
claim into a check. It reads :mod:`schema.model`; if a construct the model
declares cannot be rendered, the renderer raises or produces something that is
not that construct, and that is what fails here.

The plain-SQL renderer that used to be compared against this one was deleted
with partitioning: it could not express ``PARTITION BY``, so the two renderings
could not always agree, and two answers to "what is this schema" is worse than
one. What is asserted now is that the executed rendering *contains* each
construct the model declares.
"""

from __future__ import annotations

import re

import pytest

from rhosocial.github_actions_tracker.schema import ddl_postgres
from rhosocial.github_actions_tracker.schema.model import (
    SCHEMA,
    ColumnType,
    OnDelete,
    Table,
)

#: Every table this schema defines, core and provider alike.
EXPECTED_TABLES = (
    "ghat_repositories",
    "ghat_repository_bindings",
    "ghat_workflows",
    "ghat_workflow_runs",
    "ghat_run_attempts",
    "ghat_jobs",
    "ghat_steps",
    "ghat_step_logs",
    "ghat_job_logs",
    "ghat_webhook_delivery",
    "ghat_pat_credentials",
)


def _rendered() -> list[str]:
    """Return the statements the service executes, in schema order."""
    return ddl_postgres.render_schema(SCHEMA)


def _created_tables(statements: list[str]) -> list[str]:
    """Extract table names from ``CREATE TABLE`` statements, in order."""
    names = []
    for statement in statements:
        match = re.match(r'CREATE TABLE IF NOT EXISTS "?([\w]+)"? \(', statement)
        if match:
            names.append(match.group(1))
    return names


def _created_indexes(statements: list[str]) -> list[tuple[str, str]]:
    """Extract ``(index name, table name)`` from ``CREATE INDEX`` statements."""
    found = []
    for statement in statements:
        match = re.match(
            r'CREATE (?:UNIQUE )?INDEX IF NOT EXISTS "?([\w]+)"? ON "?([\w]+)"?',
            statement,
        )
        if match:
            found.append((match.group(1), match.group(2)))
    return found


def test_the_renderer_creates_every_table() -> None:
    assert _created_tables(_rendered()) == list(EXPECTED_TABLES)


def test_every_index_is_created() -> None:
    """A declared index that does not render is an index nobody creates.

    A GIN index is the one exception, and the exception is the point: the
    expression system renders plain column names, so an operator-class index
    (``gin_trgm_ops``) is created by ``schema.ensure_accelerator_indexes`` as an
    explicit statement instead. Asserting that split here keeps the two from
    drifting -- an index that stops being rendered *and* is not in the accelerator
    list fails this test.
    """
    from rhosocial.github_actions_tracker.schema import _TRGM_INDEXES

    operator_class = {
        (index.name, table.name)
        for table in SCHEMA.all_tables()
        for index in table.indexes
        if index.method == "gin" or index.opclass
    }
    expected = {
        (index.name, table.name) for table in SCHEMA.all_tables() for index in table.indexes
    } - operator_class
    assert set(_created_indexes(_rendered())) == expected
    assert operator_class == {(name, table) for table, _column, name in _TRGM_INDEXES}


def test_expression_rendering_never_binds_parameters() -> None:
    rendered = "\n".join(_rendered())
    # A DDL statement must render inline; a bind parameter would mean something
    # was rendered as a value that should have been a literal.
    assert "%s" not in rendered
    assert "$1" not in rendered


def test_expression_rendering_keeps_function_defaults_inline() -> None:
    """``DEFAULT CURRENT_TIMESTAMP`` stays a call, not a quoted string."""
    rendered = "\n".join(_rendered())
    assert 'created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP' in rendered
    assert "'NOW()'" not in rendered


def test_expression_rendering_keeps_cascade_actions() -> None:
    rendered = "\n".join(_rendered())
    for table in SCHEMA.all_tables():
        for key in table.foreign_keys:
            if key.on_delete is OnDelete.CASCADE:
                assert f'REFERENCES "{key.references_table}"' in rendered
    assert "ON DELETE CASCADE" in rendered


@pytest.mark.parametrize(
    ("column_type", "expected"),
    [
        (ColumnType.SERIAL, "SERIAL"),
        (ColumnType.INTEGER, "INTEGER"),
        (ColumnType.BIGINT, "BIGINT"),
        (ColumnType.TEXT, "TEXT"),
        (ColumnType.BOOLEAN, "BOOLEAN"),
        # The expression system spells the timestamp its own way; the check is
        # that the type renders, not that it uses the model's shorthand.
        (ColumnType.TIMESTAMPTZ, "TIMESTAMP WITH TIME ZONE"),
        (ColumnType.JSONB, "JSONB"),
        (ColumnType.DATE, "DATE"),
    ],
)
def test_every_column_type_renders(column_type: ColumnType, expected: str) -> None:
    """Each type the model can declare is expressible by the renderer.

    A type that exists in the model but not in the renderer would only surface
    when a migration-free rebuild hit it in production.
    """
    from rhosocial.github_actions_tracker.schema.model import Column

    column = Column("probe", column_type, nullable=False)

    # The expression path builds the type node; a missing mapping raises here.
    node = ddl_postgres._data_type(column)
    assert node is not None
    assert expected in ddl_postgres.render_create_table(
        Table(
            name="t",
            columns=(
                Column("id", ColumnType.SERIAL, primary_key=True, nullable=False),
                column,
            ),
        )
    )


def test_varchar_length_is_honoured() -> None:
    from rhosocial.github_actions_tracker.schema.model import Column

    assert "VARCHAR(42)" in ddl_postgres.render_create_table(
        Table(
            name="t",
            columns=(
                Column("id", ColumnType.SERIAL, primary_key=True, nullable=False),
                Column("probe", ColumnType.VARCHAR, length=42),
            ),
        )
    )


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------


def _statement_for(table_name: str) -> str:
    for statement in _rendered():
        if statement.startswith(f'CREATE TABLE IF NOT EXISTS "{table_name}"'):
            return statement
    raise AssertionError(f"{table_name} is not rendered")


def test_the_log_tables_are_partitioned_by_bucket() -> None:
    """The two log tables render as partitioned parents, by month.

    ``PARTITION BY RANGE`` is not decoration: without it a month cannot be
    dropped as a unit, and the whole point of the bucket column is that it
    becomes one.
    """
    for name in ("ghat_step_logs", "ghat_job_logs"):
        assert _statement_for(name).endswith('PARTITION BY RANGE ("bucket")'), name


def test_no_other_table_is_partitioned() -> None:
    """Partitioning the delivery ledger too would buy nothing: it is queried by
    time, dropped by age, and small."""
    for name in EXPECTED_TABLES:
        if name not in ("ghat_step_logs", "ghat_job_logs"):
            assert "PARTITION BY" not in _statement_for(name), name


def test_a_partitioned_table_carries_the_key_in_its_uniques() -> None:
    """PostgreSQL requires the partition column in every unique key.

    What must survive is the 1:1 rule: ``UNIQUE (step_id)`` became
    ``UNIQUE (step_id, bucket)``, and the bucket is a function of the row's own
    parent -- so two rows that would have collided still collide.
    """
    step_logs = _statement_for("ghat_step_logs")
    assert 'UNIQUE ("step_id", "bucket")' in step_logs
    # The single-column form is what PostgreSQL rejects on a partitioned table.
    assert 'UNIQUE ("step_id")' not in step_logs

    job_logs = _statement_for("ghat_job_logs")
    assert 'UNIQUE ("job_id", "bucket")' in job_logs


def test_the_bucket_column_renders_as_a_date_on_both_log_tables() -> None:
    for name in ("ghat_step_logs", "ghat_job_logs"):
        assert re.search(r'"bucket" DATE', _statement_for(name)), name


def test_drop_order_covers_every_table_and_legacy_name() -> None:
    """A reset must drop everything, including names this schema retired."""
    dropped = " ".join(ddl_postgres.drop_statements(SCHEMA))
    for name in EXPECTED_TABLES:
        assert f'"{name}"' in dropped
    assert '"ghat_github_tokens"' in dropped


def test_drops_the_parent_not_each_partition() -> None:
    """One drop per table: a partitioned parent takes its partitions with it, so
    teardown never has to know the table became a partition set."""
    statements = ddl_postgres.drop_statements(SCHEMA)
    assert len(statements) == len(EXPECTED_TABLES) + len(SCHEMA.legacy_tables)
    assert not any("_2026_" in statement for statement in statements)
