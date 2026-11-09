# src/rhosocial/github_actions_tracker/authorization/snapshot.py
"""Immutable value objects for the authorization snapshot.

The snapshot is a read model of the two facts request authentication needs:

* which repositories are subscribed, so an unknown name can be refused without
  touching the database;
* which webhook secret signs each one, so a delivery can be verified in memory.

The plan grammar and the database loader deliberately live elsewhere: this
module has no I/O and no model imports, so the verification logic stays testable
with a fake loader.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "AuthData",
    "AuthSnapshot",
    "WebhookDecision",
    "WebhookSecret",
    "WebhookVerdict",
    "normalize_repository_name",
]


def normalize_repository_name(name: str) -> str:
    """Return the key a repository is cached under.

    GitHub repository names are case-insensitive, and deliveries may spell the
    same repository differently. One normalized key is what keeps the cache
    from answering ``unknown`` for a repository it already holds.
    """
    return name.strip().lower()


@dataclass(frozen=True)
class WebhookSecret:
    """One repository's webhook secret, with the name used for processing.

    ``secret`` is excluded from ``repr`` so a debug print or an exception
    message cannot carry it. The value itself is plaintext because HMAC needs
    the key; the model already stores it that way.
    """

    repository: str
    secret: str = field(repr=False)
    state: str = "unknown"


@dataclass(frozen=True)
class AuthData:
    """What a loader returns before the service stamps a revision on it."""

    repositories: frozenset[str] = frozenset()
    webhook_secrets: Mapping[str, WebhookSecret] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthSnapshot(AuthData):
    """One loaded authorization snapshot.

    The object is immutable: a refresh builds a replacement and swaps it, so a
    request that already read the old snapshot keeps a consistent view.
    """

    revision: int = 0
    loaded_at: float = 0.0
    oversized: bool = False

    @classmethod
    def from_data(
        cls,
        data: AuthData,
        *,
        revision: int,
        loaded_at: float,
        max_entries: int,
    ) -> AuthSnapshot:
        """Stamp a loader's answer as one snapshot.

        Args:
            data: Repositories and secrets read by the loader.
            revision: Monotonic revision of this snapshot.
            loaded_at: Monotonic time the load completed.
            max_entries: Upper bound reported as ``oversized`` when exceeded.

        Returns:
            The immutable snapshot.
        """
        oversized = max_entries > 0 and len(data.repositories) > max_entries
        if oversized:
            logger.warning(
                "authorization cache holds %d repositories, above the configured "
                "limit of %d; the snapshot is kept whole",
                len(data.repositories),
                max_entries,
            )
        return cls(
            repositories=data.repositories,
            webhook_secrets=dict(data.webhook_secrets),
            revision=revision,
            loaded_at=loaded_at,
            oversized=oversized,
        )

    def superseded(self, loaded_at: float, revision: int) -> AuthSnapshot:
        """Return a copy with a new load stamp, keeping the same data."""
        return replace(self, loaded_at=loaded_at, revision=revision)

    def describe(self, *, now: float, ttl_seconds: float, dirty: bool = False) -> dict[str, Any]:
        """Return the public status of this snapshot, without any secret."""
        age = max(0.0, now - self.loaded_at)
        return {
            "enabled": ttl_seconds > 0,
            "loaded": True,
            "repositories": len(self.repositories),
            "secrets": len(self.webhook_secrets),
            "age_seconds": round(age, 3),
            "ttl_seconds": ttl_seconds,
            "stale": dirty or ttl_seconds <= 0 or age > ttl_seconds,
            "oversized": self.oversized,
            "revision": self.revision,
        }


class WebhookDecision(str, Enum):
    """What the webhook verifier concluded.

    These are authentication outcomes, not ledger reasons. The route maps each
    to an HTTP status and a metric; the old ledger reasons remain available
    only when the cache is disabled.
    """

    OK = "ok"
    UNKNOWN_REPOSITORY = "unknown_repository"
    SECRET_NOT_CONFIGURED = "secret_not_configured"
    BAD_SIGNATURE = "bad_signature"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class WebhookVerdict:
    """The verifier's answer and the repository name to process with."""

    decision: WebhookDecision
    repository: str = ""

    @property
    def ok(self) -> bool:
        """Whether the delivery may proceed to the webhook handler."""
        return self.decision is WebhookDecision.OK
