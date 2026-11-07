# src/rhosocial/github_actions_tracker/models/repository.py
"""Repository subscription model, sync and async twins.

A subscription is the anchor of every CI operation: it registers
``owner/repo``, caches the repository metadata (id, default branch) plus its
workflow list, and -- through :mod:`models.binding` -- records which credential
answers for it. A repository with no binding is an anonymous subscription of a
publicly readable repository.

The credential itself lives in a provider (``credentials/pat`` today). This
module never imports a provider's models: it reads the binding's opaque
``(kind, ref)`` pair and asks the registry to resolve it.

The model stores; it does not fetch. Subscribing, unsubscribing and refreshing
the cached workflows all involve a GitHub call, so they live in the controllers.
What remains here is the local half of each operation: creating a row from the
values a controller obtained, and :meth:`~Repository.unsubscribe` to drop the
local rows the controller has already dealt with remotely. The twins are
behavior-identical; the sync one exists for the project's sync/async parity rule.

References that would make the import graph cyclic -- the token, workflow,
run, job, step and log models, plus the ``core`` package (whose ``__init__``
imports the event handlers, which import the models) -- are imported lazily
inside method bodies; the type annotations use ``TYPE_CHECKING`` imports
instead.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.relation.async_descriptors import AsyncHasOne
from rhosocial.activerecord.relation.descriptors import HasOne

#: Imported at runtime, not under ``TYPE_CHECKING``: relation loading evaluates
#: the annotations of every relation on the class, and a name that only exists
#: while type-checking turns that evaluation into a NameError. ``webhook_secret``
#: refers back to this module only under ``TYPE_CHECKING``, so the import is not
#: a cycle.
from .binding import AsyncRepositoryBinding, RepositoryBinding
from .mixins import AsyncFindOrCreateMixin, FindOrCreateMixin
from .relations import AsyncOrderedHasMany, OrderedHasMany
from .webhook_secret import AsyncRepoWebhookSecret, RepoWebhookSecret

if TYPE_CHECKING:
    from .run import AsyncWorkflowRun, WorkflowRun
    from .workflow import AsyncWorkflow, Workflow

logger = logging.getLogger(__name__)

#: Default pagination for the local run pages.
DEFAULT_PER_PAGE = 30
#: Upper bound of one page, mirroring the GitHub API limit.
MAX_PER_PAGE = 100


class Repository(FindOrCreateMixin, DefaultTimestampMixin, ActiveRecord):
    """GitHub repository subscription, synchronous twin."""

    __table_name__ = "ghat_repositories"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    github_repo_id: int = 0
    full_name: str = ""
    owner: str = ""
    name: str = ""
    is_private: bool = False
    default_branch: str = "main"

    #: Cached workflows of this repository, in local id order.
    workflows: ClassVar[OrderedHasMany[Workflow]] = OrderedHasMany(
        foreign_key="repository_id",
        inverse_of="repository",
        order_key=lambda workflow: workflow.id or 0,
    )
    #: Which credential answers for this repository; absent for anonymous.
    binding: ClassVar[HasOne[RepositoryBinding]] = HasOne(
        foreign_key="repository_id", inverse_of="repository"
    )
    #: The secret this repository's webhook signs with; absent means it cannot
    #: receive deliveries at all (there is no shared fallback).
    webhook_secret: ClassVar[HasOne[RepoWebhookSecret]] = HasOne(
        foreign_key="repository_id", inverse_of="repository"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def find_by_full_name(cls, full_name: str) -> Repository | None:
        """Return the subscription for ``owner/repo``, or ``None``."""
        return cls.find_one(cls.c.full_name == full_name)

    @classmethod
    def find_by_github_id(cls, github_repo_id: int) -> Repository | None:
        """Return the subscription for a GitHub repository id, or ``None``."""
        return cls.find_one(cls.c.github_repo_id == github_repo_id)

    # ---- behavior -------------------------------------------------------------

    def unsubscribe(self) -> None:
        """Drop this repository's cached data, its binding and the row.

        Local only: removing the webhooks this service created on GitHub is the
        controller's job, so the model never needs a client. The DDL declares
        plain foreign keys, so the dependent rows go first.
        """
        self._purge_repository_data()
        # The binding goes with the subscription: leaving it behind would keep
        # the credential "in use" forever.
        if self.id is not None:
            from .binding import RepositoryBinding

            RepositoryBinding.unbind(self.id)
        self.delete()
        logger.info("Unsubscribed from '%s'", self.full_name)

    def _purge_repository_data(self) -> None:
        """Delete the cached CI data of this repository.

        The deletes are set-based (``select(id)`` then ``delete_all``): loading
        the cascade into memory would materialize the whole CI history just to
        drop it. They are also explicit rather than left to the schema's
        ``ON DELETE CASCADE``, because the test harness binds every model to
        its own database, where a cross-table cascade cannot reach.
        """
        from .attempt import RunAttempt
        from .job import Job
        from .log import JobLog
        from .run import WorkflowRun
        from .step import Step
        from .workflow import Workflow

        workflow_ids = [
            row.id
            for row in Workflow.query()
            .select(Workflow.c.id)
            .where(Workflow.c.repository_id == self.id or 0)
            .all()
            if row.id is not None
        ]
        if not workflow_ids:
            return

        run_ids = [
            row.id
            for row in WorkflowRun.query()
            .select(WorkflowRun.c.id)
            .where(WorkflowRun.c.workflow_id.in_(workflow_ids))
            .all()
            if row.id is not None
        ]
        job_ids: list[int] = []
        if run_ids:
            # A job belongs to an attempt, so the two-hop read: the runs'
            # attempts, then the jobs of those attempts.
            attempt_ids = [
                row.id
                for row in RunAttempt.query()
                .select(RunAttempt.c.id)
                .where(RunAttempt.c.run_id.in_(run_ids))
                .all()
                if row.id is not None
            ]
            if attempt_ids:
                job_ids = [
                    row.id
                    for row in Job.query()
                    .select(Job.c.id)
                    .where(Job.c.attempt_id.in_(attempt_ids))
                    .all()
                    if row.id is not None
                ]
        if job_ids:
            JobLog.query().where(JobLog.c.job_id.in_(job_ids)).delete_all()
            Step.query().where(Step.c.job_id.in_(job_ids)).delete_all()
            Job.query().where(Job.c.id.in_(job_ids)).delete_all()
        if run_ids:
            WorkflowRun.query().where(WorkflowRun.c.id.in_(run_ids)).delete_all()
        Workflow.query().where(Workflow.c.id.in_(workflow_ids)).delete_all()

    # ``workflows`` is the relation descriptor declared above; call it as
    # ``repo.workflows()`` to get the cached workflows in local id order.

    def resolve_workflow(self, ref: str) -> Workflow:
        """Resolve a workflow by file name, full path or numeric GitHub id.

        Args:
            ref: File name (``ci.yml``), path (``.github/workflows/ci.yml``)
                or numeric id.

        Returns:
            The stored workflow record.

        Raises:
            WorkflowNotFoundError: If no local workflow matches.
        """
        from .errors import WorkflowNotFoundError
        from .workflow import Workflow

        candidates = [ref]
        if not ref.startswith(".github/"):
            candidates.append(f".github/workflows/{ref}")

        repo_id = self.id or 0
        for candidate in candidates:
            workflow = Workflow.find_one(
                (Workflow.c.repository_id == repo_id) & (Workflow.c.path == candidate)
            )
            if workflow is not None:
                return workflow

        try:
            numeric_id = int(ref)
        except (TypeError, ValueError):
            numeric_id = None
        if numeric_id is not None:
            workflow = Workflow.find_one(
                (Workflow.c.repository_id == repo_id) & (Workflow.c.workflow_id == numeric_id)
            )
            if workflow is not None:
                return workflow

        raise WorkflowNotFoundError(f"Workflow '{ref}' not found in repository '{self.full_name}'")

    def runs(
        self,
        workflow: str | None = None,
        branch: str | None = None,
        per_page: int = 20,
        page: int = 1,
    ) -> list[WorkflowRun]:
        """List stored runs of this repository, newest first.

        Reads the local database only; no GitHub call is made. There is no
        ``status`` filter: status is a fact about an attempt, so a run row does
        not carry one.

        Args:
            workflow: Optional workflow file name, path or numeric id.
            branch: Optional branch filter.
            per_page: Page size (clamped to ``[1, MAX_PER_PAGE]``).
            page: 1-based page number.

        Returns:
            Matching runs ordered by ``run_number`` descending.
        """
        from .run import WorkflowRun

        if workflow:
            wf = self.resolve_workflow(workflow)
            workflow_ids = [wf.id or 0]
        else:
            workflow_ids = [w.id or 0 for w in self.workflows()]
            if not workflow_ids:
                return []

        size = DEFAULT_PER_PAGE if per_page < 1 else min(per_page, MAX_PER_PAGE)
        page_number = page if page > 0 else 1

        query = WorkflowRun.query().where(WorkflowRun.c.workflow_id.in_(workflow_ids))
        if branch:
            query = query.where(WorkflowRun.c.branch == branch)

        return (
            query.order_by((WorkflowRun.c.run_number, "DESC"))
            .limit(size)
            .offset((page_number - 1) * size)
            .all()
        )


class AsyncRepository(AsyncFindOrCreateMixin, DefaultTimestampMixin, AsyncActiveRecord):
    """GitHub repository subscription, asynchronous twin.

    Behavior-identical to :class:`Repository`; every method performs the
    same requests and raises the same exceptions.
    """

    __table_name__ = "ghat_repositories"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    github_repo_id: int = 0
    full_name: str = ""
    owner: str = ""
    name: str = ""
    is_private: bool = False
    default_branch: str = "main"

    #: Cached workflows of this repository, in local id order.
    workflows: ClassVar[AsyncOrderedHasMany[AsyncWorkflow]] = AsyncOrderedHasMany(
        foreign_key="repository_id",
        inverse_of="repository",
        order_key=lambda workflow: workflow.id or 0,
    )
    #: Which credential answers for this repository; absent for anonymous.
    binding: ClassVar[AsyncHasOne[AsyncRepositoryBinding]] = AsyncHasOne(
        foreign_key="repository_id", inverse_of="repository"
    )
    #: The secret this repository's webhook signs with; absent means it cannot
    #: receive deliveries at all (there is no shared fallback).
    webhook_secret: ClassVar[AsyncHasOne[AsyncRepoWebhookSecret]] = AsyncHasOne(
        foreign_key="repository_id", inverse_of="repository"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    async def find_by_full_name(cls, full_name: str) -> AsyncRepository | None:
        """Return the subscription for ``owner/repo``, or ``None``."""
        return await cls.find_one(cls.c.full_name == full_name)

    @classmethod
    async def find_by_github_id(cls, github_repo_id: int) -> AsyncRepository | None:
        """Return the subscription for a GitHub repository id, or ``None``."""
        return await cls.find_one(cls.c.github_repo_id == github_repo_id)

    # ---- behavior -------------------------------------------------------------

    async def unsubscribe(self) -> None:
        """Drop this repository's cached data, its binding and the row.

        See :meth:`Repository.unsubscribe`. Local only: the controller removes
        the webhooks first, so the model needs no client.
        """
        from .binding import AsyncRepositoryBinding

        await self._purge_repository_data()
        # The binding goes with the subscription: leaving it behind would keep
        # the credential "in use" forever, and the next delete would be refused
        # for a repository nobody is tracking any more.
        if self.id is not None:
            await AsyncRepositoryBinding.unbind(self.id)
        await self.delete()
        logger.info("Unsubscribed from '%s'", self.full_name)

    async def _purge_repository_data(self) -> None:
        """Delete the cached CI data of this repository.

        The deletes are set-based (``select(id)`` then ``delete_all``): loading
        the cascade into memory would materialize the whole CI history just to
        drop it. They are also explicit rather than left to the schema's
        ``ON DELETE CASCADE``, because the test harness binds every model to
        its own database, where a cross-table cascade cannot reach.
        """
        from .attempt import AsyncRunAttempt
        from .job import AsyncJob
        from .log import AsyncJobLog
        from .run import AsyncWorkflowRun
        from .step import AsyncStep
        from .workflow import AsyncWorkflow

        workflow_ids = [
            row.id
            for row in await AsyncWorkflow.query()
            .select(AsyncWorkflow.c.id)
            .where(AsyncWorkflow.c.repository_id == self.id or 0)
            .all()
            if row.id is not None
        ]
        if not workflow_ids:
            return

        run_ids = [
            row.id
            for row in await AsyncWorkflowRun.query()
            .select(AsyncWorkflowRun.c.id)
            .where(AsyncWorkflowRun.c.workflow_id.in_(workflow_ids))
            .all()
            if row.id is not None
        ]
        job_ids: list[int] = []
        if run_ids:
            attempt_ids = [
                row.id
                for row in await AsyncRunAttempt.query()
                .select(AsyncRunAttempt.c.id)
                .where(AsyncRunAttempt.c.run_id.in_(run_ids))
                .all()
                if row.id is not None
            ]
            if attempt_ids:
                job_ids = [
                    row.id
                    for row in await AsyncJob.query()
                    .select(AsyncJob.c.id)
                    .where(AsyncJob.c.attempt_id.in_(attempt_ids))
                    .all()
                    if row.id is not None
                ]
        if job_ids:
            await AsyncJobLog.query().where(AsyncJobLog.c.job_id.in_(job_ids)).delete_all()
            await AsyncStep.query().where(AsyncStep.c.job_id.in_(job_ids)).delete_all()
            await AsyncJob.query().where(AsyncJob.c.id.in_(job_ids)).delete_all()
        if run_ids:
            await AsyncWorkflowRun.query().where(AsyncWorkflowRun.c.id.in_(run_ids)).delete_all()
        await AsyncWorkflow.query().where(AsyncWorkflow.c.id.in_(workflow_ids)).delete_all()

    # ``workflows`` is the relation descriptor declared above; call it as
    # ``repo.workflows()`` to get the cached workflows in local id order.

    async def resolve_workflow(self, ref: str) -> Workflow:
        """Resolve a workflow by file name, full path or numeric GitHub id.

        See :meth:`Repository.resolve_workflow`; the resolution order and the
        exception set are identical.

        Raises:
            WorkflowNotFoundError: If no local workflow matches.
        """
        from .errors import WorkflowNotFoundError
        from .workflow import AsyncWorkflow

        candidates = [ref]
        if not ref.startswith(".github/"):
            candidates.append(f".github/workflows/{ref}")

        repo_id = self.id or 0
        for candidate in candidates:
            workflow = await AsyncWorkflow.find_one(
                (AsyncWorkflow.c.repository_id == repo_id) & (AsyncWorkflow.c.path == candidate)
            )
            if workflow is not None:
                return workflow

        try:
            numeric_id = int(ref)
        except (TypeError, ValueError):
            numeric_id = None
        if numeric_id is not None:
            workflow = await AsyncWorkflow.find_one(
                (AsyncWorkflow.c.repository_id == repo_id)
                & (AsyncWorkflow.c.workflow_id == numeric_id)
            )
            if workflow is not None:
                return workflow

        raise WorkflowNotFoundError(f"Workflow '{ref}' not found in repository '{self.full_name}'")

    async def runs(
        self,
        workflow: str | None = None,
        branch: str | None = None,
        per_page: int = 20,
        page: int = 1,
    ) -> list[AsyncWorkflowRun]:
        """List stored runs of this repository, newest first.

        See :meth:`Repository.runs`; no ``status`` filter, for the same reason.
        """
        from .run import AsyncWorkflowRun

        if workflow:
            wf = await self.resolve_workflow(workflow)
            workflow_ids = [wf.id or 0]
        else:
            workflow_ids = [w.id or 0 for w in await self.workflows()]
            if not workflow_ids:
                return []

        size = DEFAULT_PER_PAGE if per_page < 1 else min(per_page, MAX_PER_PAGE)
        page_number = page if page > 0 else 1

        query = AsyncWorkflowRun.query().where(AsyncWorkflowRun.c.workflow_id.in_(workflow_ids))
        if branch:
            query = query.where(AsyncWorkflowRun.c.branch == branch)

        return (
            await query.order_by((AsyncWorkflowRun.c.run_number, "DESC"))
            .limit(size)
            .offset((page_number - 1) * size)
            .all()
        )
