# src/rhosocial/github_actions_tracker/models/run.py
"""Workflow run model, sync and async twins.

A run is one GitHub Actions workflow execution, mirrored locally and updated
exclusively through the two passive channels: a GitHub webhook delivery or an
explicit pull. The pull channel never short-circuits on a terminal local state,
so it doubles as the reconciliation tool for a missed webhook, and it adopts runs
the system has never seen (the controller creates the run's workflow row first).

Neither channel is implemented here: reading a run from GitHub and deciding what
to do with it is service work (:meth:`QueryController.refresh_run
<rhosocial.github_actions_tracker.controllers.query.QueryController.refresh_run>`, and
:meth:`TriggerController.wait_for_completion
<rhosocial.github_actions_tracker.controllers.trigger.TriggerController.wait_for_completion>`
for
polling). The model takes **semantic values** it is handed
(:meth:`WorkflowRun.refresh_metadata`) and owns the write rules those values
imply; the controller translates the GitHub payload and resolves the attempt a
job hangs off.

The query domain deliberately exercises the three query builders of the ORM:
plain ``ActiveQuery`` chaining (:meth:`WorkflowRun.recent`,
:meth:`WorkflowRun.latest_by_branch`), a ``SetOperationQuery`` UNION ALL of
per-workflow top-k subqueries (:meth:`WorkflowRun.latest_per_workflow`) and a
``CTEQuery`` aggregate (:meth:`WorkflowRun.status_summary`).

The twins are behavior-identical; the sync one exists for the project's
sync/async parity rule. References that would make the import graph cyclic --
the repository, workflow, job, step models and the ``core`` package (whose
``__init__`` imports the event handlers, which import the models) -- are
imported lazily inside method bodies; the type annotations use
``TYPE_CHECKING`` imports instead.
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

if TYPE_CHECKING:
    from .attempt import AsyncRunAttempt, RunAttempt
    from .job import AsyncJob, Job
    from .workflow import AsyncWorkflow, Workflow

logger = logging.getLogger(__name__)


class _RunDerived:
    """Helpers the derived-field factories cannot name directly.

    ``DerivedField`` is discovered by ``typing.get_type_hints`` while the model
    class is still being defined, so a lambda in the class body must not name
    that class. It resolves one column of the run's newest attempt through the
    live module instead.
    """

    @staticmethod
    def latest_column(dialect: Any, model_name: str, attribute: str) -> Any:
        """One column of a run's newest attempt, resolved once the class exists."""
        model = getattr(sys.modules[__name__], model_name)
        return model._latest_column(dialect, attribute)


class _RunStateFields:
    """The derived fields of the synchronous run, declared where they resolve.

    ``DerivedField`` is discovered by ``typing.get_type_hints`` over each class
    in the MRO. The model's own annotations name related models (``RunAttempt``,
    ``Job``) that are imported under ``TYPE_CHECKING`` only, so resolving *its*
    hints fails and derived fields declared there would be invisible. This mixin
    names nothing model-specific, so it resolves cleanly.

    The factories are assigned in the class body, which runs with the module's
    globals; the class each one looks up is resolved at query time.
    """

    latest_attempt: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "WorkflowRun", "attempt_number")
    )
    latest_status: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "WorkflowRun", "status")
    )
    latest_conclusion: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "WorkflowRun", "conclusion")
    )
    latest_started_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "WorkflowRun", "started_at")
    )
    latest_github_created_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "WorkflowRun", "github_created_at")
    )
    latest_completed_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "WorkflowRun", "completed_at")
    )


class _AsyncRunStateFields:
    """The derived fields of the asynchronous run; see :class:`_RunStateFields`."""

    latest_attempt: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "AsyncWorkflowRun", "attempt_number")
    )
    latest_status: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "AsyncWorkflowRun", "status")
    )
    latest_conclusion: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "AsyncWorkflowRun", "conclusion")
    )
    latest_started_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "AsyncWorkflowRun", "started_at")
    )
    latest_github_created_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "AsyncWorkflowRun", "github_created_at")
    )
    latest_completed_at: ClassVar[DerivedField] = DerivedField(
        lambda d: _RunDerived.latest_column(d, "AsyncWorkflowRun", "completed_at")
    )


class WorkflowRun(FindOrCreateMixin, _RunStateFields, DefaultTimestampMixin, ActiveRecord):
    """A single workflow run, synchronous twin."""

    __table_name__ = "ghat_workflow_runs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    workflow_id: int = 0
    github_run_id: int = 0
    run_number: int = 0
    #: Cached pull request attribution. ``None`` never means "no PR": GitHub
    #: leaves ``pull_requests[]`` empty on fork PRs, so this is an accelerator
    #: only. Queries by PR resolve the head sha instead.
    pr_number: int | None = None
    event: str = ""
    branch: str = ""
    commit_sha: str = ""
    commit_message: str = ""
    #: GitHub's own one-line title for this run (commit subject, or ``title #n``).
    display_title: str = ""
    #: The repository the code came from; differs from the subscription on a fork.
    head_repo: str = ""
    #: The commit's timestamp, which is not when CI started.
    commit_time: datetime | None = None
    #: The commit author, which is not who triggered the run.
    commit_author: str = ""
    #: Target branch of the pull request, when the run has one.
    base_ref: str = ""
    actor: str = ""
    #: Last webhook delivery that touched this row.
    webhook_received_at: datetime | None = None

    #: Owning workflow (pairs with :attr:`Workflow.runs`).
    workflow: ClassVar[BelongsTo[Workflow]] = BelongsTo(
        foreign_key="workflow_id", inverse_of="runs"
    )
    #: Every stored execution of this run, in attempt-number order. A job no
    #: longer hangs off the run directly (``attempt_id -> run_id`` would be a
    #: BCNF violation), so reading the run's jobs goes through these.
    attempts: ClassVar[OrderedHasMany[RunAttempt]] = OrderedHasMany(
        foreign_key="run_id",
        inverse_of="run",
        order_key=lambda attempt: attempt.attempt_number,
    )

    # ---- derived: the state of the newest attempt ---------------------------
    #
    # A run row carries identity and metadata only. Its status, conclusion and
    # timestamps are facts about one *execution*, and those live in
    # ``ghat_run_attempts``. The fields below are computed by the database at
    # query time, so "the run's current state" and "its newest attempt's state"
    # cannot disagree: there is only one copy of the fact.
    #
    # They are loaded by ``find_one`` / ``find_all`` with ``derived=True``; a
    # query that does not ask for them leaves them ``None``.

    @classmethod
    def _latest_column(cls, dialect: Any, attribute: str) -> Any:
        """Correlated scalar subquery: one column of this run's newest attempt.

        ``ORDER BY attempt_number DESC LIMIT 1`` uses the
        ``(run_id, attempt_number)`` unique key, so each derived column is one
        index lookup per row.
        """
        column = getattr(RunAttempt.c, attribute)
        inner = (
            RunAttempt.query()
            .select(column)
            .where(RunAttempt.c.run_id == cls.c.id)
            .order_by((RunAttempt.c.attempt_number, "DESC"))
            .limit(1)
        )
        return Subquery(dialect, inner.to_sql())

    def _apply_attempt_state(self, attempt: RunAttempt | None) -> None:
        """Expose an attempt's state through the derived fields, in memory.

        A write path already has the attempt row in hand; re-reading the run
        through a ``derived`` query just to fill these attributes would
        cost one query per delivery. ``None`` leaves them unset.
        """
        if attempt is None:
            return
        self.__dict__["latest_attempt"] = attempt.attempt_number
        self.__dict__["latest_status"] = attempt.status
        self.__dict__["latest_conclusion"] = attempt.conclusion
        self.__dict__["latest_started_at"] = attempt.started_at
        self.__dict__["latest_completed_at"] = attempt.completed_at
        self.__dict__["latest_github_created_at"] = attempt.github_created_at

    # ---- query domain -------------------------------------------------------

    @classmethod
    def find_by_github_id(cls, github_run_id: int, *, derived: bool = False) -> WorkflowRun | None:
        """Return the stored record of a GitHub run id, or ``None``.

        ``derived`` loads ``latest_status`` and friends -- the state that lives
        on the newest attempt -- in the same query.
        """
        return cls.find_one(cls.c.github_run_id == github_run_id, derived=derived)

    @classmethod
    def recent(
        cls,
        workflow_ids: list[int],
        branch: str | None = None,
    ) -> ActiveQuery:
        """Query builder over the runs of ``workflow_ids``, newest first.

        There is no ``status`` filter: status belongs to an attempt, not to a
        run, so filtering by it would need the latest-attempt subquery in the
        ``WHERE`` clause -- a query the callers that need it can build from
        :meth:`RunAttempt.latest_for_run`.

        Args:
            workflow_ids: Local workflow ids to scope to; the caller must
                pass a non-empty list.
            branch: Optional branch filter.

        Returns:
            An :class:`ActiveQuery` ordered by ``run_number`` descending.
        """
        query = cls.query().where(cls.c.workflow_id.in_(workflow_ids))
        if branch:
            query = query.where(cls.c.branch == branch)
        return query.order_by((cls.c.run_number, "DESC"))

    @classmethod
    def latest_by_branch(cls, workflow_ids: list[int], branch: str) -> WorkflowRun | None:
        """Return the newest run of ``workflow_ids`` on ``branch``, or ``None``."""
        if not workflow_ids:
            return None
        return cls.recent(workflow_ids, branch=branch).one()

    @classmethod
    def latest_per_workflow(cls, workflow_ids: list[int], k: int = 1) -> list[WorkflowRun]:
        """Return the ``k`` newest runs of every workflow in ``workflow_ids``.

        Fetches all matching runs in a single query (ordered by ``run_number``
        DESC) and keeps the first *k* per workflow in Python. This avoids the
        ``SetOperationQuery`` rendering defect where UNION ALL branches with
        ``ORDER BY ... LIMIT`` produce invalid SQL.

        Args:
            workflow_ids: Local workflow ids to scope to.
            k: How many runs to keep per workflow (at least one).

        Returns:
            The matched runs (empty when ``workflow_ids`` is empty).
        """
        if not workflow_ids:
            return []
        rows = cls._build_latest_per_workflow_query(workflow_ids, k).all()
        counts: dict[int, int] = {}
        result: list[WorkflowRun] = []
        for row in rows:
            wid = row.workflow_id
            counts[wid] = counts.get(wid, 0) + 1
            if counts[wid] <= k:
                result.append(row)
        return result

    @classmethod
    def _build_latest_per_workflow_query(cls, workflow_ids: list[int], k: int) -> Any:
        """Build a query returning the latest *k* runs per workflow.

        The library's ``SetOperationQuery`` renders UNION ALL operands without
        parentheses, so branches containing ``ORDER BY ... LIMIT`` produce
        invalid SQL (documented in python-activerecord plan
        ``2026-10-11/set-operation-query-rendering.md``). This method works
        around that by fetching all matching runs in one query and deduplicating
        per workflow in Python.
        """
        return (
            cls.query()
            .where(cls.c.workflow_id.in_(workflow_ids))
            .order_by((cls.c.run_number, "DESC"))
        )

    @classmethod
    def status_summary(cls, workflow_ids: list[int]) -> list[dict[str, Any]]:
        """Aggregate run counts per workflow, by each run's newest attempt.

        Status is not a run column: it belongs to the execution, so the counts
        are taken from the newest attempt of every run. Two queries -- the runs
        of those workflows, then their attempts -- and the newest attempt per
        run is kept in Python.

        Args:
            workflow_ids: Local workflow ids to scope to.

        Returns:
            One dictionary per workflow with ``workflow_id``, ``total``,
            ``completed``, ``success``, ``failure`` and ``in_progress``
            (workflows with no runs are absent).
        """
        if not workflow_ids:
            return []

        from .attempt import RunAttempt

        runs = (
            cls.query()
            .select(cls.c.id, cls.c.workflow_id)
            .where(cls.c.workflow_id.in_(workflow_ids))
            .all()
        )
        by_run: dict[int, int] = {row.id: row.workflow_id for row in runs if row.id is not None}
        if not by_run:
            return []

        attempts = (
            RunAttempt.query()
            .select(
                RunAttempt.c.run_id,
                RunAttempt.c.attempt_number,
                RunAttempt.c.status,
                RunAttempt.c.conclusion,
            )
            .where(RunAttempt.c.run_id.in_(list(by_run)))
            .order_by((RunAttempt.c.run_id, "ASC"))
            .order_by((RunAttempt.c.attempt_number, "DESC"))
            .all()
        )
        newest: dict[int, tuple[str, str | None]] = {}
        for row in attempts:
            newest.setdefault(row.run_id, (row.status, row.conclusion))

        counts: dict[int, dict[str, Any]] = {}
        for run_id, workflow_id in by_run.items():
            entry = counts.setdefault(
                workflow_id,
                {
                    "workflow_id": workflow_id,
                    "total": 0,
                    "completed": 0,
                    "success": 0,
                    "failure": 0,
                    "in_progress": 0,
                },
            )
            entry["total"] += 1
            status, conclusion = newest.get(run_id, ("queued", None))
            if status == "completed":
                entry["completed"] += 1
                if conclusion == "success":
                    entry["success"] += 1
                elif conclusion == "failure":
                    entry["failure"] += 1
            elif status == "in_progress":
                entry["in_progress"] += 1
        return list(counts.values())

    # ---- behavior -------------------------------------------------------------

    def refresh_metadata(
        self,
        *,
        run_number: int | None = None,
        pr_number: int | None = None,
        event: str | None = None,
        branch: str | None = None,
        commit_sha: str | None = None,
        commit_message: str | None = None,
        display_title: str | None = None,
        head_repo: str | None = None,
        commit_time: datetime | None = None,
        commit_author: str | None = None,
        base_ref: str | None = None,
        actor: str | None = None,
    ) -> WorkflowRun:
        """Apply attempt-independent metadata to this stored run.

        The execution a payload describes -- its status, conclusion and
        timestamps -- belongs to ``ghat_run_attempts``, the single record of it.
        Only identity and commit metadata land here, so a late payload for an
        older attempt cannot rewrite the run's current state.

        ``None``, a blank string and zero mean "the caller does not know a new
        value", never "clear it": a run payload carries no PR number once the
        pull request is closed, and writing ``None`` would erase the cache.
        """
        values = {
            "run_number": run_number,
            "pr_number": pr_number,
            "event": event,
            "branch": branch,
            "commit_sha": commit_sha,
            "commit_message": commit_message,
            "display_title": display_title,
            "head_repo": head_repo,
            "commit_time": commit_time,
            "commit_author": commit_author,
            "base_ref": base_ref,
            "actor": actor,
        }
        for field, value in values.items():
            if value is None or value == "" or value == 0:
                continue
            if getattr(self, field) != value:
                setattr(self, field, value)
        if self.is_dirty:
            self.save()
        return self

    def attempt_ids(self) -> list[int]:
        """Local ids of every stored attempt of this run, number ascending.

        One small query, reused by every two-hop job read: a run holds a handful
        of attempts, so doing this first keeps the job query to a single
        ``WHERE attempt_id IN (...)`` instead of a join the ORM would have to
        express for us.
        """
        from .attempt import RunAttempt

        if self.id is None:
            return []
        rows = (
            RunAttempt.query()
            .select(RunAttempt.c.id, RunAttempt.c.attempt_number)
            .where(RunAttempt.c.run_id == self.id)
            .order_by((RunAttempt.c.attempt_number, "ASC"))
            .all()
        )
        return [row.id for row in rows if row.id is not None]

    def jobs_of_attempt(self, attempt: int | None = None) -> list[Job]:
        """Return the stored jobs of one attempt, in local id order.

        A re-run reuses the run id and increments ``run_attempt``, and job ids
        differ per attempt, so the table accumulates jobs from every attempt.
        Everything that reports "the jobs of this run" means the latest attempt
        -- that is what GitHub itself returns from ``/runs/{id}/jobs``.

        Args:
            attempt: Attempt number to select; ``None`` uses this run's own
                attempt.

        Returns:
            The jobs of that attempt (empty when nothing is stored for it).
        """
        from .attempt import RunAttempt

        if self.id is None:
            return []
        if attempt is None:
            attempt_row = RunAttempt.latest_for_run(self.id)
        else:
            attempt_row = RunAttempt.find_attempt(self.id, attempt)
        if attempt_row is None or attempt_row.id is None:
            return []
        return (
            Job.query().where(Job.c.attempt_id == attempt_row.id).order_by((Job.c.id, "ASC")).all()
        )

    # ---- shared internals -----------------------------------------------------

    def _stored_record(self) -> WorkflowRun | None:
        """The persisted record of this run's GitHub id, if any."""
        cls = type(self)
        return cls.find_one(cls.c.github_run_id == self.github_run_id)


class AsyncWorkflowRun(
    AsyncFindOrCreateMixin, _AsyncRunStateFields, DefaultTimestampMixin, AsyncActiveRecord
):
    """A single workflow run, asynchronous twin.

    Behavior-identical to :class:`WorkflowRun`; every method performs the
    same requests and raises the same exceptions.
    """

    __table_name__ = "ghat_workflow_runs"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    workflow_id: int = 0
    github_run_id: int = 0
    run_number: int = 0
    #: Cached pull request attribution. ``None`` never means "no PR": GitHub
    #: leaves ``pull_requests[]`` empty on fork PRs, so this is an accelerator
    #: only. Queries by PR resolve the head sha instead.
    pr_number: int | None = None
    event: str = ""
    branch: str = ""
    commit_sha: str = ""
    commit_message: str = ""
    #: GitHub's own one-line title for this run (commit subject, or ``title #n``).
    display_title: str = ""
    #: The repository the code came from; differs from the subscription on a fork.
    head_repo: str = ""
    #: The commit's timestamp, which is not when CI started.
    commit_time: datetime | None = None
    #: The commit author, which is not who triggered the run.
    commit_author: str = ""
    #: Target branch of the pull request, when the run has one.
    base_ref: str = ""
    actor: str = ""
    #: Last webhook delivery that touched this row.
    webhook_received_at: datetime | None = None

    #: Owning workflow (pairs with :attr:`AsyncWorkflow.runs`).
    workflow: ClassVar[AsyncBelongsTo[AsyncWorkflow]] = AsyncBelongsTo(
        foreign_key="workflow_id", inverse_of="runs"
    )
    #: Every stored execution of this run, in attempt-number order. A job no
    #: longer hangs off the run directly (``attempt_id -> run_id`` would be a
    #: BCNF violation), so reading the run's jobs goes through these.
    attempts: ClassVar[AsyncOrderedHasMany[AsyncRunAttempt]] = AsyncOrderedHasMany(
        foreign_key="run_id",
        inverse_of="run",
        order_key=lambda attempt: attempt.attempt_number,
    )

    # ---- derived: the state of the newest attempt ---------------------------
    #
    # See :class:`WorkflowRun`: the run row is identity plus metadata, and the
    # current state is computed from the newest attempt at query time.

    @classmethod
    def _latest_column(cls, dialect: Any, attribute: str) -> Any:
        """Correlated scalar subquery: one column of this run's newest attempt.

        See :meth:`WorkflowRun._latest_column`; the async twin.
        """
        column = getattr(AsyncRunAttempt.c, attribute)
        inner = (
            AsyncRunAttempt.query()
            .select(column)
            .where(AsyncRunAttempt.c.run_id == cls.c.id)
            .order_by((AsyncRunAttempt.c.attempt_number, "DESC"))
            .limit(1)
        )
        return Subquery(dialect, inner.to_sql())

    def _apply_attempt_state(self, attempt: RunAttempt | None) -> None:
        """Expose an attempt's state through the derived fields, in memory.

        A write path already has the attempt row in hand; re-reading the run
        through a ``derived`` query just to fill these attributes would
        cost one query per delivery. ``None`` leaves them unset.
        """
        if attempt is None:
            return
        self.__dict__["latest_attempt"] = attempt.attempt_number
        self.__dict__["latest_status"] = attempt.status
        self.__dict__["latest_conclusion"] = attempt.conclusion
        self.__dict__["latest_started_at"] = attempt.started_at
        self.__dict__["latest_completed_at"] = attempt.completed_at
        self.__dict__["latest_github_created_at"] = attempt.github_created_at

    # ---- query domain -------------------------------------------------------

    @classmethod
    async def find_by_github_id(
        cls, github_run_id: int, *, derived: bool = False
    ) -> AsyncWorkflowRun | None:
        """Return the stored record of a GitHub run id, or ``None``.

        See :meth:`WorkflowRun.find_by_github_id`; ``derived`` loads the newest
        attempt's state in the same query.
        """
        return await cls.find_one(cls.c.github_run_id == github_run_id, derived=derived)

    @classmethod
    def recent(
        cls,
        workflow_ids: list[int],
        branch: str | None = None,
    ) -> AsyncActiveQuery:
        """Query builder over the runs of ``workflow_ids``, newest first.

        See :meth:`WorkflowRun.recent`; no ``status`` filter, for the same
        reason.
        """
        query = cls.query().where(cls.c.workflow_id.in_(workflow_ids))
        if branch:
            query = query.where(cls.c.branch == branch)
        return query.order_by((cls.c.run_number, "DESC"))

    @classmethod
    async def latest_by_branch(
        cls, workflow_ids: list[int], branch: str
    ) -> AsyncWorkflowRun | None:
        """Return the newest run of ``workflow_ids`` on ``branch``, or ``None``."""
        if not workflow_ids:
            return None
        return await cls.recent(workflow_ids, branch=branch).one()

    @classmethod
    async def latest_per_workflow(
        cls, workflow_ids: list[int], k: int = 1
    ) -> list[AsyncWorkflowRun]:
        """Return the ``k`` newest runs of every workflow in ``workflow_ids``.

        See :meth:`WorkflowRun.latest_per_workflow`; the single-query plus
        Python dedup approach is identical.
        """
        if not workflow_ids:
            return []
        rows = await cls._build_latest_per_workflow_query(workflow_ids, k).all()
        counts: dict[int, int] = {}
        result: list[AsyncWorkflowRun] = []
        for row in rows:
            wid = row.workflow_id
            counts[wid] = counts.get(wid, 0) + 1
            if counts[wid] <= k:
                result.append(row)
        return result

    @classmethod
    def _build_latest_per_workflow_query(cls, workflow_ids: list[int], k: int) -> AsyncActiveQuery:
        """Build a query returning all runs for the given workflows.

        The library's ``SetOperationQuery`` renders UNION ALL operands without
        parentheses, so branches containing ``ORDER BY ... LIMIT`` produce
        invalid SQL. This method returns a plain query instead; the caller
        deduplicates per workflow in Python.
        """
        return (
            cls.query()
            .where(cls.c.workflow_id.in_(workflow_ids))
            .order_by((cls.c.run_number, "DESC"))
        )

    @classmethod
    async def status_summary(cls, workflow_ids: list[int]) -> list[dict[str, Any]]:
        """Aggregate run counts per workflow, by each run's newest attempt.

        See :meth:`WorkflowRun.status_summary`; the same two queries and the
        same keys.
        """
        if not workflow_ids:
            return []

        from .attempt import AsyncRunAttempt

        runs = await (
            cls.query()
            .select(cls.c.id, cls.c.workflow_id)
            .where(cls.c.workflow_id.in_(workflow_ids))
            .all()
        )
        by_run: dict[int, int] = {row.id: row.workflow_id for row in runs if row.id is not None}
        if not by_run:
            return []

        attempts = await (
            AsyncRunAttempt.query()
            .select(
                AsyncRunAttempt.c.run_id,
                AsyncRunAttempt.c.attempt_number,
                AsyncRunAttempt.c.status,
                AsyncRunAttempt.c.conclusion,
            )
            .where(AsyncRunAttempt.c.run_id.in_(list(by_run)))
            .order_by((AsyncRunAttempt.c.run_id, "ASC"))
            .order_by((AsyncRunAttempt.c.attempt_number, "DESC"))
            .all()
        )
        newest: dict[int, tuple[str, str | None]] = {}
        for row in attempts:
            newest.setdefault(row.run_id, (row.status, row.conclusion))

        counts: dict[int, dict[str, Any]] = {}
        for run_id, workflow_id in by_run.items():
            entry = counts.setdefault(
                workflow_id,
                {
                    "workflow_id": workflow_id,
                    "total": 0,
                    "completed": 0,
                    "success": 0,
                    "failure": 0,
                    "in_progress": 0,
                },
            )
            entry["total"] += 1
            status, conclusion = newest.get(run_id, ("queued", None))
            if status == "completed":
                entry["completed"] += 1
                if conclusion == "success":
                    entry["success"] += 1
                elif conclusion == "failure":
                    entry["failure"] += 1
            elif status == "in_progress":
                entry["in_progress"] += 1
        return list(counts.values())

    # ---- behavior -------------------------------------------------------------

    async def refresh_metadata(
        self,
        *,
        run_number: int | None = None,
        pr_number: int | None = None,
        event: str | None = None,
        branch: str | None = None,
        commit_sha: str | None = None,
        commit_message: str | None = None,
        display_title: str | None = None,
        head_repo: str | None = None,
        commit_time: datetime | None = None,
        commit_author: str | None = None,
        base_ref: str | None = None,
        actor: str | None = None,
    ) -> AsyncWorkflowRun:
        """Apply attempt-independent metadata; see :meth:`WorkflowRun.refresh_metadata`."""
        values = {
            "run_number": run_number,
            "pr_number": pr_number,
            "event": event,
            "branch": branch,
            "commit_sha": commit_sha,
            "commit_message": commit_message,
            "display_title": display_title,
            "head_repo": head_repo,
            "commit_time": commit_time,
            "commit_author": commit_author,
            "base_ref": base_ref,
            "actor": actor,
        }
        for field, value in values.items():
            if value is None or value == "" or value == 0:
                continue
            if getattr(self, field) != value:
                setattr(self, field, value)
        if self.is_dirty:
            await self.save()
        return self

    async def jobs_of_attempt(self, attempt: int | None = None) -> list[AsyncJob]:
        """Return the stored jobs of one attempt, in local id order.

        See :meth:`WorkflowRun.jobs_of_attempt`; the selection and ordering are
        identical.
        """
        from .attempt import AsyncRunAttempt

        if self.id is None:
            return []
        if attempt is None:
            attempt_row = await AsyncRunAttempt.latest_for_run(self.id)
        else:
            attempt_row = await AsyncRunAttempt.find_attempt(self.id, attempt)
        if attempt_row is None or attempt_row.id is None:
            return []
        # ``with_("steps")`` loads every job's steps in one extra query; asking
        # each job for its steps afterwards would be one query per job.
        return await (
            AsyncJob.query()
            .where(AsyncJob.c.attempt_id == attempt_row.id)
            .order_by((AsyncJob.c.id, "ASC"))
            .with_("steps")
            .all()
        )

    async def attempt_ids(self) -> list[int]:
        """Local ids of every stored attempt of this run, number ascending.

        See :meth:`WorkflowRun.attempt_ids`; the async twin.
        """
        from .attempt import AsyncRunAttempt

        if self.id is None:
            return []
        rows = await (
            AsyncRunAttempt.query()
            .select(AsyncRunAttempt.c.id, AsyncRunAttempt.c.attempt_number)
            .where(AsyncRunAttempt.c.run_id == self.id)
            .order_by((AsyncRunAttempt.c.attempt_number, "ASC"))
            .all()
        )
        return [row.id for row in rows if row.id is not None]

    # ---- shared internals -----------------------------------------------------

    async def _stored_record(self) -> AsyncWorkflowRun | None:
        """The persisted record of this run's GitHub id, if any."""
        cls = type(self)
        return await cls.find_one(cls.c.github_run_id == self.github_run_id)
