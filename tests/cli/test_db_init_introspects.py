# tests/cli/test_db_init_introspects.py
"""`db init` must introspect the backend before running DDL.

Partition maintenance — which `create_all` performs — is gated on the server
version, and the dialect only learns that version in `introspect_and_adapt()`.
The service path gets this from the pool, and the test fixture calls it
explicitly, so a CLI path that skips it passes local tests but fails on a real
`db init` against a fresh database (as CI caught). This pins the call order
without needing a live server.
"""

from __future__ import annotations

import argparse
from typing import Any

from rhosocial.github_actions_tracker.adapters.cli import db as cli_db


class _RecordingBackend:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def connect(self) -> None:
        self._calls.append("connect")

    async def introspect_and_adapt(self) -> None:
        self._calls.append("introspect")

    async def disconnect(self) -> None:
        self._calls.append("disconnect")


class _NoopServer:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def ensure(self) -> bool:
        self._calls.append("ensure_database")
        return False


def test_db_init_introspects_before_create_all(monkeypatch: Any) -> None:
    calls: list[str] = []

    class _Config:
        database = "d"

    monkeypatch.setattr(cli_db, "get_connection_config", lambda: _Config())
    monkeypatch.setattr(cli_db, "AsyncPostgresBackend", lambda **_: _RecordingBackend(calls))
    monkeypatch.setattr(
        "rhosocial.github_actions_tracker.database.PostgresServer",
        lambda *_: _NoopServer(calls),
    )

    async def fake_create_all(backend: Any) -> None:
        calls.append("create_all")

    monkeypatch.setattr(cli_db, "create_all", fake_create_all)

    args = argparse.Namespace(no_create_database=False, maintenance_db="postgres")
    cli_db._run(cli_db.cmd_init(args))

    assert "introspect" in calls, "db init skipped introspection; partition DDL will fail"
    assert calls.index("connect") < calls.index("introspect") < calls.index("create_all")
    assert calls[-1] == "disconnect"
