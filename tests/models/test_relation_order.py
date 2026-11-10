# tests/models/test_relation_order.py
"""The declared order of every ordered relation, read back from PostgreSQL.

The models promise a stable order on four relations: ``repository.workflows``
(id), ``workflow.runs`` (run number, newest first), ``run.attempts`` (attempt
number) and ``job.steps`` (number). Those promises used to be kept by a local
``OrderedHasMany`` that sorted the batch-load result in Python; they are now the
library's ``order_by=...``, which puts the ORDER BY in the SQL instead and
appends the primary key as a tie-breaker. This file pins the promises, not the
mechanism -- what each relation actually answers.

The rows are inserted in an order that is deliberately *not* the declared order
-- steps numbered 3, 1, 2 and runs numbered 1, 3, 2 -- so a relation that forgot
its ``order_by`` would hand back the insertion order and fail here rather than
pass by coincidence. The tie-breaker case is the one worth spelling out: two
runs sharing a ``run_number`` come back in local id order, which the old
Python-side sort only got by accident, as a stable sort over whatever order the
query happened to produce.
"""

from __future__ import annotations

from rhosocial.github_actions_tracker import models


async def _repository(full_name: str, github_repo_id: int) -> models.AsyncRepository:
    repo = models.AsyncRepository(
        github_repo_id=github_repo_id, full_name=full_name, owner="acme", name="order"
    )
    await repo.save()
    assert repo.id is not None
    return repo


async def _workflow(repo_id: int, workflow_id: int, path: str) -> models.AsyncWorkflow:
    workflow = models.AsyncWorkflow(
        repository_id=repo_id,
        workflow_id=workflow_id,
        name="CI",
        path=path,
        state="active",
    )
    await workflow.save()
    assert workflow.id is not None
    return workflow


async def _run(workflow_id: int, github_run_id: int, run_number: int) -> models.AsyncWorkflowRun:
    run = models.AsyncWorkflowRun(
        workflow_id=workflow_id,
        github_run_id=github_run_id,
        run_number=run_number,
        run_attempt=1,
        status="completed",
        conclusion="success",
        branch="main",
    )
    await run.save()
    return run


async def test_every_ordered_relation_keeps_its_order(bound) -> None:
    repo = await _repository("acme/order", 4_001)
    ci = await _workflow(repo.id, 7_101, ".github/workflows/ci.yml")
    nightly = await _workflow(repo.id, 7_102, ".github/workflows/nightly.yml")

    # Insert run_number 1, 3, 2, and a second run sharing the number 3.
    await _run(ci.id, 90_001, 1)
    newest = await _run(ci.id, 90_003, 3)
    await _run(ci.id, 90_002, 2)
    await _run(ci.id, 90_004, 3)

    reloaded = await models.AsyncWorkflow.find_one(models.AsyncWorkflow.c.id == ci.id)
    assert reloaded is not None
    # Newest first; the two runs that share run_number 3 answer in id order.
    assert [row.github_run_id for row in await reloaded.runs()] == [90_003, 90_004, 90_002, 90_001]

    repo_reloaded = await models.AsyncRepository.find_one(models.AsyncRepository.c.id == repo.id)
    assert repo_reloaded is not None
    workflows = await repo_reloaded.workflows()
    assert [row.path for row in workflows] == [
        ".github/workflows/ci.yml",
        ".github/workflows/nightly.yml",
    ]
    assert [row.id for row in workflows] == [ci.id, nightly.id]

    run = await models.AsyncWorkflowRun.find_one(models.AsyncWorkflowRun.c.id == newest.id)
    assert run is not None
    await models.AsyncRunAttempt(run_id=run.id or 0, attempt_number=2, status="completed").save()
    await models.AsyncRunAttempt(run_id=run.id or 0, attempt_number=1, status="completed").save()
    assert [row.attempt_number for row in await run.attempts()] == [1, 2]

    attempt = await models.AsyncRunAttempt.find_attempt(run.id or 0, 1)
    assert attempt is not None
    job = models.AsyncJob(attempt_id=attempt.id or 0, github_job_id=80_001, name="tests")
    await job.save()
    for number in (3, 1, 2):
        await models.AsyncStep(
            job_id=job.id or 0,
            github_step_id=70_000 + number,
            number=number,
            name=f"step {number}",
        ).save()

    job_reloaded = await models.AsyncJob.find_one(models.AsyncJob.c.id == job.id)
    assert job_reloaded is not None
    assert [row.number for row in await job_reloaded.steps()] == [1, 2, 3]

    # Eager loading answers the same order: the ORDER BY is in the query, so
    # the batched load sees it too. The Python-side sort could not promise this.
    loaded = await (
        models.AsyncJob.query().where(models.AsyncJob.c.id == job.id).with_("steps").all()
    )
    assert len(loaded) == 1
    assert [row.number for row in await loaded[0].steps()] == [1, 2, 3]


async def test_a_relation_with_one_row_answers_that_row(bound) -> None:
    """The order must not swallow or duplicate a singleton collection."""
    repo = await _repository("acme/single", 4_002)
    workflow = await _workflow(repo.id, 7_103, ".github/workflows/ci.yml")
    only = await _run(workflow.id, 91_001, 1)

    reloaded = await models.AsyncWorkflow.find_one(models.AsyncWorkflow.c.id == workflow.id)
    assert reloaded is not None
    runs = await reloaded.runs()
    assert [row.id for row in runs] == [only.id]

    run = await models.AsyncWorkflowRun.find_one(models.AsyncWorkflowRun.c.id == only.id)
    assert run is not None
    assert [row.attempt_number for row in await run.attempts()] == []
    assert [row.github_run_id for row in await reloaded.runs()] == [91_001]
