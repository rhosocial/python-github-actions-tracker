# src/rhosocial/github_ci_tracker/adapters/cli/commands/rerun.py
"""rhosocial-github-ci-tracker rerun command."""

from __future__ import annotations

import argparse
from typing import Any

from ..formatters import format_trigger_result


async def run(args: argparse.Namespace, container: Any) -> None:
    """Execute the rerun command.

    Args:
        args: Parsed command-line arguments.
        container: The controllers this process built.
    """
    service = container.trigger
    result = await service.rerun_workflow(
        repository=args.repo,
        run_id=args.run_id,
        failed_only=getattr(args, "failed_only", False),
    )

    output = format_trigger_result(
        {
            "run_id": result.run_id,
            "status": result.status,
            "attempt": result.attempt,
            "message": result.message,
            "dispatched": True,
            "html_url": result.html_url,
            # Only filled by the selective request; empty on the whole-run one.
            "reran_jobs": [
                {"name": name, "conclusion": conclusion} for name, conclusion in result.reran_jobs
            ],
        },
        args.format,
    )
    print(output)

    # Wait for completion if requested
    if args.wait:
        status = await service.wait_for_completion(
            repository=args.repo,
            run_id=result.run_id,
            timeout=args.wait,
        )
        print(f"\nFinal status: {status.status} ({status.conclusion or 'N/A'})")
