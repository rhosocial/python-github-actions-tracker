"""Delete the junk run record produced by the old pagination bug.

The pagination defect stored GitHub's ``{"total_count": N, "workflow_runs": [...]}``
envelope as a run row, leaving one record with ``github_run_id = 0``. It has no
jobs attached and is safe to drop.
"""

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "src"))

from rhosocial.activerecord.backend.impl.postgres.async_support import run_coroutine


async def main() -> None:
    # Imported inside main on purpose: this script runs outside the package (it
    # inserts src/ into sys.path above), so these are only importable once that
    # has happened. isort is told to leave the block here rather than at the top
    # of the file, where it would fail on a clean checkout.
    from rhosocial.github_actions_tracker.infrastructure.database import (  # isort: skip
        bind_backend,
        create_pool,
    )
    from rhosocial.github_actions_tracker.models import (
        Job,
        JobLog,
        Step,
        WorkflowRun,
    )  # isort: skip

    pool = await create_pool()
    try:
        async with bind_backend(pool):
            junk = await WorkflowRun.query().where(WorkflowRun.c.github_run_id == 0).all()
            if not junk:
                print("No junk record found (already clean).")
                return

            for run in junk:
                jobs = await Job.query().where(Job.c.run_id == run.id).all()
                for job in jobs:
                    await Step.query().where(Step.c.job_id == job.id).delete()
                    await JobLog.query().where(JobLog.c.job_id == job.id).delete()
                    await job.delete()
                await run.delete()
                print(f"Deleted junk run record id={run.id} (github_run_id=0).")
    finally:
        await pool.close()


if __name__ == "__main__":
    run_coroutine(main())
