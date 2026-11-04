"""Delete the junk run record produced by the old pagination bug.

The pagination defect stored GitHub's ``{"total_count": N, "workflow_runs": [...]}``
envelope as a run row, leaving one record with ``github_run_id = 0``. It has no
jobs attached and is safe to drop.
"""

import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "src"))

from rhosocial.activerecord.backend.impl.postgres.async_support import run_coroutine


async def main() -> None:
    from rhosocial.github_ci_tracker.infrastructure.database import (
        bind_backend,
        create_pool,
    )

    from rhosocial.github_ci_tracker.models import Job, JobLog, Step, WorkflowRun

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
