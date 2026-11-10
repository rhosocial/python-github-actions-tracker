# tests/models/test_workflow_by_path.py
"""The by-path lookup picks a row on purpose, not by plan.

``AsyncWorkflow.by_path`` exists for a payload whose workflow id was never
learned: the row is stored under its path with ``workflow_id = 0`` and a later
delivery fills the id in. While such a row is pending -- and for as long as a
database predates the ``(repository_id, path)`` constraint -- one path can hold
more than one row, and the plain ``find_one`` this replaces was
``LIMIT 1`` with **no ORDER BY**: the answer depended on which row the planner
reached first, which is the undefined behaviour the library's
``find_one_or_create`` docstring warns about for a key without a constraint.

The rule pinned here is the newest row wins, which is the same last-write-wins
the model already follows in :meth:`AsyncWorkflow.refresh` and in
:meth:`AsyncWorkflow.not_found`.

The constraint this schema now declares makes duplicates impossible, so the
interesting shape is constructed the way a pre-migration database would have
it: the constraint is dropped for the duration of one test and restored after.
That is the whole point -- a test that only ran under the constraint would pass
with the ordering removed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from rhosocial.github_actions_tracker.controllers.run import RunData
from rhosocial.github_actions_tracker.models import AsyncRepository, AsyncWorkflow

CONSTRAINT = "ghat_workflows_repository_id_path_key"


@asynccontextmanager
async def pre_migration_shape(backend, *, repository_id: int, path: str) -> AsyncIterator[None]:
    """The shape a database has before the constraint was declared.

    Drops it for the duration of the block, and on the way out does the two
    things in the order they must happen: **the rows this test wrote go first**,
    because re-adding the constraint over duplicates fails -- and that failure
    would leave the session's schema without the invariant, so a later test
    would pass for the wrong reason. The delete is scoped to this test's own
    repository and path, which are constants of the test, so it cannot touch
    another test's rows.
    """
    await backend.execute(f"ALTER TABLE ghat_workflows DROP CONSTRAINT {CONSTRAINT}")
    try:
        yield
    finally:
        await backend.execute(
            f"DELETE FROM ghat_workflows WHERE repository_id = {repository_id} "
            f"AND path = '{path}'"
        )
        await backend.execute(
            f"ALTER TABLE ghat_workflows ADD CONSTRAINT {CONSTRAINT} "
            "UNIQUE (repository_id, path)"
        )


async def _repository() -> AsyncRepository:
    repository = AsyncRepository(
        github_repo_id=7, full_name="acme/paths", owner="acme", name="paths"
    )
    await repository.save()
    return repository


async def _workflow(repo_id: int, *, workflow_id: int, path: str) -> AsyncWorkflow:
    workflow = AsyncWorkflow(repository_id=repo_id, workflow_id=workflow_id, name="CI", path=path)
    await workflow.save()
    return workflow


class TestOneRowPerPath:
    async def test_the_only_row_is_found(self, bound) -> None:
        repo = await _repository()
        stored = await _workflow(repo.id or 0, workflow_id=7, path=".github/workflows/ci.yml")

        found = await AsyncWorkflow.by_path(repo.id or 0, ".github/workflows/ci.yml")

        assert found is not None
        assert found.id == stored.id

    async def test_another_repositorys_path_is_not_a_match(self, bound) -> None:
        """The path is a key *within* a repository, never across them."""
        repo = await _repository()
        other_repo = AsyncRepository(
            github_repo_id=8, full_name="acme/other", owner="acme", name="other"
        )
        await other_repo.save()
        await _workflow(repo.id or 0, workflow_id=7, path=".github/workflows/ci.yml")

        found = await AsyncWorkflow.by_path(other_repo.id or 0, ".github/workflows/ci.yml")

        assert found is None

    async def test_a_blank_path_is_not_a_lookup(self, bound) -> None:
        """``path=''`` would match every row stored without one."""
        repo = await _repository()
        await _workflow(repo.id or 0, workflow_id=7, path="")

        assert await AsyncWorkflow.by_path(repo.id or 0, "") is None


class TestTwoRowsAtOnePath:
    async def test_the_newest_row_wins(self, bound_models) -> None:
        """The pre-migration shape: two rows, one path, no constraint.

        The ordering is what makes this test worth writing at all. Under the
        declared constraint the answer can only be one row, so a test that ran
        only there would still pass with the ``order_by`` removed.
        """
        repo = await _repository()
        path = ".github/workflows/ci.yml"
        repo_id = repo.id or 0

        async with pre_migration_shape(bound_models, repository_id=repo_id, path=path):
            older = await _workflow(repo_id, workflow_id=7, path=path)
            newer = await _workflow(repo_id, workflow_id=0, path=path)

            found = await AsyncWorkflow.by_path(repo_id, path)

            assert found is not None
            # The row written last answers, which is the row whose values the
            # caller's payload is about to refresh.
            assert found.id == max(older.id or 0, newer.id or 0)
            assert found.id == newer.id

    async def test_the_controller_picks_the_same_row(self, bound_models) -> None:
        """``store_workflow`` reads through ``by_path``, so the controller's
        answer is the ordered one -- the run is filed under the row the model
        would pick, not under whichever row the planner reached."""
        repo = await _repository()
        path = ".github/workflows/ci.yml"
        repo_id = repo.id or 0

        async with pre_migration_shape(bound_models, repository_id=repo_id, path=path):
            await _workflow(repo_id, workflow_id=7, path=path)
            newest = await _workflow(repo_id, workflow_id=0, path=path)

            # A payload that names the path but not the id: the delivery that
            # created the second row in the first place.
            record = await RunData().store_workflow(
                repo, {"workflow_id": 0, "name": "CI", "path": path}
            )
            assert record.id == newest.id


class TestTheConstraintIsEnforced:
    async def test_two_rows_at_one_path_are_refused(self, bound) -> None:
        """The database now says what the code assumed.

        ``find_one_or_create`` cannot recover from a violation that never
        fires; with the constraint in place the racing insert raises one, the
        loser re-reads, and both callers end up holding the winner's row.
        """
        from rhosocial.activerecord.backend.errors import IntegrityError

        repo = await _repository()
        await _workflow(repo.id or 0, workflow_id=7, path=".github/workflows/ci.yml")

        with pytest.raises(IntegrityError):
            await _workflow(repo.id or 0, workflow_id=9, path=".github/workflows/ci.yml")
