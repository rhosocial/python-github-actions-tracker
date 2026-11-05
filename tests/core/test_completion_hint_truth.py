# tests/core/test_completion_hint_truth.py
"""The answer that announces a finished run must not promise a read that lies.

When GitHub says a run is finished, the per-step archive has only just begun to
exist: it stays fetchable for roughly an hour and then stops. A completion
answer that says "go read the failed step's log" without saying so walks the
caller into an empty read it cannot interpret -- the caller has no way to tell
"not yet" from "never", and concludes "this job printed nothing", which is the
one wrong answer in the whole flow.

Three properties are pinned, one per layer:

* ``wait_for_run``'s completion hint names the archive window before it names
  the call;
* the run-level roster carries the capture block, so "failed and 0/3 here" is
  visible in the same answer that announced completion;
* ``get_job``'s hint distinguishes a transient (``partial``/nothing yet, with a
  way to wait) from a closed window (nothing more will ever come).
"""

from __future__ import annotations

from rhosocial.github_actions_tracker.adapters.mcp.tools.query import _job_hint, _job_summary
from rhosocial.github_actions_tracker.views import CaptureStatus, JobRecord, StepRecord

WINDOW = "about an hour"


def _failed_job(capture: CaptureStatus | None) -> JobRecord:
    job = JobRecord(
        job_id=77,
        run_id=88,
        name="tests",
        status="completed",
        conclusion="failure",
        failed=True,
        capture=capture,
    )
    job.steps = [
        StepRecord(
            step_id=1,
            number=1,
            name="checkout",
            status="completed",
            conclusion="success",
        ),
        StepRecord(
            step_id=2,
            number=2,
            name="pytest",
            status="completed",
            conclusion="failure",
            failed=True,
        ),
    ]
    return job


class TestWaitForRunHint:
    def test_the_completion_hint_names_the_window_before_the_call(self) -> None:
        """The hint a failed run carries is the one the model acts on.

        The hint is built inside a closure in ``register_query_tools``, so it is
        read from that function's source: the property under test is the
        sentence, and a sentence is what an editor removes by accident. A
        behavioural test would need a whole fake GitHub run to reach one string.
        """
        import inspect

        from rhosocial.github_actions_tracker.adapters.mcp.tools import query as tool

        source = inspect.getsource(tool.register_query_tools)
        assert WINDOW in source, (
            "wait_for_run's completion hint no longer says how long GitHub keeps "
            "serving the per-step archive, so a caller reading a failed step "
            "immediately cannot tell an empty answer is a transient"
        )
        assert "refresh_logs=true" in source, (
            "the hint no longer names the explicit retry, leaving 'wait' as an "
            "instruction with no call attached"
        )
        assert "capture" in source, (
            "the hint no longer points at the capture block, which is where "
            "'are the files here yet' is actually answered"
        )


class TestRosterCarriesCapture:
    def test_a_finished_job_summarises_its_capture(self) -> None:
        job = _failed_job(
            CaptureStatus(expected_logs=3, captured_logs=0, state="none", window_closes_at=None)
        )
        summary = _job_summary(job)
        assert summary["capture"] == {
            "expected_logs": 3,
            "captured_logs": 0,
            "state": "none",
            "window_closes_at": None,
        }, (
            "the roster must answer 'are the step files there yet' in the same "
            "answer that announced the completion"
        )

    def test_a_running_job_carries_no_capture(self) -> None:
        """Nothing to capture yet, and the field would say so pointlessly."""
        job = JobRecord(job_id=77, run_id=88, name="tests", status="in_progress")
        job.steps = []
        summary = _job_summary(job)
        assert "capture" not in summary


class TestJobHintDistinguishesTransientFromGone:
    def test_nothing_captured_yet_offers_both_a_wait_and_a_read(self) -> None:
        job = _failed_job(
            CaptureStatus(expected_logs=3, captured_logs=0, state="none", window_closes_at=None)
        )
        hint = _job_hint(job, job.steps, {"expected_logs": 3, "captured_logs": 0, "state": "none"})
        assert WINDOW in hint
        assert "refresh_logs=true" in hint, "the transient must come with a way to wait"
        assert "get_job_log" in hint, "and a way to read what is readable now"

    def test_a_closed_window_says_nothing_more_will_come(self) -> None:
        job = _failed_job(
            CaptureStatus(expected_logs=3, captured_logs=1, state="closed", window_closes_at=None)
        )
        hint = _job_hint(
            job, job.steps, {"expected_logs": 3, "captured_logs": 1, "state": "closed"}
        )
        assert WINDOW in hint
        assert "cannot be fetched" in hint or "gone" in hint
        assert (
            "refresh_logs" not in hint
        ), "a closed window must not invite a retry that cannot succeed"

    def test_a_captured_failing_step_is_named_directly(self) -> None:
        job = _failed_job(
            CaptureStatus(expected_logs=3, captured_logs=3, state="complete", window_closes_at=None)
        )
        job.steps[1].has_log = True
        hint = _job_hint(
            job, job.steps, {"expected_logs": 3, "captured_logs": 3, "state": "complete"}
        )
        assert "step=2" in hint or "Step 2" in hint
        assert WINDOW not in hint, "a captured log needs no window story"
