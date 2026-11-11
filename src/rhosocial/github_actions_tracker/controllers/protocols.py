# src/rhosocial/github_actions_tracker/controllers/protocols.py
"""What a controller needs from the thing it gets connections from.

The controllers never hold a connection: they hold something that can lend one.
This protocol is that promise, and it is deliberately smaller than
:class:`~rhosocial.github_actions_tracker.database.Database` -- a test can pass
any object with a ``connection()`` method instead of building a pool.

``Database`` satisfies it structurally, so nothing here imports it.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

__all__ = ["ConnectionSource"]


class ConnectionSource(Protocol):
    """Something a controller can borrow a connection from for one block."""

    def connection(self, *, timeout: float | None = None) -> AbstractAsyncContextManager[Any]:
        """Return a context manager that yields one connection.

        Args:
            timeout: How long to wait for one before giving up. The source's
                own default applies when omitted.
        """
        ...
