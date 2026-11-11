# src/rhosocial/github_actions_tracker/controllers/protocols.py
"""What a controller asks of the things it collaborates with.

Three kinds of promise live here.

**Storage.** :class:`ConnectionSource` is what a controller borrows a database
connection from; it is deliberately smaller than ``Database``, so a test can pass
a stub with one method.

**Capabilities.** The ``RunReader`` / ``RunControl`` / ``RepositoryReader`` /
``WebhookManager`` / ``CredentialProbe`` protocols live in ``services/github`` --
a controller names the GitHub capability it needs, not the client that provides
it.

**Other controllers.** A controller that needs one behaviour from another
controller declares *that behaviour*, not the class. ``WebhookController`` asks
for :class:`RunFetch` -- one method -- rather than a ``RunController`` with its
ten methods and its network reader and its row layer. The rule behind every
protocol here is the same: the width of the interface is the number of methods
actually called, and nothing else.

``Database`` and the controller classes satisfy these structurally, so nothing
in this module imports them.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from ..models import AsyncRepository, AsyncWorkflow, AsyncWorkflowRun
from ..views import DurationSample, RunRecord, RunView, RunWaitView

__all__ = [
    "ConnectionSource",
    "DispatchVerbs",
    "RecordReader",
    "RunFetch",
    "RunReads",
    "RunRefresh",
    "WaitableTrigger",
    "WorkflowLookup",
]


class ConnectionSource(Protocol):
    """Something a controller can borrow a connection from for one block."""

    def connection(self, *, timeout: float | None = None) -> AbstractAsyncContextManager[Any]:
        """Return a context manager that yields one connection.

        Args:
            timeout: How long to wait for one before giving up. The source's
                own default applies when omitted.
        """
        ...


class RunFetch(Protocol):
    """Bring one run up to date from GitHub. All a delivery asks for."""

    async def fetch(
        self, repo: AsyncRepository, run_id: int, *, with_jobs: bool = True
    ) -> AsyncWorkflowRun: ...


class RunRefresh(RunFetch, Protocol):
    """Accompanying reads a query path needs beside :meth:`RunFetch.fetch`."""

    async def fetch_by_name(
        self, repo_full_name: str, run_id: int, *, with_jobs: bool = True
    ) -> AsyncWorkflowRun: ...

    async def repository(self, repo_full_name: str) -> AsyncRepository: ...


class RunReads(RunRefresh, Protocol):
    """Plus the timing history a wait budgets from."""

    async def duration_sample(
        self, workflow_id: int, *, samples: int = 10
    ) -> DurationSample | None: ...


class DispatchVerbs(Protocol):
    """The five verbs that spend CI minutes or stop something already running."""

    async def cancel(self, repo_full_name: str, run_id: int) -> AsyncWorkflowRun: ...

    async def dispatch(
        self,
        repo_full_name: str,
        workflow: str,
        ref: str,
        *,
        inputs: dict[str, str] | None = None,
        resolve_attempts: int = 3,
        resolve_delay: float = 1.0,
    ) -> AsyncWorkflowRun: ...

    async def rerun(
        self,
        repo_full_name: str,
        run_id: int,
        *,
        resolve_attempts: int = 3,
        resolve_delay: float = 1.0,
    ) -> tuple[AsyncWorkflowRun, int]: ...

    async def rerun_failed(
        self,
        repo_full_name: str,
        run_id: int,
        *,
        resolve_attempts: int = 3,
        resolve_delay: float = 1.0,
    ) -> tuple[AsyncWorkflowRun, int, list[tuple[str, str]]]: ...

    async def run_for_head(
        self,
        repo_full_name: str,
        workflow: str,
        ref: str,
        *,
        commit_sha: str | None = None,
    ) -> AsyncWorkflowRun | None: ...


class RecordReader(Protocol):
    """Turn stored runs into the views a pull-request answer needs."""

    async def records(
        self,
        runs: Sequence[AsyncWorkflowRun],
        repo_full_name: str,
        *,
        with_jobs: bool = True,
    ) -> list[RunRecord]: ...


class WorkflowLookup(Protocol):
    """Resolve and list a repository's workflows."""

    async def get_workflow(self, repository: str, workflow: str) -> AsyncWorkflow: ...

    async def list_workflows(self, repository: str) -> list[AsyncWorkflow]: ...


class WaitableTrigger(Protocol):
    """Trigger, re-run, and wait -- the three verbs a workflow command uses."""

    async def trigger_workflow(
        self,
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str] | None = None,
        *,
        if_absent: bool = False,
        commit_sha: str | None = None,
    ) -> RunView: ...

    async def rerun_workflow(
        self, repository: str, run_id: int, *, failed_only: bool = False
    ) -> RunView: ...

    async def wait_for_completion(
        self,
        repository: str,
        run_id: int,
        timeout: float | None = 600.0,
        poll_interval: float = 5.0,
    ) -> RunWaitView: ...
