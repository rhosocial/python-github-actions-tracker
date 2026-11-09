# src/rhosocial/github_actions_tracker/authorization/__init__.py
"""Authorization snapshots and the request verification they answer.

The package is a cross-cutting capability the adapters call *before* a
controller: whether a request carries a credential the service accepts must not
cost a database connection, and a webhook delivery that cannot be verified must
not acquire one at all.

``loader`` is deliberately not re-exported here. It is the only module that
imports ``models``, and an adapter importing this package must not pull the
model layer in transitively -- the layering test names the modules that are
allowed to touch the models, and an indirect import would quietly widen it.
"""

from __future__ import annotations

from .service import AuthorizationService
from .snapshot import (
    AuthData,
    AuthSnapshot,
    WebhookDecision,
    WebhookSecret,
    WebhookVerdict,
    normalize_repository_name,
)

__all__ = [
    "AuthData",
    "AuthSnapshot",
    "AuthorizationService",
    "WebhookDecision",
    "WebhookSecret",
    "WebhookVerdict",
    "normalize_repository_name",
]
