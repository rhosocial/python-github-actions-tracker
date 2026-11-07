# tests/models/test_workflow_identity.py
"""A workflow is identified by GitHub's id, and it is history that stays.

GitHub allows a workflow to be renamed, moved or deleted at any time. A run, once
made, still belongs to the workflow that existed when it was triggered -- so this
project keeps two things apart:

* **identity** -- ``workflow_id``, the only key aggregation and filtering use;
* **attributes** -- ``name``/``path``/``state``, whatever GitHub says *now*.

The row is never removed while runs point at it: a deleted workflow becomes a
tombstone (``state='deleted'``), because the foreign key is ``RESTRICT`` and a
cascade here would take a whole run history with one statement.
"""

from __future__ import annotations

import pytest
from rhosocial.activerecord.backend.errors import IntegrityError

from rhosocial.github_actions_tracker.models import (
    AsyncRepository,
    AsyncWorkflow,
    AsyncWorkflowRun,
)


async def _repository() -> AsyncRepository:
    repository = AsyncRepository(github_repo_id=1, full_name="acme/demo", owner="acme", name="demo")
    await repository.save()
    return repository


def _run_payload(workflow_id: int = 7, name: str = "CI") -> dict[str, object]:
    """What ``Run.adopt_from_remote`` hands to the workflow upsert.

    A run payload carries the workflow identity and labels, and **no state** --
    the state belongs to the workflow, not to the execution.
    """
    return {
        "workflow_id": workflow_id,
        "name": name,
        "path": ".github/workflows/ci.yml",
    }


class TestIdentity:
    async def test_by_github_id_is_the_lookup(self, bound) -> None:
        repository = await _repository()
        workflow = await AsyncWorkflow(repository_id=repository.id or 0).upsert_from_remote(
            _run_payload()
        )

        found = await AsyncWorkflow.by_github_id(repository.id or 0, 7)

        assert found is not None
        assert found.id == workflow.id

    async def test_a_rename_keeps_the_same_row(self, bound) -> None:
        """Name and path change; the id does not, so the aggregation still sees
        one workflow rather than two."""
        repository = await _repository()
        first = await AsyncWorkflow(repository_id=repository.id or 0).upsert_from_remote(
            _run_payload()
        )

        renamed = await AsyncWorkflow(repository_id=repository.id or 0).upsert_from_remote(
            {**_run_payload(), "name": "Renamed"}
        )

        assert renamed.id == first.id
        assert renamed.name == "Renamed"


class TestTheStateIsGitHubs:
    async def test_a_run_payload_creates_an_unknown_state(self, bound) -> None:
        """We know the workflow exists; we do not know it is healthy. Saying
        ``active`` here would be a claim the payload never made."""
        repository = await _repository()

        workflow = await AsyncWorkflow(repository_id=repository.id or 0).upsert_from_remote(
            _run_payload()
        )

        assert workflow.state == "unknown"

    async def test_an_old_run_does_not_resurrect_a_deleted_workflow(self, bound) -> None:
        repository = await _repository()
        workflow = await AsyncWorkflow(repository_id=repository.id or 0).upsert_from_remote(
            _run_payload()
        )
        workflow.state = "deleted"
        await workflow.save()

        again = await AsyncWorkflow(repository_id=repository.id or 0).upsert_from_remote(
            _run_payload()
        )

        assert again.id == workflow.id
        assert again.state == "deleted"

    async def test_the_state_is_taken_when_the_payload_carries_it(self, bound) -> None:
        """``workflow_run`` deliveries have the workflow object beside the run;
        that is where ``active``/``disabled_*``/``deleted`` comes from."""
        repository = await _repository()

        workflow = await AsyncWorkflow(repository_id=repository.id or 0).upsert_from_remote(
            {**_run_payload(), "state": "disabled_manually"}
        )

        assert workflow.state == "disabled_manually"


class TestTheRowIsHistory:
    async def test_deleting_a_workflow_with_runs_is_refused(self, bound) -> None:
        """RESTRICT, not CASCADE: one ``DELETE`` must not take a run history."""
        repository = await _repository()
        workflow = await AsyncWorkflow(repository_id=repository.id or 0).upsert_from_remote(
            _run_payload()
        )
        run = AsyncWorkflowRun(
            workflow_id=workflow.id or 0,
            github_run_id=11,
            run_number=1,
            event="push",
            branch="main",
            commit_sha="a" * 40,
        )
        await run.save()

        backend = AsyncWorkflow.__backend__
        with pytest.raises(IntegrityError):
            await backend.execute("DELETE FROM ghat_workflows WHERE id = %s", (workflow.id,))

        kept = await AsyncWorkflow.find_one(AsyncWorkflow.c.id == (workflow.id or 0))
        assert kept is not None
        assert await AsyncWorkflowRun.find_one(AsyncWorkflowRun.c.id == (run.id or 0)) is not None
