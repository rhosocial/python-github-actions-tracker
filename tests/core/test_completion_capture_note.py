# tests/core/test_completion_capture_note.py
"""A completed delivery says what its capture got, not just that it ran.

The one moment the per-step archive is guaranteed to be near is the instant the
run finishes: GitHub has just written it, and it stays fetchable for only about
an hour. A capture attempt at that moment is therefore worth recording -- a
shortfall is a window running down, which is invisible in a ledger that says only
"processed".

Seen once: the ledger showed ``processed`` for every completion and nothing else,
so "the delivery worked" and "we have the step logs" looked identical from the
outside, and the only way to tell them apart was to read the container's log.
"""

from __future__ import annotations

from conftest import make_webhook_service

from rhosocial.github_actions_tracker.controllers.webhook import _capture_note
from rhosocial.github_actions_tracker.models import (
    AsyncRepository,
    AsyncWebhookDelivery,
    AsyncWorkflow,
    AsyncWorkflowRun,
)
from rhosocial.github_actions_tracker.views import LogCaptureView

REPO = "acme/demo"
RUN_ID = 4242


async def _tracked_run() -> AsyncWorkflowRun:
    repo = AsyncRepository(github_repo_id=99, full_name=REPO, owner="acme", name="demo")
    await repo.save()
    workflow = AsyncWorkflow(
        repository_id=repo.id or 0,
        workflow_id=7,
        name="CI",
        path=".github/workflows/test.yml",
    )
    await workflow.save()
    run = AsyncWorkflowRun(
        workflow_id=workflow.id or 0,
        github_run_id=RUN_ID,
        run_number=1,
        run_attempt=1,
        status="in_progress",
        conclusion=None,
        branch="main",
        commit_sha="0" * 40,
        event="workflow_dispatch",
    )
    await run.save()
    return run


def _run_payload() -> dict:
    return {
        "action": "completed",
        "repository": {"id": 99, "full_name": REPO},
        "workflow_run": {
            "id": RUN_ID,
            "run_number": 1,
            "run_attempt": 1,
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": "0" * 40,
            "event": "workflow_dispatch",
        },
    }


class _FakeLogs:
    """A capture flow that answers with whatever it was handed."""

    def __init__(self, view: LogCaptureView) -> None:
        self._view = view
        self.calls = 0

    async def ensure(self, repository, run, *, job=None, want=None, retry=False):
        self.calls += 1
        return self._view


class _RaisingLogs:
    def __init__(self) -> None:
        self.calls = 0

    async def ensure(self, repository, run, *, job=None, want=None, retry=False):
        self.calls += 1
        raise RuntimeError("the archive endpoint is down")


def _note_shape(text: str) -> str:
    """The note's prefix, for assertions that do not want to pin the numbers."""
    return text.strip()


class TestTheNoteItself:
    def test_a_full_capture_says_stored(self) -> None:
        view = LogCaptureView(run_id=RUN_ID, steps_stored=12, missing_steps=[])
        assert _capture_note(view) == "capture stored=12 missing=0"

    def test_a_shortfall_says_both_numbers(self) -> None:
        view = LogCaptureView(run_id=RUN_ID, steps_stored=3, missing_steps=[4, 5])
        assert _capture_note(view) == "capture stored=3 missing=2"

    def test_a_capture_that_declined_names_the_reason(self) -> None:
        view = LogCaptureView(run_id=RUN_ID, skipped_reason="archive_unavailable")
        assert _capture_note(view) == "capture archive_unavailable"


class TestTheLedgerCarriesIt:
    async def test_a_completed_delivery_records_what_was_captured(self, bound) -> None:
        await _tracked_run()
        logs = _FakeLogs(LogCaptureView(run_id=RUN_ID, steps_stored=3, missing_steps=[4]))
        service = make_webhook_service(logs=logs)

        result = await service.process_webhook("workflow_run", _run_payload(), delivery_id="run-1")
        assert result["handled"] is True
        assert logs.calls == 1

        rows = await AsyncWebhookDelivery.query().all()
        assert len(rows) == 1
        row = rows[0]
        # The delivery's own verdict is unchanged, and the note rides behind it.
        assert row.reason == "processed: capture stored=3 missing=1"

    async def test_a_capture_that_failed_still_leaves_a_note(self, bound) -> None:
        """A failed capture is not a failed delivery -- but it is not nothing."""
        await _tracked_run()
        logs = _RaisingLogs()
        service = make_webhook_service(logs=logs)

        result = await service.process_webhook("workflow_run", _run_payload(), delivery_id="run-1")
        assert result["handled"] is True, "the delivery itself was applied"

        rows = await AsyncWebhookDelivery.query().all()
        assert rows[0].reason == "processed: capture failed: RuntimeError"

    async def test_a_job_delivery_carries_no_capture_note(self, bound) -> None:
        """The capture belongs to the run's completion, and only there."""
        await _tracked_run()
        logs = _FakeLogs(LogCaptureView(run_id=RUN_ID, steps_stored=9))
        service = make_webhook_service(logs=logs)

        payload = {
            "action": "completed",
            "repository": {"id": 99, "full_name": REPO},
            "workflow_job": {
                "id": 5001,
                "run_id": RUN_ID,
                "name": "tests",
                "status": "completed",
                "conclusion": "success",
                "steps": [],
            },
        }
        result = await service.process_webhook("workflow_job", payload, delivery_id="job-1")
        assert result["handled"] is True

        rows = await AsyncWebhookDelivery.query().all()
        assert rows[0].reason == "processed"
        assert logs.calls == 0

    async def test_the_note_stays_inside_the_ledger_column(self, bound) -> None:
        """The reason column is 64 characters; a long note must not overflow it.

        ``already_captured`` is the longest declined reason, so the bound is
        checked with it: the answer is truncated by the writer, never rejected.
        """
        await _tracked_run()
        logs = _FakeLogs(LogCaptureView(run_id=RUN_ID, skipped_reason="already_captured"))
        service = make_webhook_service(logs=logs)

        await service.process_webhook("workflow_run", _run_payload(), delivery_id="run-1")

        rows = await AsyncWebhookDelivery.query().all()
        assert len(rows[0].reason) <= 64
        assert rows[0].reason.startswith("processed")
