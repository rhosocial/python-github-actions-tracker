# src/rhosocial/github_actions_tracker/models/attempt.py
"""Workflow run attempt model, sync and async twins.

A *run* is one GitHub Actions workflow run id; an *attempt* is one execution of
it. GitHub reuses the run id when a run is re-run and increments
``run_attempt``, so the run id alone never identifies one try -- a fact this
project once got the wrong way round. ``TriggerController.rerun`` inferred that a
re-run created a *new* run and went looking for an unfamiliar one, which
returned an unrelated run whenever anything else was pushed to the same branch.
This table exists so that question has a stored answer instead of a guess.

What an attempt carries that the run row does not: ``created_at``,
``run_started_at`` and ``updated_at`` are per attempt and they differ. Measured
on a public run with two attempts, where attempt 1 started at ``07:32:41Z`` and
the run row had already been overwritten with attempt 2's ``08:55:41Z``. Before
this table existed, storing the run overwrote the earlier execution's state, and
the only thing distinguishing the two was the ``ghat_jobs.run_attempt`` integer --
now a foreign key, ``ghat_jobs.attempt_id``.

The forward-only state machine is deliberately **not** applied here. An attempt
is history: a late ``in_progress`` payload for attempt 1 arriving after attempt
2 has started is the past arriving out of order, not a contradiction, and
refusing it would lose the only record of that execution.

The twins are behavior-identical; the sync one exists for the project's
sync/async parity rule. References that would make the import graph cyclic --
the run and job models plus the ``core`` package -- are imported lazily inside
method bodies; the type annotations use ``TYPE_CHECKING`` imports instead.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from rhosocial.activerecord.backend.expression.core import Subquery
from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.base.fields import DerivedField
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.query import ActiveQuery, AsyncActiveQuery
from rhosocial.activerecord.relation.async_descriptors import AsyncBelongsTo
from rhosocial.activerecord.relation.descriptors import BelongsTo

from .mixins import AsyncFindOrCreateMixin, FindOrCreateMixin
from .relations import AsyncOrderedHasMany, OrderedHasMany
from .validators import IdentityMixin, UtcTimestampMixin

if TYPE_CHECKING:
    from .job import AsyncJob, Job
    from .run import AsyncWorkflowRun, WorkflowRun

logger = logging.getLogger(__name__)


class _AttemptMerge:
    """The forward-only merge both attempt twins apply to a stored row."""

    @staticmethod
    def _merge(record: Any, values: dict[str, Any]) -> bool:
        """Apply semantic values to an attempt row, refusing to move backwards.

        ``status``/``conclusion`` go through the forward-only state machine;
        timestamps and metadata are filled in when supplied, and
        ``completed_at`` only while the execution is completed -- a late
        ``in_progress`` value must not clear it.
        """
        from .errors import InvalidStateTransitionError
        from .states import KIND_RUN, STATUS_COMPLETED, StateMachine

        before = (record.status, record.conclusion)
        changed = False
        status = values.get("status")
        if status is not None:
            try:
                changed = StateMachine.apply_transition(
                    record, status, values.get("conclusion"), KIND_RUN
                )
            except InvalidStateTransitionError as exc:
                logger.warning(
                    "Ignoring out-of-order status for attempt %s of run %s: %s",
                    record.attempt_number,
                    record.run_id,
                    exc,
                )

        # A same-status replay refreshes the conclusion without changing the
        # status, so the machine's return value alone does not say "saved".
        if (record.status, record.conclusion) != before:
            changed = True

        for name, value in values.items():
            if name in ("status", "conclusion") or value is None:
                continue
            if name == "completed_at" and record.status != STATUS_COMPLETED:
                continue
            if getattr(record, name) != value:
                setattr(record, name, value)
                changed = True
        return changed


#: Conclusions that mean "this job did its work". Used by the succeeded-span
#: derived field, which is a different question from "how long did this
#: execution last": a caller budgeting a wait for a *green* workflow wants the
#: span of the jobs that passed, not the span including the one that died early.
#: ``skipped`` is deliberately absent -- a skipped job has no timestamps at all,
#: so the ``IS NOT NULL`` filter already excludes it.
_PASSING_CONCLUSIONS_DEFAULT: frozenset[str] = frozenset({"success", "neutral"})


class _AttemptTimingFields:
    """The timing fields of one execution, computed by the database.

    An attempt's own ``started_at`` / ``completed_at`` come from the run payload,
    which describes the attempt as GitHub saw it -- a window that **includes the
    queue**: a run that waited ten minutes for a runner reports a ten-minute
    window even though nothing ran. The jobs carry when work *actually* started
    and ended, so the real duration of an execution is a fact about its jobs.

    Three numbers, because they answer three different questions:

    * ``job_span_seconds`` -- from the first job that started to the last job
      that ended. This is the duration a caller should budget a wait from, and
      the one ``duration_sample`` uses. A job that died after twenty seconds
      belongs in it: that is a real ending, not noise.
    * ``queue_seconds`` -- how long this attempt waited before its first job
      started. Reported next to the span, because "this workflow is slow" and
      "the runners were busy" call for different responses.
    * ``succeeded_job_span_seconds`` -- the same span over the jobs that
      actually passed. A separate number rather than a filter on the first one,
      so a caller comparing budgets across repositories can pick the口径 it
      means.

    All three are ``None`` until there is something to measure: an attempt whose
    jobs have not all reported times has no span, and saying so beats inventing
    one. There is deliberately no in-memory equivalent: the write path has the
    attempt row, not every job's timestamps, so a fresh instance carries
    ``None`` here until it is read back with ``derived=True``.
    """

    job_span_seconds: ClassVar[DerivedField] = DerivedField(
        lambda d: _AttemptDerived.job_span_seconds(d, "RunAttempt", ran_only=False)
    )
    queue_seconds: ClassVar[DerivedField] = DerivedField(
        lambda d: _AttemptDerived.queue_seconds(d, "RunAttempt")
    )
    succeeded_job_span_seconds: ClassVar[DerivedField] = DerivedField(
        lambda d: _AttemptDerived.job_span_seconds(d, "RunAttempt", ran_only=True)
    )


class _AsyncAttemptTimingFields:
    """The timing fields of the asynchronous twin; see :class:`_AttemptTimingFields`."""

    job_span_seconds: ClassVar[DerivedField] = DerivedField(
        lambda d: _AttemptDerived.job_span_seconds(d, "AsyncRunAttempt", ran_only=False)
    )
    queue_seconds: ClassVar[DerivedField] = DerivedField(
        lambda d: _AttemptDerived.queue_seconds(d, "AsyncRunAttempt")
    )
    succeeded_job_span_seconds: ClassVar[DerivedField] = DerivedField(
        lambda d: _AttemptDerived.job_span_seconds(d, "AsyncRunAttempt", ran_only=True)
    )


class _AttemptDerived:
    """Derived-field factories the model class body cannot name directly.

    ``DerivedField`` is discovered by ``typing.get_type_hints`` while the class
    is still being defined, so a lambda in the class body must not reference the
    class itself. These helpers resolve the model through the live module at
    query time instead.
    """

    @staticmethod
    def _seconds(dialect: Any, spread: Any) -> Any:
        """Turn a timestamp difference into seconds using expression nodes only.

        The library's ``EXTRACT`` speaks calendar fields (``year``, ...,
        ``second``) and has no ``EPOCH``. A PostgreSQL ``timestamp - timestamp``
        is an interval of days and time -- never months -- so its total length is
        exactly the sum of those components. Every component here is an ORM
        ``ExtractExpression`` and every multiplication an ORM
        ``ArithmeticOperation``; nothing is spelled as SQL text.
        """
        from rhosocial.activerecord.backend.expression.functions.datetime import extract

        return (
            extract(dialect, "day", spread) * 86_400
            + extract(dialect, "hour", spread) * 3_600
            + extract(dialect, "minute", spread) * 60
            + extract(dialect, "second", spread)
        )

    @staticmethod
    def queue_seconds(dialect: Any, model_name: str) -> Any:
        """How long this attempt waited before it started running.

        ``run_started_at`` against ``created_at`` -- both columns of this row, so
        no subquery is needed. ``None`` while either is missing, which is the
        honest answer for an attempt GitHub has not reported a start for.
        """
        from rhosocial.activerecord.backend.expression.operators import ArithmeticOperation

        model = getattr(sys.modules[__name__], model_name)
        spread = ArithmeticOperation(dialect, "sub", model.c.started_at, model.c.github_created_at)
        return _AttemptDerived._seconds(dialect, spread)

    @staticmethod
    def job_span_seconds(dialect: Any, model_name: str, *, ran_only: bool) -> Any:
        """The span of this attempt's jobs, as one correlated subquery.

        ``ran_only`` narrows it to the jobs that passed, which is a different
        question from "how long did this execution last" -- see
        :class:`_AttemptTimingFields`.

        The aggregate and the filter are expression nodes built through the
        same query builder the rest of the model uses; the scalar subquery
        carries its bind parameters with it instead of concatenating text.
        """
        from rhosocial.activerecord.backend.expression.functions.aggregate import max_, min_
        from rhosocial.activerecord.backend.expression.operators import ArithmeticOperation

        # The twin that matches this side: the async attempt is bound to the
        # async job model, and a derived field is built from the query builder of
        # the model whose backend will render it.
        from . import job as job_module

        model = getattr(sys.modules[__name__], model_name)
        job = job_module.AsyncJob if model_name.startswith("Async") else job_module.Job

        spread = ArithmeticOperation(
            dialect,
            "sub",
            max_(dialect, job.c.completed_at),
            min_(dialect, job.c.started_at),
        )
        inner = (
            job.query()
            .select(_AttemptDerived._seconds(dialect, spread))
            .where(job.c.attempt_id == model.c.id)
            .where(job.c.started_at.is_not_null())
            .where(job.c.completed_at.is_not_null())
        )
        if ran_only:
            inner = inner.where(job.c.conclusion.in_(sorted(_PASSING_CONCLUSIONS_DEFAULT)))
        return Subquery(dialect, inner.to_sql())


class RunAttempt(
    FindOrCreateMixin,
    _AttemptMerge,
    IdentityMixin,
    UtcTimestampMixin,
    _AttemptTimingFields,
    DefaultTimestampMixin,
    ActiveRecord,
):
    """One execution of a workflow run, synchronous twin."""

    __table_name__ = "ghat_run_attempts"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: Local run this attempt belongs to. The run id is *reused* across
    #: attempts: only :attr:`attempt_number` tells the executions apart.
    run_id: int = 0
    attempt_number: int = 1
    status: str = "queued"
    conclusion: str | None = None
    #: Who triggered this attempt. A re-run can be started by someone other than
    #: the run's original author, so this is not the run's actor.
    actor: str | None = None
    #: GitHub's ``run_started_at`` for *this* attempt.
    started_at: datetime | None = None
    #: GitHub's ``completed_at`` for *this* attempt; ``None`` while it runs.
    completed_at: datetime | None = None
    #: GitHub's ``completed_at`` for *this* attempt; ``None`` while it runs.
    completed_at: datetime | None = None
    #: GitHub's ``created_at`` / ``updated_at`` for this attempt, prefixed so
    #: they cannot be mistaken for this row's own timestamps.
    github_created_at: datetime | None = None
    github_updated_at: datetime | None = None

    #: Owning run (pairs with :attr:`WorkflowRun.attempts`).
    run: ClassVar[BelongsTo[WorkflowRun]] = BelongsTo(foreign_key="run_id", inverse_of="attempts")
    #: Jobs of this attempt, in local id order.
    jobs: ClassVar[OrderedHasMany[Job]] = OrderedHasMany(
        foreign_key="attempt_id",
        inverse_of="attempt",
        order_key=lambda job: job.id,
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_run(cls, run_id: int) -> ActiveQuery:
        """Query builder over every stored attempt of one run, number ascending."""
        return cls.query().where(cls.c.run_id == run_id).order_by((cls.c.attempt_number, "ASC"))

    @classmethod
    def find_attempt(cls, run_id: int, attempt_number: int) -> RunAttempt | None:
        """Return one stored attempt by its natural key, or ``None``."""
        return cls.find_one((cls.c.run_id == run_id) & (cls.c.attempt_number == attempt_number))

    @classmethod
    def latest_for_run(cls, run_id: int) -> RunAttempt | None:
        """Return the highest stored attempt number for a run, or ``None``.

        The run row's own ``run_attempt`` says the same thing, but answering it
        from this table keeps "what do we know about this run's executions"
        in one place -- and it stays right when a re-run arrives before the run
        row is refreshed.

        The query is built here rather than by chaining onto :meth:`for_run`:
        that one orders ascending, and the ORM appends a second ``order_by``
        instead of replacing the first, so the descending key was decoration and
        the answer was always the *earliest* attempt.
        """
        return (
            cls.query()
            .where(cls.c.run_id == run_id)
            .order_by((cls.c.attempt_number, "DESC"))
            .limit(1)
            .one()
        )

    # ---- behavior -------------------------------------------------------------

    @property
    def is_finished(self) -> bool:
        """Whether this execution has reached a terminal state."""
        from .states import STATUS_COMPLETED

        return self.status == STATUS_COMPLETED

    @classmethod
    def ensure(cls, run: WorkflowRun, attempt_number: int) -> RunAttempt:
        """Return the attempt row for ``(run, attempt_number)``, creating it if absent.

        The foreign key on ``ghat_jobs`` is NOT NULL, so a job cannot be stored
        before its attempt exists. A ``workflow_job`` webhook fragment can carry
        a job whose attempt has never been stored -- the fragment names the
        attempt but says nothing about its status -- so the write path asks for
        the row rather than assuming it. A minimal row is created and the fuller
        values overwrite it later, via :meth:`record`.
        """
        # One operation rather than "read, then insert": the read and the insert
        # can interleave with another delivery of the same run, and the loser of
        # that race must end up with the winner's row, not an exception.
        record, created = cls.find_one_or_create(
            defaults={"status": "queued"},
            run_id=run.id or 0,
            attempt_number=attempt_number,
        )
        if created:
            logger.debug(
                "Created run %s attempt %s from a fragment",
                run.github_run_id,
                attempt_number,
            )
        return record

    @classmethod
    def record(
        cls,
        run: WorkflowRun,
        attempt_number: int,
        *,
        status: str | None = None,
        conclusion: str | None = None,
        actor: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        github_created_at: datetime | None = None,
        github_updated_at: datetime | None = None,
    ) -> RunAttempt:
        """Store or refresh one execution of ``run`` from semantic values.

        The key is ``(run_id, attempt_number)``. A stored attempt is merged in
        place through the forward-only state machine, so attempt 1's state
        survives a re-run instead of being overwritten by it.

        Args:
            run: The persisted run this attempt belongs to -- the **direct**
                parent. Which attempt number a payload describes is the
                controller's decision, because only it knows the payload.
            attempt_number: GitHub's ``run_attempt`` for this execution.
            status: Canonical status; ``None`` leaves the stored one alone.
            conclusion: Canonical conclusion, when the execution finished.
            actor: Who triggered this execution.
            started_at: GitHub's ``run_started_at`` for *this* attempt.
            completed_at: GitHub's ``completed_at`` for *this* attempt.
            github_created_at: GitHub's ``created_at``, the partition key.
            github_updated_at: GitHub's ``updated_at``.

        Returns:
            The persisted :class:`RunAttempt` record.
        """
        from .run import WorkflowRun

        if not isinstance(run, WorkflowRun) or run.id is None:
            raise ValueError(
                "RunAttempt.record needs a stored run; the controller stores the "
                "parent before handing it over"
            )

        values = {
            "status": status,
            "conclusion": conclusion,
            "actor": actor,
            "started_at": started_at,
            "completed_at": completed_at,
            "github_created_at": github_created_at,
            "github_updated_at": github_updated_at,
        }
        defaults = {name: value for name, value in values.items() if value is not None}
        record, created = cls.find_one_or_create(
            defaults=defaults, run_id=run.id, attempt_number=attempt_number
        )
        if created:
            return record
        if cls._merge(record, values):
            record.save()
        return record


class AsyncRunAttempt(
    AsyncFindOrCreateMixin,
    _AttemptMerge,
    IdentityMixin,
    UtcTimestampMixin,
    _AsyncAttemptTimingFields,
    DefaultTimestampMixin,
    AsyncActiveRecord,
):
    """One execution of a workflow run, asynchronous twin."""

    __table_name__ = "ghat_run_attempts"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    run_id: int = 0
    attempt_number: int = 1
    status: str = "queued"
    conclusion: str | None = None
    actor: str | None = None
    started_at: datetime | None = None
    #: GitHub's ``completed_at`` for *this* attempt; ``None`` while it runs.
    completed_at: datetime | None = None
    github_created_at: datetime | None = None
    github_updated_at: datetime | None = None

    #: Owning run (pairs with :attr:`AsyncWorkflowRun.attempts`).
    run: ClassVar[AsyncBelongsTo[AsyncWorkflowRun]] = AsyncBelongsTo(
        foreign_key="run_id", inverse_of="attempts"
    )
    #: Jobs of this attempt, in local id order.
    jobs: ClassVar[AsyncOrderedHasMany[AsyncJob]] = AsyncOrderedHasMany(
        foreign_key="attempt_id",
        inverse_of="attempt",
        order_key=lambda job: job.id,
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_run(cls, run_id: int) -> AsyncActiveQuery:
        """Query builder over every stored attempt of one run, number ascending."""
        return cls.query().where(cls.c.run_id == run_id).order_by((cls.c.attempt_number, "ASC"))

    @classmethod
    async def find_attempt(cls, run_id: int, attempt_number: int) -> AsyncRunAttempt | None:
        """Return one stored attempt by its natural key, or ``None``."""
        return await cls.find_one(
            (cls.c.run_id == run_id) & (cls.c.attempt_number == attempt_number)
        )

    @classmethod
    async def latest_for_run(cls, run_id: int) -> AsyncRunAttempt | None:
        """Return the highest stored attempt number for a run, or ``None``.

        See :meth:`RunAttempt.latest_for_run`: building the query here keeps the
        descending order as the only key, instead of appending it to
        :meth:`for_run`'s ascending one.
        """
        return await (
            cls.query()
            .where(cls.c.run_id == run_id)
            .order_by((cls.c.attempt_number, "DESC"))
            .limit(1)
            .one()
        )

    # ---- behavior -------------------------------------------------------------

    @property
    def is_finished(self) -> bool:
        """Whether this execution has reached a terminal state.

        See :attr:`RunAttempt.is_finished`; the same predicate.
        """
        from .states import STATUS_COMPLETED

        return self.status == STATUS_COMPLETED

    @classmethod
    async def ensure(cls, run: AsyncWorkflowRun, attempt_number: int) -> AsyncRunAttempt:
        """Return the attempt row for ``(run, attempt_number)``, creating it if absent.

        See :meth:`RunAttempt.ensure`; the async twin, same behaviour.
        """
        record, created = await cls.find_one_or_create(
            defaults={"status": "queued"},
            run_id=run.id or 0,
            attempt_number=attempt_number,
        )
        if created:
            logger.debug(
                "Created run %s attempt %s from a fragment",
                run.github_run_id,
                attempt_number,
            )
        return record

    @classmethod
    async def record(
        cls,
        run: AsyncWorkflowRun,
        attempt_number: int,
        *,
        status: str | None = None,
        conclusion: str | None = None,
        actor: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        github_created_at: datetime | None = None,
        github_updated_at: datetime | None = None,
    ) -> AsyncRunAttempt:
        """Store or refresh one execution of ``run``; see :meth:`RunAttempt.record`."""
        from .run import AsyncWorkflowRun

        if not isinstance(run, AsyncWorkflowRun) or run.id is None:
            raise ValueError(
                "RunAttempt.record needs a stored run; the controller stores the "
                "parent before handing it over"
            )

        values = {
            "status": status,
            "conclusion": conclusion,
            "actor": actor,
            "started_at": started_at,
            "completed_at": completed_at,
            "github_created_at": github_created_at,
            "github_updated_at": github_updated_at,
        }
        defaults = {name: value for name, value in values.items() if value is not None}
        record, created = await cls.find_one_or_create(
            defaults=defaults, run_id=run.id, attempt_number=attempt_number
        )
        if created:
            return record
        if cls._merge(record, values):
            await record.save()
        return record


__all__ = ["AsyncRunAttempt", "RunAttempt"]
