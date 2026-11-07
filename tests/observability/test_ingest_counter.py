# tests/observability/test_ingest_counter.py
"""The ingest counter moves, or the next person deletes it as dead code.

``ghat_run_ingest_total`` was declared, exported and documented -- with a comment
explaining why ``created`` and ``updated`` are separate outcomes -- and never
incremented anywhere. The exposition happily printed its ``# HELP`` and ``# TYPE``
with no samples under them, which reads on a dashboard as "this service has never
ingested a run" rather than "this counter is unwired". It survived that way for a
week, because nothing failed: a metric nobody reads costs nothing until somebody
draws a graph on it.

Seen here: the delivery ledger's new capture note answered "how many step logs
did the last completion store" from a table read, while this counter -- whose
whole job is that question for runs -- sat at zero.
"""

from __future__ import annotations

from rhosocial.github_actions_tracker.controllers import RunData
from rhosocial.github_actions_tracker.models import AsyncRepository
from rhosocial.github_actions_tracker.observability import reset
from rhosocial.github_actions_tracker.observability.exposition import metrics_app_text

REPO = "acme/demo"


def _run_payload(run_id: int = 4242, *, action: str = "completed") -> dict:
    completed = action == "completed"
    return {
        "action": action,
        "repository": {"id": 1, "full_name": REPO},
        "workflow_run": {
            "id": run_id,
            "run_number": 12,
            "run_attempt": 1,
            "status": "completed" if completed else "in_progress",
            "conclusion": "success" if completed else None,
            "head_branch": "feature/x",
            "head_sha": "a" * 40,
            "event": "push",
            "head_commit": {"message": "fix things"},
        },
    }


async def _repository() -> AsyncRepository:
    repo = AsyncRepository(github_repo_id=1, full_name=REPO, owner="acme", name="demo")
    await repo.save()
    return repo


def _count(result: str) -> int:
    """The counter's value for one outcome, read out of the exposition."""
    text = metrics_app_text()
    for line in text.splitlines():
        if line.startswith(f'ghat_run_ingest_total{{result="{result}"}}'):
            return int(line.rsplit(" ", 1)[1])
    return 0


class TestTheCounterMoves:
    async def test_a_new_run_row_is_counted_as_created(self, bound) -> None:
        reset()
        repo = await _repository()
        store = RunData()

        await store.adopt_run(repo, _run_payload()["workflow_run"])

        assert _count("created") == 1
        assert _count("updated") == 0, (
            "adopting a run the database does not have is not an update; the "
            "separation is the whole point of the label"
        )

    async def test_applying_a_payload_to_a_stored_run_is_counted_as_updated(self, bound) -> None:
        """A redelivery of a run already stored is an update, not a new run."""
        reset()
        repo = await _repository()
        store = RunData()

        await store.adopt_run(repo, _run_payload()["workflow_run"])
        run = await store.run(4242, with_state=True)
        assert run is not None, "the run just adopted must be found again"
        await store.update_run(run, _run_payload()["workflow_run"])

        assert _count("created") == 1
        assert _count("updated") == 1
