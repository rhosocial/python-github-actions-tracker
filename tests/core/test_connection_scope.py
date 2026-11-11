# tests/core/test_connection_scope.py
"""Waiting on GitHub must not occupy a database connection.

The measured difference, on a pool of two connections and ten concurrent
callers, each spending eight seconds on something that is not the database:

* borrow, then wait, then read: 8 of 10 succeed, median 24.6 seconds, and the
  pool records two acquire timeouts -- the failure 2026-11-08 showed in
  production;
* wait, then borrow, then read: 10 of 10 succeed, median 8.6 seconds, none.

``scripts/probe_connection_scope.py`` reproduces both numbers on demand. This
file pins the second shape against the real controller mixin, the real pool, and
the real database: the caller waits first, and ``RunData`` borrows a connection
only for its own query.

The sampling inside the wait is the assertion that matters. A test that only
checked "every call returned" would pass under the old shape too -- slower, but
passing -- until the pool happened to be small enough for the queue to overflow.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig
from rhosocial.activerecord.connection.pool import AsyncBackendPool, PoolConfig

from rhosocial.github_actions_tracker.controllers.run import RunData
from rhosocial.github_actions_tracker.database import Database

#: How long each caller pretends to wait for GitHub.
WAIT_SECONDS = 2.0
#: Concurrent callers, deliberately more than the pool has connections.
CALLERS = 10
#: Connections available to all of them.
POOL_SIZE = 2


@pytest.fixture
async def small_pool(pg_scenario: dict[str, object]):
    """A pool of two against the test server, closed afterwards."""
    from pg import TEST_DATABASE

    config = {**pg_scenario, "database": TEST_DATABASE}
    pool = await AsyncBackendPool.create(
        PoolConfig(
            min_size=1,
            max_size=POOL_SIZE,
            backend_factory=lambda: AsyncPostgresBackend(
                connection_config=PostgresConnectionConfig(**config)
            ),
        )
    )
    database = Database(pool)
    try:
        yield database
    finally:
        await database.close()


async def test_waiting_holds_no_connection(small_pool: Database) -> None:
    data = RunData(database=small_pool)

    async def caller() -> None:
        # The GitHub call, or the retry sleep, or anything else that is not SQL.
        await asyncio.sleep(WAIT_SECONDS)
        # The database work, and nothing else, borrows.
        await data.count()

    tasks = [asyncio.create_task(caller()) for _ in range(CALLERS)]
    # Sample while every caller is still waiting: a connection held across the
    # wait would show up here, and that is precisely the shape being forbidden.
    await asyncio.sleep(WAIT_SECONDS / 2)
    during_wait = small_pool.pool.get_stats().current_in_use

    await asyncio.gather(*tasks)

    stats = small_pool.pool.get_stats()
    assert during_wait == 0, "a caller held a connection while waiting"
    assert stats.total_timeouts == 0, "nobody had to wait for a connection"
    assert stats.current_in_use == 0, "every borrow was returned"


class _CountingSource:
    """A source that records how many borrows were opened and closed."""

    def __init__(self) -> None:
        self.enters = 0
        self.leaves = 0

    @asynccontextmanager
    async def connection(self):
        self.enters += 1
        try:
            yield None
        finally:
            self.leaves += 1


async def test_a_borrow_is_closed_when_the_query_fails() -> None:
    """A failure inside the block must not leave the borrow open.

    The source yields nothing, so the model raises ``No backend configured``
    from inside the block -- the cheapest way to make the borrowed window fail
    without a database.
    """
    from rhosocial.activerecord.backend.errors import DatabaseError

    source = _CountingSource()
    data = RunData(database=source)

    with pytest.raises(DatabaseError):
        await data.count()

    assert source.enters == 1, "the borrow opened before the query"
    assert source.leaves == 1, "and closed on the way out"
