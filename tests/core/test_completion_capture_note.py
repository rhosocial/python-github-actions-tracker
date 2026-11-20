# tests/core/test_completion_capture_note.py
"""What a completed delivery records, and where the capture note went.

Capture is now a detached, bounded side effect: the webhook answers while the
archive download is still running. The delivery ledger therefore says only
that the delivery was processed -- it cannot carry a capture result it does not
have yet. The note still exists as a log line and as a view for a caller that
asks for logs; the numbers live in ``ci_status.health.capture``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

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


class _Source:
    """A connection source: the queue only needs it to know capture is possible."""

    @asynccontextmanager
    async def connection(self):
        yield None


class _FakeLogs:
    """A capture flow that answers with whatever it was handed."""

    def __init__(self, view: LogCaptureView) -> None:
        self._view = view
        self.calls = 0

    async def ensure(self, repository, run, *, job=None, want=None, retry=False, **kwargs):
        self.calls += 1
        return self._view


class _RaisingLogs:
    def __init__(self) -> None:
        self.calls = 0

    async def ensure(self, repository, run, *, job=None, want=None, retry=False, **kwargs):
        self.calls += 1
        raise RuntimeError("the archive endpoint is down")


async def _wait_for_calls(logs: object, expected: int) -> None:
    """The capture runs in a task; give the drain a bounded moment to pick it up."""
    for _ in range(100):
        if getattr(logs, "calls", 0) >= expected:
            return
        await asyncio.sleep(0.01)


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
    async def test_a_completed_delivery_records_only_itself(self, bound) -> None:
        """The ledger says 'processed'; the capture result is not available yet."""
        await _tracked_run()
        logs = _FakeLogs(LogCaptureView(run_id=RUN_ID, steps_stored=3, missing_steps=[4]))
        service = make_webhook_service(logs=logs, database=_Source())
        await service.start_captures()
        try:
            result = await service.process_webhook(
                "workflow_run", _run_payload(), delivery_id="run-1"
            )
            assert result["handled"] is True
            rows = await AsyncWebhookDelivery.query().all()
            assert len(rows) == 1
            assert rows[0].reason == "processed"

            await _wait_for_calls(logs, 1)
            assert logs.calls == 1
        finally:
            await service.drain_captures()

    async def test_a_failed_capture_does_not_fail_the_delivery(self, bound) -> None:
        """The failure is counted as a capture outcome, not written to the ledger."""
        await _tracked_run()
        logs = _RaisingLogs()
        service = make_webhook_service(logs=logs, database=_Source())
        await service.start_captures()
        try:
            result = await service.process_webhook(
                "workflow_run", _run_payload(), delivery_id="run-1"
            )
            assert result["handled"] is True

            await _wait_for_calls(logs, 1)
            rows = await AsyncWebhookDelivery.query().all()
            assert rows[0].reason == "processed"
            assert service.capture_health.outcomes.get("RuntimeError") == 1
        finally:
            await service.drain_captures()

    async def test_a_job_delivery_carries_no_capture_note(self, bound) -> None:
        """Job completions do not queue the run-level capture."""
        await _tracked_run()
        logs = _FakeLogs(LogCaptureView(run_id=RUN_ID, steps_stored=9))
        service = make_webhook_service(logs=logs, database=_Source())

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

    async def test_the_reason_stays_short(self, bound) -> None:
        """The ledger's reason column is 64 characters; no capture note is appended."""
        await _tracked_run()
        service = make_webhook_service(
            logs=_FakeLogs(LogCaptureView(run_id=RUN_ID)), database=_Source()
        )
        await service.process_webhook("workflow_run", _run_payload(), delivery_id="run-1")

        rows = await AsyncWebhookDelivery.query().all()
        assert len(rows[0].reason) <= 64
        assert rows[0].reason == "processed"
