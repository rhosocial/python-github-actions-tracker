# src/rhosocial/github_actions_tracker/services/github/__init__.py
"""GitHub as a service: capability protocols, and one object per group.

A service names the protocol it needs and is handed anything that satisfies it --
the implementation here in production, a recording fake in a test. The protocols
are structural, so a fake does not inherit behaviour it will not use, and a reader
typed against ``RunReader`` cannot dispatch a workflow.

The grouping is by resource, which is the unit GitHub itself uses:

* :class:`~.runs.GithubRuns` -- ``/actions/runs``, reads and the three verbs that
  spend CI minutes;
* :class:`~.repositories.GithubRepositories` -- ``/repos``, metadata, workflows
  and credential-scoped discovery.

:class:`~.payloads.GithubPayloads` translates the JSON those groups return into
model-shaped values. It is the only class that knows a GitHub field name, which
is what keeps a rename in the interface from reaching the controllers and the
models.
"""

from __future__ import annotations

from .base import TokenSource
from .credentials import GithubCredentials
from .payloads import GithubPayloads
from .protocols import (
    CredentialProbe,
    RepositoryReader,
    RunControl,
    RunReader,
    WebhookManager,
)
from .repositories import GithubRepositories
from .runs import GithubRuns
from .webhooks import GithubWebhooks

__all__ = [
    "CredentialProbe",
    "GithubCredentials",
    "GithubPayloads",
    "GithubRepositories",
    "GithubRuns",
    "GithubWebhooks",
    "RepositoryReader",
    "RunControl",
    "RunReader",
    "TokenSource",
    "WebhookManager",
]
