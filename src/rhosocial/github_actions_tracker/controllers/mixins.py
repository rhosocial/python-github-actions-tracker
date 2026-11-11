# src/rhosocial/github_actions_tracker/controllers/mixins.py
"""The implementation behind :class:`ConnectionSource`, mixed into controllers.

A controller holds the source, not a connection. Every method that touches the
database wraps its database statements::

    async def repository(self, full_name):
        async with self.connection():
            return await AsyncRepository.find_one(...)

and everything else a controller does -- calling GitHub, downloading a log,
sleeping between polls -- happens outside that block, because it is outside the
methods that borrow.

Nested borrows reuse the outer connection: the pool keeps the current connection
on a ``ContextVar`` and hands the same one back, so a method calling another
method of the same controller costs one connection, not two.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from .protocols import ConnectionSource

__all__ = ["DatabaseConnectionMixin"]


class DatabaseConnectionMixin:
    """Hold a :class:`ConnectionSource` and lend from it per database block."""

    _database: ConnectionSource | None = None

    def __init__(self, *, database: ConnectionSource | None = None, **kwargs: Any) -> None:
        """Take the source, and leave the rest of the arguments to the next base.

        Args:
            database: What to borrow from. Omitted where the caller already holds
                a connection -- a test binding the models to a backend runs in a
                fixture's context, and every method then runs where it was called.
        """
        super().__init__(**kwargs)
        self._database = database

    @asynccontextmanager
    async def connection(self):
        """Borrow one connection for the length of the ``async with`` block."""
        if self._database is None:
            yield None
            return
        async with self._database.connection():
            yield None
