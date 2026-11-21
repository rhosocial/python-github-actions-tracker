# tests/core/test_startup_database_error.py
"""A cold start reports an unreachable database as one actionable line.

A listener cannot start without its database. The backend's connect failure is
a verbose psycopg traceback that names no host and suggests no cause; at startup
the cause is almost always external (database not up, wrong name, DNS). The
cold-start path must turn it into a DatabaseUnavailableError carrying the
coordinates and likely causes, with the original error chained for anyone who
wants the traceback.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from rhosocial.activerecord.backend.errors import ConnectionError as BackendConnectionError

from rhosocial.github_actions_tracker.config import DatabaseSettings, Settings
from rhosocial.github_actions_tracker.core.exceptions import DatabaseUnavailableError
from rhosocial.github_actions_tracker.database import (
    Database,
    connection_failure_message,
)


def _settings() -> Settings:
    return Settings(database=DatabaseSettings(host="db.internal", port=5432, database="ghat"))


def test_failure_message_names_coordinates_and_causes() -> None:
    message = connection_failure_message(_settings(), BackendConnectionError("dns failed"))
    assert "db.internal:5432/ghat" in message
    assert "dns failed" in message
    assert "Docker DNS" in message
    assert "credentials" in message


@pytest.mark.asyncio
async def test_startup_connection_wraps_backend_error() -> None:
    """A backend connect failure becomes DatabaseUnavailableError, chained."""
    original = BackendConnectionError("failed to resolve host 'db.internal'")

    class _StubbingDatabase(Database):
        @asynccontextmanager
        async def connection(self, *, timeout=None):  # type: ignore[override]
            raise original
            yield  # pragma: no cover

    database = _StubbingDatabase.__new__(_StubbingDatabase)
    database._settings = _settings()

    with pytest.raises(DatabaseUnavailableError) as info:
        async with database.startup_connection():
            pass

    message = str(info.value)
    assert "db.internal:5432/ghat" in message
    assert info.value.__cause__ is original
