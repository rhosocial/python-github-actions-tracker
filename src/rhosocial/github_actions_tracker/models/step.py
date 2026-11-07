# src/rhosocial/github_actions_tracker/models/step.py
"""Workflow job step model, sync and async twins.

A step is one phase of a job's execution as GitHub reports it in the job
payload (``number``, ``name``, ``status``, ``conclusion`` and the two
timestamps). GitHub does not give steps a stable id of their own, so the
``github_step_id`` column mirrors the step ``number`` -- the same convention
the schema's ``UNIQUE(job_id, github_step_id)`` constraint is built on.

Steps are stored exclusively through :meth:`Step.record`, keyed by
``(job_id, number)``; the controller calls it for every entry of a job
payload's ``steps`` array.

The twins are behavior-identical; the sync one exists for the project's
sync/async parity rule. There are no cross-model references here -- the
methods scope themselves by the job they are handed -- so this module needs no
lazy imports beyond the state machine used by the write path.
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
from .validators import IdentityMixin, UtcTimestampMixin

if TYPE_CHECKING:
    from .job import AsyncJob, Job
    from .log import AsyncStepLog, StepLog

logger = logging.getLogger(__name__)


class Step(
    FindOrCreateMixin, IdentityMixin, UtcTimestampMixin, DefaultTimestampMixin, ActiveRecord
):
    """One step of a job, synchronous twin."""

    __table_name__ = "ghat_steps"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: Local job this step belongs to.
    job_id: int = 0
    #: Mirrors the step ``number``: GitHub step payloads carry no separate
    #: id, and the schema's ``UNIQUE(job_id, github_step_id)`` constraint is
    #: aligned with the ``(job_id, number)`` upsert key.
    github_step_id: int = 0
    name: str = ""
    status: str = "queued"
    conclusion: str | None = None
    number: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None

    #: Owning job (pairs with :attr:`Job.steps`).
    job: ClassVar[BelongsTo[Job]] = BelongsTo(foreign_key="job_id", inverse_of="steps")

    #: What this step printed, when it was captured. Optional: GitHub keeps the
    #: per-step archive for about an hour after the run finishes, so an unread
    #: run has no row here and cannot get one later -- absence is the answer,
    #: not a failure.
    step_log: ClassVar[HasOne[StepLog]] = HasOne(foreign_key="step_id", inverse_of="step")

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_job(cls, job_id: int) -> ActiveQuery:
        """Query builder over the steps of one job, number ascending.

        The builder is not terminated; callers may add further clauses
        before calling ``all()`` / ``one()`` / ``count()``.
        """
        return cls.query().where(cls.c.job_id == job_id).order_by((cls.c.number, "ASC"))

    # ---- behavior -------------------------------------------------------------

    @classmethod
    def record(
        cls,
        job: Job,
        *,
        number: int,
        name: str | None = None,
        status: str | None = None,
        conclusion: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> Step:
        """Store or refresh one step of ``job`` from semantic values.

        The key is ``(job_id, number)``. ``github_step_id`` mirrors ``number``,
        which is the convention the schema's unique constraint is built on.
        A stored step moves through the forward-only state machine; an unseen
        one is inserted.

        Args:
            job: The persisted job the step belongs to -- the **direct** parent.
            number: The step number inside that job.
            name: The step's display name.
            status: Canonical status; ``None`` leaves the stored one alone.
            conclusion: Canonical conclusion, when the step finished.
            started_at: When the step started.
            completed_at: When it ended.

        Returns:
            The persisted :class:`Step` record.
        """
        from .errors import InvalidStateTransitionError
        from .job import Job
        from .states import KIND_STEP, StateMachine

        if not isinstance(job, Job) or job.id is None:
            raise ValueError(
                "Step.record needs a stored job; the controller stores the parent "
                "before handing it over"
            )

        values = {
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        defaults = {field: value for field, value in values.items() if value is not None}
        defaults["github_step_id"] = number
        step, created = cls.find_one_or_create(defaults=defaults, job_id=job.id, number=number)
        if created:
            logger.debug("Stored step %s ('%s') for job %s", number, name, job.id)
            return step

        if status is not None:
            try:
                StateMachine.apply_transition(step, status, conclusion, KIND_STEP)
            except InvalidStateTransitionError as exc:
                logger.warning("Ignoring out-of-order status for step %s: %s", number, exc)
        if name:
            step.name = name
        if started_at is not None:
            step.started_at = started_at
        if completed_at is not None:
            step.completed_at = completed_at
        if step.is_dirty:
            step.save()
        return step


class AsyncStep(
    AsyncFindOrCreateMixin,
    IdentityMixin,
    UtcTimestampMixin,
    DefaultTimestampMixin,
    AsyncActiveRecord,
):
    """One step of a job, asynchronous twin.

    Behavior-identical to :class:`Step`; every method performs the same
    database operations and raises the same exceptions.
    """

    __table_name__ = "ghat_steps"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: Local job this step belongs to.
    job_id: int = 0
    #: Mirrors the step ``number``: GitHub step payloads carry no separate
    #: id, and the schema's ``UNIQUE(job_id, github_step_id)`` constraint is
    #: aligned with the ``(job_id, number)`` upsert key.
    github_step_id: int = 0
    name: str = ""
    status: str = "queued"
    conclusion: str | None = None
    number: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None

    #: Owning job (pairs with :attr:`AsyncJob.steps`).
    job: ClassVar[AsyncBelongsTo[AsyncJob]] = AsyncBelongsTo(
        foreign_key="job_id", inverse_of="steps"
    )

    #: What this step printed, when it was captured (see the sync twin).
    step_log: ClassVar[AsyncHasOne[AsyncStepLog]] = AsyncHasOne(
        foreign_key="step_id", inverse_of="step"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_job(cls, job_id: int) -> AsyncActiveQuery:
        """Query builder over the steps of one job, number ascending.

        See :meth:`Step.for_job`; the builder is not terminated.
        """
        return cls.query().where(cls.c.job_id == job_id).order_by((cls.c.number, "ASC"))

    # ---- behavior -------------------------------------------------------------

    @classmethod
    async def record(
        cls,
        job: AsyncJob,
        *,
        number: int,
        name: str | None = None,
        status: str | None = None,
        conclusion: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> AsyncStep:
        """Store or refresh one step of ``job``; see :meth:`Step.record`."""
        from .errors import InvalidStateTransitionError
        from .job import AsyncJob
        from .states import KIND_STEP, StateMachine

        if not isinstance(job, AsyncJob) or job.id is None:
            raise ValueError(
                "Step.record needs a stored job; the controller stores the parent "
                "before handing it over"
            )

        values = {
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        defaults = {field: value for field, value in values.items() if value is not None}
        defaults["github_step_id"] = number
        step, created = await cls.find_one_or_create(
            defaults=defaults, job_id=job.id, number=number
        )
        if created:
            logger.debug("Stored step %s ('%s') for job %s", number, name, job.id)
            return step

        if status is not None:
            try:
                StateMachine.apply_transition(step, status, conclusion, KIND_STEP)
            except InvalidStateTransitionError as exc:
                logger.warning("Ignoring out-of-order status for step %s: %s", number, exc)
        if name:
            step.name = name
        if started_at is not None:
            step.started_at = started_at
        if completed_at is not None:
            step.completed_at = completed_at
        if step.is_dirty:
            await step.save()
        return step
