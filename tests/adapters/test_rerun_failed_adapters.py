# tests/adapters/test_rerun_failed_adapters.py
"""``failed_only`` crosses every protocol with the same name and meaning.

The plan requires one parameter, one name, three adapters. What kills you later is
not the controller -- it is the day an adapter renames the flag, or drops it, and
a caller's ``failed_only=true`` silently becomes a whole-run re-run that spends the
CI minutes it asked not to spend. These tests pin the three surfaces together.

The controller's own semantics (preconditions checked before the request) are
pinned in :mod:`tests.controllers.test_rerun_failed_jobs`.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from rhosocial.github_actions_tracker.views import RunView


class _Calls:
    """Records the one keyword the test cares about."""

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}


def _tool_container(monkeypatch, calls: _Calls, *, result: RunView) -> Any:
    """Register the trigger tools against a container whose trigger records calls."""
    from types import SimpleNamespace

    from rhosocial.github_actions_tracker.adapters.mcp.tools import trigger as trigger_tools

    class _Trigger:
        async def rerun_workflow(
            self, repository, run_id, failed_only=False
        ):  # noqa: ANN001, ANN202
            calls.seen["failed_only"] = failed_only
            return result

    class _Tokens:
        async def list_tokens(self, limit=100, offset=0):  # noqa: ANN001, ANN202
            return [{"is_active": True}]

    def _register(recorder, container) -> None:
        trigger_tools.register_trigger_tools(recorder, container)

    recorder = _Recorder()
    _register(recorder, SimpleNamespace(token=_Tokens(), trigger=_Trigger()))
    return recorder.registered


class _Recorder:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def tool(self, *, name: str, **_: object):
        def decorate(function):
            self.registered[name] = function
            return function

        return decorate


RERAN = RunView(
    run_id=90_001,
    status="queued",
    attempt=2,
    previous_attempt=1,
    reran_jobs=[("build", "failure")],
)


class TestTheMcpTool:
    def test_the_parameter_exists_and_defaults_to_the_whole_run(self, monkeypatch) -> None:
        calls = _Calls()
        tools = _tool_container(monkeypatch, calls, result=RERAN)
        signature = inspect.signature(tools["rerun_run"])
        assert signature.parameters["failed_only"].default is False

    def test_a_true_is_passed_through_and_answered(self, monkeypatch) -> None:
        calls = _Calls()
        tools = _tool_container(monkeypatch, calls, result=RERAN)

        payload = asyncio.run(
            tools["rerun_run"](repository="acme/demo", run_id=90_001, failed_only=True)
        )

        assert calls.seen["failed_only"] is True
        assert payload["reran_jobs"] == [{"name": "build", "conclusion": "failure"}]

    def test_an_omitted_flag_stays_false(self, monkeypatch) -> None:
        """The default is the *whole* run: a caller who never heard of the
        selective path must not silently land on it."""
        calls = _Calls()
        tools = _tool_container(monkeypatch, calls, result=RERAN)

        asyncio.run(tools["rerun_run"](repository="acme/demo", run_id=90_001))

        assert calls.seen["failed_only"] is False

    def test_a_whole_run_reports_an_empty_list(self, monkeypatch) -> None:
        """Empty is the whole-run answer, not a claim that nothing ran -- and the
        field is only ever filled by the selective request."""
        calls = _Calls()
        tools = _tool_container(
            monkeypatch, calls, result=RunView(run_id=90_001, status="queued", attempt=2)
        )

        payload = asyncio.run(
            tools["rerun_run"](repository="acme/demo", run_id=90_001, failed_only=False)
        )

        assert payload["reran_jobs"] == []


class TestTheRestRoute:
    def test_the_query_parameter_exists_and_defaults_to_false(self) -> None:
        """FastAPI wraps a query default in ``Query``; the value inside it is
        what matters, because that is what a caller who omits the parameter gets.
        """
        from rhosocial.github_actions_tracker.adapters.rest.routes import runs as runs_routes

        route = next(
            item for item in runs_routes.router.routes if item.path == "/runs/{run_id}/rerun"
        )
        default = inspect.signature(route.endpoint).parameters["failed_only"].default
        # FastAPI wraps a query default in a ``Query`` marker; the value inside
        # it is what a caller who omits the parameter actually gets.
        assert type(default).__name__ == "Query"
        assert default.default is False

    def test_the_response_carries_the_selection(self) -> None:
        from rhosocial.github_actions_tracker.adapters.rest.schemas.responses import (
            RerunRunResponse,
        )

        payload = RerunRunResponse(
            run_id=90_001,
            repository="acme/demo",
            status="queued",
            message="x",
            attempt=2,
            previous_attempt=1,
            advanced=True,
            reran_jobs=[{"name": "build", "conclusion": "failure"}],
        )
        assert payload.reran_jobs[0].conclusion == "failure"
        assert payload.advanced is True


class TestTheCli:
    def test_the_flag_exists_on_the_command(self) -> None:
        """``--failed-only`` is the CLI's spelling of the same parameter; the
        default is the whole run, exactly as on the other two protocols."""
        from rhosocial.github_actions_tracker.adapters.cli.main import build_parser

        parser = build_parser()
        asked = parser.parse_args(
            ["rerun", "--repo", "acme/demo", "--run-id", "90001", "--failed-only"]
        )
        omitted = parser.parse_args(["rerun", "-r", "acme/demo", "--run-id", "90001"])

        assert asked.failed_only is True
        assert omitted.failed_only is False
        assert omitted.command == "rerun"
