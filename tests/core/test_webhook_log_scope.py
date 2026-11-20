# tests/core/test_webhook_log_scope.py
"""A detached capture must not inherit the request's context.

``asyncio.create_task`` copies the current context, which holds the pooled
connection the models were bound to. A task started inside the request would
keep using a connection the response had already returned to the pool. The
capture queue creates its tasks in a fresh context, so the work runs with no
binding and borrows its own connections for the rows it writes.
"""

from __future__ import annotations

import contextvars
from types import SimpleNamespace

from conftest import make_webhook_service

#: Stands in for "the connection this request is bound to".
REQUEST_CONTEXT = contextvars.ContextVar("request_context", default="none")


class _Source:
    """A connection source: the protocol's one method, and nothing else."""

    async def connection(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def cm():
            yield None

        return cm()


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
            await service.start_captures()
            await service.drain_captures()
        finally:
            REQUEST_CONTEXT.reset(token)

        assert seen == ["none"], "the task inherited the caller's connection"

    async def test_without_a_database_nothing_is_scheduled(self, bound) -> None:
        service = make_webhook_service()

        service._schedule_log_fetch(  # noqa: SLF001 - the seam under test
            SimpleNamespace(workflow_id=1, id=1),
            SimpleNamespace(id=1, github_job_id=2),
        )

        assert service.capture_health.pending == 0
        assert service.capture_health.dropped == 1
