# tests/controllers/test_token_connection_scope.py
"""TokenController borrows a connection for its provider's model calls.

Since the connection scope moved from per-command to per-operation, every
controller must open a scope around the database work it delegates. The
credential provider reads/writes ``AsyncPatCredential`` directly, so a
TokenController without a connection scope leaves those models unbound and
``list_tokens`` (the CLI's ``credential pat list``, and the init.d ``pat ensure``
path) raises "No backend configured". The other controllers were given a
DatabaseConnectionMixin when that change landed; this one was missed, and every
test ran it under ``bound`` (models already globally bound), hiding the gap.
"""

from __future__ import annotations

import pytest
from conftest import FakeGithub

from rhosocial.github_actions_tracker.controllers.token import TokenController
from rhosocial.github_actions_tracker.services.github import GithubCredentials


@pytest.mark.asyncio
async def test_token_controller_borrows_a_connection(pg_database, monkeypatch) -> None:
    """With a real Database and no fixture-bound models, provider calls work."""
    from rhosocial.github_actions_tracker.database import Database

    for key, name in (
        ("GHAT_DB_HOST", "host"),
        ("GHAT_DB_PORT", "port"),
        ("GHAT_DB_USER", "username"),
        ("GHAT_DB_PASSWORD", "password"),
        ("GHAT_DB_NAME", "database"),
    ):
        monkeypatch.setenv(key, str(pg_database[name]))
    # The tracker settings carry no sslrootcert, so a verify-full scenario cannot
    # be rebuilt from env. The connection-scope contract is independent of TLS
    # verification; prefer keeps this test about the backend binding.
    monkeypatch.setenv("GHAT_DB_SSLMODE", "prefer")

    database = await Database.create()
    try:
        controller = TokenController(probe=GithubCredentials(FakeGithub()), database=database)
        # The provider calls AsyncPatCredential.query(), the exact line that
        # raised "No backend configured" without the connection scope.
        rows = await controller.list_tokens()
        assert isinstance(rows, list)
    finally:
        await database.close()
