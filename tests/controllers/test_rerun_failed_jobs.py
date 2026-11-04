# tests/controllers/test_rerun_failed_jobs.py
"""Re-running only what broke, without asking GitHub to refuse it.

``rerun-failed-jobs`` is a different GitHub endpoint with different
preconditions: the run must be finished, and its latest attempt must actually
hold a failed job. Both are checked *locally*, because GitHub answers 403 for
either one -- and a 403 reads like a permission problem the caller cannot act
on, when the real answer is "nothing to re-run".

What these tests pin:

* the two preconditions refuse *before* any GitHub call -- a fake control that
  records calls sees none;
* the selective request carries the failed jobs it selected, so the caller learns
  what broke rather than trusting a message;
* the whole-run path is unchanged, and says ``reran_jobs: []`` because that is
  the whole-run answer, not a claim that nothing ran.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from rhosocial.github_ci_tracker.controllers.dispatch import DispatchController
from rhosocial.github_ci_tracker.controllers.trigger import TriggerController
from rhosocial.github_ci_tracker.core.exceptions import ValidationError


class _Control:
    """The write verbs, recording what was called."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def dispatch(self, repo, workflow_id, ref, inputs=None):  # noqa: ANN001, ANN202
        self.calls.append(("dispatch", workflow_id))
        return {}

    async def rerun(self, repo_full_name, run_id):  # noqa: ANN001, ANN202
        self.calls.append(("rerun", run_id))

    async def rerun_failed(self, repo_full_name, run_id):  # noqa: ANN001, ANN202
        self.calls.append(("rerun_failed", run_id))

    async def cancel(self, repo_full_name, run_id):  # noqa: ANN001, ANN202
        self.calls.append(("cancel", run_id))


class _Readers:
    async def get_run(self, repo_full_name, run_id):  # noqa: ANN001, ANN202
        return {"id": run_id, "run_attempt": 2, "status": "completed"}


class _Run:
    """The stored run, as the flows read it."""

    def __init__(self, *, status: str = "completed", attempt: int = 2) -> None:
        self.github_run_id = 90_001
        self.latest_status = status
        self.latest_attempt = attempt
        self.latest_conclusion = "failure"
        self.workflow_id = 7


class _Store:
    """Run rows, plus the failed jobs the read paths answer from."""

    def __init__(self, run: _Run | None = None, failed: list[tuple[str, str]] | None = None):
        self.stored_run = run or _Run()
        self.failed = failed if failed is not None else [("build", "failure")]
        self.updated_with: Any = None

    async def repository(self, full_name):  # noqa: ANN001, ANN202
        return type("Repo", (), {"full_name": full_name, "id": 1})()

    async def run(self, github_run_id):  # noqa: ANN001, ANN202
        return self.stored_run

    async def workflow_of(self, run):  # noqa: ANN001, ANN202
        return type("Workflow", (), {"repository_id": 1})()

    async def latest_attempt(self, run):  # noqa: ANN001, ANN202
        return None

    async def jobs(self, run):  # noqa: ANN001, ANN202
        return []

    async def failed_jobs_by_run(self, run_ids):  # noqa: ANN001, ANN202
        return {run_id: list(self.failed) for run_id in run_ids}

    async def update(self, run, payload):  # noqa: ANN001, ANN202
        self.updated_with = payload
        return run


def _dispatch(store: _Store) -> DispatchController:
    """A dispatch controller wired to fakes, with the read-back wired to nothing."""
    controller = DispatchController(
        control=_Control(),
        readers=_Readers(),
        runs_data=store,
    )
    return controller


def _trigger(store: _Store, dispatch: DispatchController) -> TriggerController:
    runs = type("Runs", (), {"duration_sample": staticmethod(_no_sample)})()
    return TriggerController(dispatch=dispatch, runs=runs)


async def _no_sample(workflow_id):  # noqa: ANN001, ANN202
    return None


class TestThePreconditionsAreLocal:
    async def test_a_run_still_going_is_refused_before_the_request(self) -> None:
        store = _Store(_Run(status="in_progress"))
        controller = _dispatch(store)

        with pytest.raises(ValidationError) as caught:
            await controller.rerun_failed("acme/demo", 90_001)

        assert "not completed" in str(caught.value)
        assert controller._control.calls == [], "no GitHub call was made"

    async def test_a_run_with_no_failed_job_is_refused_before_the_request(self) -> None:
        store = _Store(failed=[])
        controller = _dispatch(store)

        with pytest.raises(ValidationError) as caught:
            await controller.rerun_failed("acme/demo", 90_001)

        assert "no failed job" in str(caught.value)
        assert controller._control.calls == [], "no GitHub call was made"


class TestTheSelectiveRequest:
    async def test_it_calls_the_failed_jobs_endpoint(self) -> None:
        store = _Store()
        controller = _dispatch(store)

        run, previous, failed = await controller.rerun_failed("acme/demo", 90_001)

        assert ("rerun_failed", 90_001) in controller._control.calls
        assert ("rerun", 90_001) not in controller._control.calls
        assert failed == [("build", "failure")]
        assert previous == 2

    async def test_the_view_carries_what_it_selected(self) -> None:
        store = _Store(failed=[("build", "failure"), ("test", "timed_out")])
        trigger = _trigger(store, _dispatch(store))

        view = await trigger.rerun_workflow("acme/demo", 90_001, failed_only=True)

        assert view.reran_jobs == [("build", "failure"), ("test", "timed_out")]
        # The message names what was selected; the conclusions ride along in the
        # field, which is where a caller that maps over them needs them.
        assert "build" in view.message and "test" in view.message

    async def test_the_whole_run_says_empty_rather_than_nothing_ran(self) -> None:
        """An empty list is the whole-run answer: the field only ever carries the
        selective request's selection, so nothing was claimed either way."""
        store = _Store()
        trigger = _trigger(store, _dispatch(store))

        view = await trigger.rerun_workflow("acme/demo", 90_001)

        assert view.reran_jobs == []
        assert view.previous_attempt == 2


class TestTheAdapterPassesItThrough:
    def test_the_tool_takes_the_flag_and_answers_with_it(self, monkeypatch) -> None:
        from rhosocial.github_ci_tracker.adapters.mcp.tools import trigger as trigger_tools
        from rhosocial.github_ci_tracker.views import RunView

        seen: dict[str, Any] = {}

        class _Trigger:
            async def rerun_workflow(
                self, repository, run_id, failed_only=False
            ):  # noqa: ANN001, ANN202
                seen["failed_only"] = failed_only
                return RunView(
                    run_id=90_001,
                    status="queued",
                    attempt=2,
                    previous_attempt=1,
                    reran_jobs=[("build", "failure")],
                )

        tools = _registered(
            trigger_tools,
            trigger_tools.register_trigger_tools,
            monkeypatch,
            result=None,
            trigger=_Trigger(),
        )
        payload = asyncio.run(
            tools["rerun_run"](repository="acme/demo", run_id=90_001, failed_only=True)
        )

        assert seen["failed_only"] is True
        assert payload["reran_jobs"] == [{"name": "build", "conclusion": "failure"}]


def _registered(tools_module, register, monkeypatch, *, result, trigger=None):
    """Register a tool group against a recorder, with a stubbed service."""
    from types import SimpleNamespace

    class _Tokens:
        async def list_tokens(self, limit=100, offset=0):  # noqa: ANN001, ANN202
            return [{"is_active": True}]

    container = SimpleNamespace(token=_Tokens(), trigger=trigger)

    class _Recorder:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, *, name: str, **_: object):
            def decorate(function):
                self.registered[name] = function
                return function

            return decorate

    recorder = _Recorder()
    register(recorder, container)
    return recorder.registered
