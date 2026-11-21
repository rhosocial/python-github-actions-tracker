# tests/adapters/test_cli_repo_scopes.py
"""The repo CLI's credential lookups borrow a connection scope.

`repo subscribe --credential` and `repo list` read credential/binding models
through module-level helpers. Those models need a connection on the pool's
ContextVar; after the per-operation scope refactor they no longer get one from
a command-wide binding, so the helpers must borrow one themselves. These tests
run with a real Database and no fixture-bound models — the exact condition that
produced "No backend configured" on a real deploy.
"""

from __future__ import annotations

import pytest

from rhosocial.github_actions_tracker.adapters.cli.commands import repo as repo_command


@pytest.mark.asyncio
async def test_resolve_credential_works_without_a_global_binding(pg_database, monkeypatch) -> None:
    """_resolve_credential borrows a scope, so find_by_name has a backend."""

    from conftest import FakeGithub

    from rhosocial.github_actions_tracker.controllers.token import TokenController
    from rhosocial.github_actions_tracker.database import Database
    from rhosocial.github_actions_tracker.services.github import GithubCredentials

    for key, name in (
        ("GHAT_DB_HOST", "host"),
        ("GHAT_DB_PORT", "port"),
        ("GHAT_DB_USER", "username"),
        ("GHAT_DB_PASSWORD", "password"),
        ("GHAT_DB_NAME", "database"),
    ):
        monkeypatch.setenv(key, str(pg_database[name]))
    monkeypatch.setenv("GHAT_DB_SSLMODE", "prefer")

    database = await Database.create()
    try:
        token = TokenController(probe=GithubCredentials(FakeGithub()), database=database)
        # register a credential so there is something to resolve
        await token.add_token(name="resolve-me", token="ghp_resolve_me")

        # _resolve_credential only needs `service.connection()`; a minimal
        # stand-in with the real database's scope is enough to prove the point.
        class _Scoped:
            def connection(self):
                return database.connection()

        kind, ref = await repo_command._resolve_credential("pat/resolve-me", _Scoped())
        assert kind == "pat" and isinstance(ref, int) and ref > 0
    finally:
        await database.close()
