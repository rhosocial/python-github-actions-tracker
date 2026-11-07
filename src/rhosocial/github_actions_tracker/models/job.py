# src/rhosocial/github_actions_tracker/models/job.py
"""Workflow run job model, sync and async twins.

A job is one unit of work of a workflow run, mirrored locally from the
GitHub jobs payload. The behavior here is the write path: :meth:`Job.record`
is keyed by ``(attempt_id, github_job_id)`` and applies the forward-only state
machine. It takes semantic values only -- the controller translates a GitHub
payload and cascades its ``steps`` array through :meth:`Step.record
<rhosocial.github_actions_tracker.models.step.Step.record>`.

Nothing here talks to GitHub: reading a log is orchestration (decide whether
the stored copy is enough, fetch it otherwise, store it), so it lives in
:meth:`QueryController.get_job_logs
<rhosocial.github_actions_tracker.controllers.query.QueryController.get_job_logs>`.
``ghat_job_logs.job_id`` is UNIQUE, so a job has at most one log and a purged log
leaves the job untouched -- see :mod:`.log`.

The twins are behavior-identical; the sync one exists for the project's
sync/async parity rule. References that would make the import graph cyclic
-- the run, step, log and repository models plus the ``core`` package
(whose ``__init__`` imports the event handlers, which import the models) --
are imported lazily inside method bodies; the type annotations use
``TYPE_CHECKING`` imports instead.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.query import ActiveQuery, AsyncActiveQuery
from rhosocial.activerecord.relation.async_descriptors import AsyncBelongsTo, AsyncHasOne
from rhosocial.activerecord.relation.descriptors import BelongsTo, HasOne

from .mixins import AsyncFindOrCreateMixin, FindOrCreateMixin
from .relations import AsyncOrderedHasMany, OrderedHasMany
from .validators import IdentityMixin, UtcTimestampMixin

if TYPE_CHECKING:
    from .attempt import AsyncRunAttempt, RunAttempt
    from .log import AsyncJobLog, JobLog
    from .step import AsyncStep, Step

logger = logging.getLogger(__name__)


class Job(FindOrCreateMixin, IdentityMixin, UtcTimestampMixin, DefaultTimestampMixin, ActiveRecord):
    """A job within a workflow run, synchronous twin."""

    __table_name__ = "ghat_jobs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: GitHub job id. Globally unique -- a re-run issues *new* job ids rather
    #: than reusing the old ones -- so this alone determines the row.
    github_job_id: int = 0
    #: Which attempt of the run this job belongs to. Not a ``run_id``: an
    #: attempt determines exactly one run, so carrying it here would make
    #: ``attempt_id -> run_id`` a functional dependency whose determinant is not
    #: a superkey -- a BCNF violation, and one that leaves the two foreign keys
    #: free to disagree. See :mod:`rhosocial.github_actions_tracker.schema.model`.
    attempt_id: int = 0
    name: str = ""
    status: str = "queued"
    conclusion: str | None = None
    runner_name: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    #: Owning attempt (pairs with :attr:`RunAttempt.jobs`).
    attempt: ClassVar[BelongsTo[RunAttempt]] = BelongsTo(
        foreign_key="attempt_id", inverse_of="jobs"
    )
    #: This job's log, when one is stored. The optional side of the 1:1 is the
    #: absent row, so a purged log costs no UPDATE here (see :mod:`.log`).
    job_log: ClassVar[HasOne[JobLog]] = HasOne(foreign_key="job_id", inverse_of="job")

    @property
    def is_finished(self) -> bool:
        """Whether this job has reached a terminal state."""
        return self.status == "completed"

    @property
    def is_failed(self) -> bool:
        """Whether this job finished with a failure.

        A failed job is the one whose log is worth fetching eagerly, so the
        predicate belongs here rather than as a string comparison at each call
        site.
        """
        return self.status == "completed" and self.conclusion == "failure"

    #: Steps of this job, in step number order.
    steps: ClassVar[OrderedHasMany[Step]] = OrderedHasMany(
        foreign_key="job_id",
        inverse_of="job",
        order_key=lambda step: step.number,
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def find_by_github_id(cls, github_job_id: int) -> Job | None:
        """Return the stored record of a GitHub job id, or ``None``."""
        return cls.find_one(cls.c.github_job_id == github_job_id)

    @classmethod
    def for_attempt(cls, attempt_id: int) -> ActiveQuery:
        """Query builder over the jobs of one attempt, id ascending.

        The builder is not terminated; callers may add further clauses
        before calling ``all()`` / ``one()`` / ``count()``. To read the jobs of
        a whole run, go through the run's attempts --
        ``WorkflowRun.jobs_of_attempt`` does that for its latest attempt.
        """
        return cls.query().where(cls.c.attempt_id == attempt_id).order_by((cls.c.id, "ASC"))

    # ---- behavior -------------------------------------------------------------

    # ``steps`` is the relation descriptor declared above; call it as
    # ``job.steps()`` to get the stored steps in number order.

    @classmethod
    def record(
        cls,
        attempt: RunAttempt,
        *,
        github_job_id: int,
        name: str | None = None,
        status: str | None = None,
        conclusion: str | None = None,
        runner_name: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> Job:
        """Store or refresh one job of ``attempt`` from semantic values.

        The key is ``(attempt_id, github_job_id)``. A stored job moves through
        the forward-only state machine -- a stale or out-of-order value is
        logged and skipped, never fatal -- and is refreshed in place; an unseen
        job is inserted.

        Args:
            attempt: The persisted attempt the job belongs to -- the **direct**
                parent, not the run. The controller resolves which attempt a
                payload describes before handing it over.
            github_job_id: GitHub's job id, globally unique across attempts.
            name: The job's display name.
            status: Canonical status; ``None`` leaves the stored one alone.
            conclusion: Canonical conclusion, when the job finished.
            runner_name: The runner that executed it.
            started_at: When the job actually started.
            completed_at: When it ended.

        Returns:
            The persisted :class:`Job` record.
        """
        from .errors import InvalidStateTransitionError
        from .states import KIND_JOB, StateMachine

        # The foreign key is NOT NULL and this model has exactly one parent:
        # refusing a half-built attempt here is what keeps the cascade honest
        # instead of letting this method silently create a grandparent.
        if not isinstance(attempt, RunAttempt) or attempt.id is None:
            raise ValueError(
                "Job.record needs a stored attempt; the controller resolves which "
                "one and stores it before handing it over"
            )

        values = {
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "runner_name": runner_name,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        defaults = {field: value for field, value in values.items() if value is not None}
        defaults["github_job_id"] = github_job_id
        job, created = cls.find_one_or_create(
            defaults=defaults, attempt_id=attempt.id, github_job_id=github_job_id
        )
        if created:
            return job

        if status is not None:
            try:
                StateMachine.apply_transition(job, status, conclusion, KIND_JOB)
            except InvalidStateTransitionError as exc:
                logger.warning("Ignoring out-of-order status for job %s: %s", github_job_id, exc)
        if name:
            job.name = name
        if runner_name:
            job.runner_name = runner_name
        if started_at is not None:
            job.started_at = started_at
        if completed_at is not None:
            job.completed_at = completed_at
        if job.is_dirty:
            job.save()
        return job


class AsyncJob(
    AsyncFindOrCreateMixin,
    IdentityMixin,
    UtcTimestampMixin,
    DefaultTimestampMixin,
    AsyncActiveRecord,
):
    """A job within a workflow run, asynchronous twin.

    Behavior-identical to :class:`Job`; every method performs the same
    requests and raises the same exceptions.
    """

    __table_name__ = "ghat_jobs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: GitHub job id. Globally unique -- a re-run issues *new* job ids rather
    #: than reusing the old ones -- so this alone determines the row.
    github_job_id: int = 0
    #: Which attempt of the run this job belongs to. Not a ``run_id``: see
    #: :attr:`Job.attempt_id` for why carrying the run here would be a BCNF
    #: violation.
    attempt_id: int = 0
    name: str = ""
    status: str = "queued"
    conclusion: str | None = None
    runner_name: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    #: Owning attempt (pairs with :attr:`AsyncRunAttempt.jobs`).
    attempt: ClassVar[AsyncBelongsTo[AsyncRunAttempt]] = AsyncBelongsTo(
        foreign_key="attempt_id", inverse_of="jobs"
    )

    @property
    def is_finished(self) -> bool:
        """Whether this job has reached a terminal state.

        See :attr:`Job.is_finished`; the same predicate.
        """
        return self.status == "completed"

    @property
    def is_failed(self) -> bool:
        """Whether this job finished with a failure.

        See :attr:`Job.is_failed`; the same predicate, so the two paths cannot
        disagree about which jobs are worth a log fetch.
        """
        return self.status == "completed" and self.conclusion == "failure"

    #: This job's log, when one is stored. The optional side of the 1:1 is the
    #: absent row, so a purged log costs no UPDATE here (see :mod:`.log`).
    job_log: ClassVar[AsyncHasOne[AsyncJobLog]] = AsyncHasOne(
        foreign_key="job_id", inverse_of="job"
    )
    #: Steps of this job, in step number order.
    steps: ClassVar[AsyncOrderedHasMany[AsyncStep]] = AsyncOrderedHasMany(
        foreign_key="job_id",
        inverse_of="job",
        order_key=lambda step: step.number,
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    async def find_by_github_id(cls, github_job_id: int) -> AsyncJob | None:
        """Return the stored record of a GitHub job id, or ``None``."""
        return await cls.find_one(cls.c.github_job_id == github_job_id)

    @classmethod
    def for_attempt(cls, attempt_id: int) -> AsyncActiveQuery:
        """Query builder over the jobs of one attempt, id ascending.

        See :meth:`Job.for_attempt`; the builder is not terminated.
        """
        return cls.query().where(cls.c.attempt_id == attempt_id).order_by((cls.c.id, "ASC"))

    # ---- behavior -------------------------------------------------------------

    # ``steps`` is the relation descriptor declared above; call it as
    # ``job.steps()`` to get the stored steps in number order.

    @classmethod
    async def record(
        cls,
        attempt: AsyncRunAttempt,
        *,
        github_job_id: int,
        name: str | None = None,
        status: str | None = None,
        conclusion: str | None = None,
        runner_name: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> AsyncJob:
        """Store or refresh one job of ``attempt``; see :meth:`Job.record`."""
        from .errors import InvalidStateTransitionError
        from .states import KIND_JOB, StateMachine

        if not isinstance(attempt, AsyncRunAttempt) or attempt.id is None:
            raise ValueError(
                "Job.record needs a stored attempt: the controller resolves which "
                "one and stores it before handing it over"
            )

        values = {
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "runner_name": runner_name,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        defaults = {field: value for field, value in values.items() if value is not None}
        defaults["github_job_id"] = github_job_id
        job, created = await cls.find_one_or_create(
            defaults=defaults, attempt_id=attempt.id, github_job_id=github_job_id
        )
        if created:
            return job

        if status is not None:
            try:
                StateMachine.apply_transition(job, status, conclusion, KIND_JOB)
            except InvalidStateTransitionError as exc:
                logger.warning("Ignoring out-of-order status for job %s: %s", github_job_id, exc)
        if name:
            job.name = name
        if runner_name:
            job.runner_name = runner_name
        if started_at is not None:
            job.started_at = started_at
        if completed_at is not None:
            job.completed_at = completed_at
        if job.is_dirty:
            await job.save()
        return job
