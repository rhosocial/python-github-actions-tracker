# src/rhosocial/github_actions_tracker/authorization/loader.py
"""The one batch read the authorization snapshot refreshes from.

This is the package's only file that imports ``models``. Everything else here
decides *when* to read and *what a read means*; this file answers the read
itself, with two bulk queries instead of the per-request lookups the receiver
used before.

Two queries rather than one join, on purpose: a join that returns a repository
once per secret needs de-duplication in Python, and the natural ORM spelling of
``LEFT JOIN`` in this codebase is a nested eager load -- one query per parent
row, which is the N+1 the plan forbids. Two flat ``select`` queries are O(1)
round trips no matter how many repositories are subscribed, and the merge is a
dictionary lookup.

The read is not a transaction and it writes nothing: it runs inside whatever
connection scope the caller has open (the composition root opens one), and the
result is turned into plain value objects immediately, so a model instance
never escapes this module.
"""

from __future__ import annotations

import logging
from typing import Any

from ..database import Database
from ..models import AsyncRepository, AsyncRepoWebhookSecret
from .snapshot import AuthData, WebhookSecret, normalize_repository_name

logger = logging.getLogger(__name__)

__all__ = ["WebhookSecretLoader"]


class WebhookSecretLoader:
    """Read every subscribed repository and the secret that signs for it."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def __call__(self) -> AuthData:
        """Return the repositories and secrets of the deployment, in one batch."""
        async with self._database.connection():
            repositories = await self._repository_names()
            secrets = await self._secrets()
        return self._merge(repositories, secrets)

    async def _repository_names(self) -> dict[int, str]:
        """``{repository id: full_name}``, in one query."""
        rows = (
            await AsyncRepository.query()
            .select(AsyncRepository.c.id, AsyncRepository.c.full_name)
            .all()
        )
        return {int(row.id): row.full_name for row in rows if row.id is not None and row.full_name}

    async def _secrets(self) -> list[Any]:
        """Every secret row, in one query, without touching the repository side."""
        return list(
            await AsyncRepoWebhookSecret.query()
            .select(
                AsyncRepoWebhookSecret.c.repository_id,
                AsyncRepoWebhookSecret.c.secret,
                AsyncRepoWebhookSecret.c.state,
            )
            .all()
        )

    @staticmethod
    def _merge(repositories: dict[int, str], secrets: list[Any]) -> AuthData:
        """Join the two reads in memory, keyed by normalized repository name.

        A repository whose name cannot be read is dropped: a snapshot is only
        useful if it can name the repositories it refuses. A secret whose
        repository is gone is dropped with it -- the row would never be asked
        about again, and keeping it would let a stale id answer ``ok``.
        """
        names: dict[str, str] = {}
        for repository_id, full_name in repositories.items():
            names[repository_id] = full_name
        webhook_secrets: dict[str, WebhookSecret] = {}
        for row in secrets:
            repository_id = int(row.repository_id)
            full_name = names.get(repository_id)
            secret = (row.secret or "").strip()
            if full_name is None or not secret:
                continue
            webhook_secrets[normalize_repository_name(full_name)] = WebhookSecret(
                repository=full_name,
                secret=secret,
                state=(row.state or "unknown"),
            )
        return AuthData(
            repositories=frozenset(normalize_repository_name(name) for name in names.values()),
            webhook_secrets=webhook_secrets,
        )
