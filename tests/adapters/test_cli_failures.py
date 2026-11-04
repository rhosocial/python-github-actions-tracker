# tests/adapters/test_cli_failures.py
"""The ``failures`` verb: one copy per distinct text, and the codes a script reads.

The grouping lives in the controller; this file pins the third surface - what a
human sees and what ``--format json`` publishes - because a command that only
imports is a command nobody has run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace

from rhosocial.github_ci_tracker.adapters.cli.commands import failures as failures_command
from rhosocial.github_ci_tracker.views import FailedStepRef, FailureGroup, RunFailuresView


def _args(*, format: str = "text", tail: int = 2) -> argparse.Namespace:
    return argparse.Namespace(
        repo="acme/demo",
        run_id=9,
        tail=tail,
        format=format,
        verbose=False,
    )


def _view() -> RunFailuresView:
    return RunFailuresView(
        run_id=9,
        jobs_failed=2,
        groups=[
            FailureGroup(
                content="setup noise\noldest detail\nmiddle line\nImportError: no module named x\n",
                occurrences=[
                    FailedStepRef(
                        job_id=1,
                        job_name="py3.10",
                        step=11,
                        step_name="Run tests",
                        conclusion="failure",
                    ),
                    FailedStepRef(
                        job_id=2,
                        job_name="py3.11",
                        step=11,
                        step_name="Run tests",
                        conclusion="failure",
                    ),
                ],
            )
        ],
    )


class _Query:
    def __init__(self, view: RunFailuresView) -> None:
        self._view = view
        self.calls: list[tuple[str, int]] = []

    async def get_run_failed_logs(self, repository, run_id):  # noqa: ANN001, ANN202
        self.calls.append((repository, run_id))
        return self._view


def test_text_output_names_every_job_and_shows_the_tail(capsys) -> None:
    query = _Query(_view())

    asyncio.run(failures_command.run(_args(), SimpleNamespace(query=query)))

    said = capsys.readouterr().out
    assert query.calls == [("acme/demo", 9)]
    assert "group 1: 2 steps" in said
    assert "py3.10 step 11: Run tests (failure)" in said
    assert "py3.11 step 11: Run tests (failure)" in said
    assert "ImportError" in said
    assert "oldest detail" not in said, "the tail, not the head"


def test_json_output_is_the_same_view_as_data(capsys) -> None:
    query = _Query(_view())

    asyncio.run(failures_command.run(_args(format="json"), SimpleNamespace(query=query)))

    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == 9
    assert payload["groups"][0]["occurrences"] == 2
    assert [step["job_name"] for step in payload["groups"][0]["steps"]] == [
        "py3.10",
        "py3.11",
    ]


def test_an_uncaptured_failure_says_where_to_look(capsys) -> None:
    view = RunFailuresView(
        run_id=9,
        jobs_failed=1,
        steps_without_logs=[
            FailedStepRef(job_id=3, job_name="docs", step=2, step_name="Build docs")
        ],
    )

    asyncio.run(failures_command.run(_args(), SimpleNamespace(query=_Query(view))))

    said = capsys.readouterr().out
    assert "No captured log for:" in said
    assert "docs step 2: Build docs" in said
    assert "logs --repo acme/demo --job-id <id>" in said


def test_a_green_run_says_so(capsys) -> None:
    view = RunFailuresView(run_id=9, jobs_failed=0)

    asyncio.run(failures_command.run(_args(), SimpleNamespace(query=_Query(view))))

    assert "No job of this run failed." in capsys.readouterr().out
