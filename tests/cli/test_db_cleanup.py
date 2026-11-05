# tests/cli/test_db_cleanup.py
"""The cleanup command says which of its two shapes is the cheap one.

``db cleanup --logs-before`` still works -- a pre-partition database has rows to
delete -- but the log tables are partitioned now, and a row-by-row delete is the
expensive shape of an intent the partition command answers in one metadata
operation. The deprecation note is the whole point of this test: an operator who
runs the old flag must be told about the new one, not quietly served the slower
path.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any


class _Result:
    def __init__(self, affected: int) -> None:
        self.affected_rows = affected
        self.data: list[dict[str, Any]] = []


class _Backend:
    """Records the statements it was asked to run."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def execute(self, statement: str, params: Any = None) -> _Result:
        self.statements.append(statement)
        return _Result(0)


def _config() -> Any:
    from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

    return PostgresConnectionConfig(
        host="localhost", port=5432, username="u", password="p", database="d"
    )


def _args(**overrides: Any) -> argparse.Namespace:
    values = {
        "runs_before": None,
        "logs_before": None,
        "deliveries_before": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _run(monkeypatch, args: argparse.Namespace) -> tuple[str, _Backend]:
    from rhosocial.github_actions_tracker.adapters.cli import db as db_cli

    backend = _Backend()
    monkeypatch.setattr(db_cli, "AsyncPostgresBackend", lambda connection_config: backend)
    monkeypatch.setattr(db_cli, "get_connection_config", _config)
    asyncio.run(db_cli.cmd_cleanup(args))
    return backend.statements, backend


def test_logs_before_points_at_the_partitioned_path(monkeypatch, capsys) -> None:
    """The deprecation note names the flag to use instead, before any deletion."""
    statements, _ = _run(monkeypatch, _args(logs_before="365d"))

    said = capsys.readouterr().out
    assert "partitions --drop-before" in said
    assert any("DELETE FROM ghat_job_logs" in item for item in statements)


def test_the_other_flags_do_not_claim_a_deprecation(monkeypatch, capsys) -> None:
    """Only the log flag is superseded; ``--runs-before`` and the delivery ledger
    stay as they are, so they must not inherit the note."""
    _run(monkeypatch, _args(runs_before="90d", deliveries_before="30d"))

    said = capsys.readouterr().out
    assert "partitions --drop-before" not in said
