# tests/core/test_webhook_log_scope.py
"""A detached log fetch must not inherit the request's connection.

``asyncio.create_task`` copies the current context, and that context holds the
pooled connection the models were bound to. A task started that way keeps using
a connection the response has already returned to the pool -- the interleaving
the ORM warns about for a shared backend, except here it spans two requests.

So the fetch runs in an empty context. It does not need a connection of its own
either: the GitHub download is the long part and happens outside any borrow, and
each row it writes borrows from the pool itself, one operation at a time.

Without a database the fetch is skipped, which is honest: there is nothing it
could safely write to.
"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import asynccontextmanager
from types import SimpleNamespace

from conftest import make_webhook_service

#: Stands in for "the connection this request is bound to".
REQUEST_CONTEXT = contextvars.ContextVar("request_context", default="none")


class _Source:
    """A connection source: the protocol's one method, and nothing else."""

    @asynccontextmanager
    async def connection(self):
        yield SimpleNamespace()


class TestLogScope:
    async def test_the_fetch_runs_in_an_empty_context(self, bound, monkeypatch) -> None:
        service = make_webhook_service(database=_Source())
        seen: list[str] = []

        async def record(run, job) -> None:
            seen.append(REQUEST_CONTEXT.get())

        monkeypatch.setattr(service, "_fetch_job_logs", record)
        token = REQUEST_CONTEXT.set("the request connection")
        try:
            service._schedule_log_fetch(  # noqa: SLF001 - the seam under test
                SimpleNamespace(workflow_id=1, id=1),
                SimpleNamespace(id=1, github_job_id=2),
            )
            await asyncio.gather(*service._log_tasks)  # noqa: SLF001
        finally:
            REQUEST_CONTEXT.reset(token)

        assert seen == ["none"], "the task inherited the caller's connection"

    async def test_without_a_database_nothing_is_scheduled(self, bound) -> None:
        service = make_webhook_service()

        service._schedule_log_fetch(  # noqa: SLF001 - the seam under test
            SimpleNamespace(workflow_id=1, id=1),
            SimpleNamespace(id=1, github_job_id=2),
        )

        assert service._log_tasks == set()  # noqa: SLF001
