# src/rhosocial/github_ci_tracker/adapters/cli/commands/failures.py
"""rhosocial-github-ci-tracker failures command."""

from __future__ import annotations

import argparse
from typing import Any

from ..formatters import format_output


def _as_dict(view: Any) -> dict[str, Any]:
    """The grouping as plain data, so both output shapes read the same view."""

    def reference(item: Any) -> dict[str, Any]:
        return {
            "job_id": item.job_id,
            "job_name": item.job_name,
            "step": item.step,
            "step_name": item.step_name,
            "conclusion": item.conclusion,
        }

    return {
        "run_id": view.run_id,
        "jobs_failed": view.jobs_failed,
        "groups": [
            {
                "occurrences": len(group.occurrences),
                "steps": [reference(item) for item in group.occurrences],
                "content": group.content,
            }
            for group in view.groups
        ],
        "steps_without_logs": [reference(item) for item in view.steps_without_logs],
        "jobs_without_failed_steps": [
            {"job_id": job_id, "job_name": job_name}
            for job_id, job_name in view.jobs_without_failed_steps
        ],
    }


def _tail(text: str, lines: int) -> str:
    """The last ``lines`` lines: a failure is reported at the end of a step."""
    return "\n".join(text.splitlines()[-lines:])


async def run(args: argparse.Namespace, container: Any) -> None:
    """Group a run's failed-step logs by identical text.

    Args:
        args: Parsed command-line arguments
        container: The controllers this process built.
    """
    view = await container.query.get_run_failed_logs(args.repo, args.run_id)

    if getattr(args, "format", "text") != "text":
        print(format_output(_as_dict(view), args.format, args.verbose))
        return

    if view.jobs_failed == 0:
        print("No job of this run failed.")
        return

    for index, group in enumerate(view.groups, start=1):
        heading = "step" if len(group.occurrences) == 1 else "steps"
        print(f"===== group {index}: {len(group.occurrences)} {heading} =====")
        for item in group.occurrences:
            print(
                f"  {item.job_name} step {item.step}: {item.step_name} "
                f"({item.conclusion or 'no conclusion'})"
            )
        print(_tail(group.content, args.tail))

    if view.steps_without_logs:
        print()
        print("No captured log for:")
        for item in view.steps_without_logs:
            print(f"  {item.job_name} step {item.step}: {item.step_name}")
        print(
            "Step logs are fetched while GitHub still splits a run into per-step "
            "files (about an hour after it finishes)."
        )

    if view.jobs_without_failed_steps:
        print()
        print("Failed jobs with no failed step to read:")
        for job_id, job_name in view.jobs_without_failed_steps:
            print(f"  {job_name} (job {job_id})")

    if not view.groups:
        print(
            "The failed steps' logs were never captured: read a job's merged log "
            f"with `logs --repo {args.repo} --job-id <id>` instead."
        )
