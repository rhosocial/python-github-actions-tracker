# tests/controllers/test_capture_relation_scope.py
"""Relation loading in the capture path must open its own connection scope.

The capture queue runs handlers in an empty ContextVar context (so they don't
inherit a webhook request's connection). `job.attempt()` reads the model
backend from that ContextVar; without a scope it raises "No backend configured",
which `_attempt_of` swallowed into None, which then crashed the caller on
`attempt.github_created_at`. Regression: open a scope around the relation load.
"""

from __future__ import annotations

import pytest

from rhosocial.github_actions_tracker.controllers.logs import _attempt_of
from rhosocial.github_actions_tracker.controllers.run import RunData
from rhosocial.github_actions_tracker.database import Database

REPO = "acme/demo"


@pytest.mark.asyncio
async def test_attempt_of_works_from_an_empty_context(pg_database, monkeypatch) -> None:
    # Use the suite's own PostgreSQL fixture (the CI service container or the
    # configured scenario) — never a hard-coded host, which CI cannot reach.
    for key, name in (
        ("GHAT_DB_HOST", "host"),
        ("GHAT_DB_PORT", "port"),
        ("GHAT_DB_USER", "username"),
        ("GHAT_DB_PASSWORD", "password"),
        ("GHAT_DB_NAME", "database"),
    ):
        monkeypatch.setenv(key, str(pg_database[name]))
    monkeypatch.setenv("GHAT_DB_SSLMODE", "prefer")

    import random

    suffix = random.randint(1, 2_000_000_000)
    database = await Database.create()
    try:
        store = RunData(database=database)
        # Build a minimal repo -> run -> attempt -> job chain via the existing
        # store helpers, which borrow their own scopes. IDs are randomised so
        # repeated runs against a shared database cannot collide.
        from rhosocial.github_actions_tracker.models import AsyncRepository

        async with database.connection():
            repo = await AsyncRepository.find_by_github_id(suffix)
            if repo is None:
                repo = AsyncRepository(
                    github_repo_id=suffix, full_name=REPO, owner="acme", name="demo"
                )
                await repo.save()

        run = await store.adopt_run(
            repo,
            {
                "id": suffix,
                "run_number": 1,
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
                "head_branch": "main",
                "head_sha": "b" * 40,
                "event": "push",
                "created_at": "2026-11-20T00:00:00Z",
                "workflow_id": 7,
                "path": ".github/workflows/ci.yml",
                "name": "CI",
            },
        )
        job = await store.store_job(
            run,
            {
                "id": suffix,
                "name": "build",
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-11-20T00:00:01Z",
                "completed_at": "2026-11-20T00:01:00Z",
                "steps": [
                    {"number": 1, "name": "x", "status": "completed", "conclusion": "success"}
                ],
            },
        )
        assert job is not None

        # The exact condition that failed in production: no connection scope is
        # open in this context (store helpers borrow and return their own).
        # _attempt_of must open its own scope to resolve the relation.
        attempt = await _attempt_of(job, store.connection)
        assert attempt is not None, "attempt relation did not load from an unbound context"
        assert getattr(attempt, "id", None) is not None
    finally:
        await database.close()
