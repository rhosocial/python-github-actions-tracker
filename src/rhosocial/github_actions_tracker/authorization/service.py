# src/rhosocial/github_actions_tracker/authorization/service.py
"""The authorization snapshot and the request authentication it answers.

Two credentials are handled here:

* the static bearer key from ``GHAT_API_KEY``, which never needs the database;
* a repository's webhook secret, which is read once per TTL into an immutable
  snapshot and then checked in memory.

The service has no model imports and no database handle of its own. A loader is
attached by the composition root; this module only decides when to call it and
what a failed refresh means.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .snapshot import (
    AuthData,
    AuthSnapshot,
    WebhookDecision,
    WebhookVerdict,
    normalize_repository_name,
)

logger = logging.getLogger(__name__)

__all__ = ["AuthorizationService"]

#: A loader answers one batch of repositories and secrets.
Loader = Callable[[], Awaitable[AuthData]]


def _count_refresh(outcome: str) -> None:
    """Record one refresh attempt. Never raises: a metric is not a reason to fail.

    Counted here rather than at the call site because this is the one place both
    outcomes pass through, and a refresh nobody counted is how "the cache stopped
    refreshing" stays invisible until a delivery is refused.
    """
    try:
        from ..observability.definitions import AUTH_CACHE_REFRESH_TOTAL

        AUTH_CACHE_REFRESH_TOTAL.inc(outcome=outcome)
    except Exception as exc:  # noqa: BLE001 - a metric must not fail a request
        logger.debug("could not record an authorization refresh: %s", exc)


class AuthorizationService:
    """Verify request credentials from memory, refreshing on a TTL.

    Args:
        api_key: The static operator key. Empty keeps the development mode where
            the protocol endpoint is open.
        ttl_seconds: How long a snapshot serves before a request refreshes it.
            Zero disables the snapshot.
        max_entries: Upper bound reported when a snapshot is unexpectedly large.
        backoff_seconds: Minimum wait after a failed refresh before another try.
        clock: Monotonic clock, injectable for tests.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        ttl_seconds: float = 30.0,
        max_entries: int = 50_000,
        backoff_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_key = api_key
        self._ttl = max(0.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._backoff = max(0.0, float(backoff_seconds))
        self._clock = clock
        self._loader: Loader | None = None
        self._snapshot: AuthSnapshot | None = None
        self._revision = 0
        self._lock = asyncio.Lock()
        self._refreshing = False
        self._dirty = False
        self._last_error: str | None = None
        self._next_retry_at = 0.0

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        clock: Callable[[], float] | None = None,
    ) -> AuthorizationService:
        """Build the service from one settings object."""
        auth = settings.auth
        return cls(
            api_key=settings.service.api_key,
            ttl_seconds=auth.cache_ttl_seconds,
            max_entries=auth.cache_max_entries,
            backoff_seconds=auth.cache_refresh_backoff_seconds,
            clock=clock or time.monotonic,
        )

    # -- lifecycle -------------------------------------------------------------

    @property
    def cache_enabled(self) -> bool:
        """Whether webhook verification uses a snapshot at all."""
        return self._ttl > 0

    @property
    def api_key_configured(self) -> bool:
        """Whether a static bearer key is configured."""
        return bool(self._api_key)

    @property
    def snapshot(self) -> AuthSnapshot | None:
        """The current snapshot, for diagnostics and tests."""
        return self._snapshot

    def attach_loader(self, loader: Loader) -> None:
        """Give the service the batch read it refreshes from."""
        self._loader = loader

    async def start(self) -> None:
        """Load the first snapshot, if caching is enabled and a loader is set.

        A failure is recorded rather than raised: a process that cannot load its
        snapshot may still serve the static-bearer surfaces, and readiness is
        where the reduced capability is reported.
        """
        if not self.cache_enabled or self._loader is None:
            return
        await self.refresh(force=True)

    def invalidate(self) -> None:
        """Mark the snapshot stale after a local write.

        The old snapshot keeps serving until the next refresh completes, so a
        rotation never makes every delivery fail while a query is in flight.
        """
        self._dirty = True

    # -- verification ----------------------------------------------------------

    def verify_bearer(self, token: str) -> bool:
        """Return whether *token* is the configured operator key.

        An empty key is the explicit development mode: every caller is allowed.
        The comparison is constant-time and done on bytes, so a non-ASCII token
        is refused rather than turning into an exception.
        """
        if not self._api_key:
            return True
        if not token:
            return False
        return hmac.compare_digest(
            token.encode("utf-8"),
            self._api_key.encode("utf-8"),
        )

    async def verify_webhook(
        self, repository: str, payload: bytes, signature: str
    ) -> WebhookVerdict:
        """Verify one delivery against the snapshot, without touching the database.

        Args:
            repository: ``owner/repo`` read from the delivery body.
            payload: Raw request body.
            signature: ``X-Hub-Signature-256`` value.

        Returns:
            The decision and, when accepted, the repository name to process with.
        """
        name = normalize_repository_name(repository)
        if not name:
            return WebhookVerdict(WebhookDecision.UNKNOWN_REPOSITORY)
        snapshot = await self._snapshot_for_verify()
        if snapshot is None:
            return WebhookVerdict(WebhookDecision.UNAVAILABLE)
        entry = snapshot.webhook_secrets.get(name)
        if entry is None:
            if name in snapshot.repositories:
                return WebhookVerdict(WebhookDecision.SECRET_NOT_CONFIGURED)
            return WebhookVerdict(WebhookDecision.UNKNOWN_REPOSITORY)
        if not self._signature_matches(payload, signature, entry.secret):
            return WebhookVerdict(WebhookDecision.BAD_SIGNATURE)
        return WebhookVerdict(WebhookDecision.OK, entry.repository)

    # -- status ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return the snapshot state, without any secret."""
        snapshot = self._snapshot
        if not self.cache_enabled:
            return {
                "enabled": False,
                "loaded": snapshot is not None,
                "repositories": len(snapshot.repositories) if snapshot else 0,
                "secrets": len(snapshot.webhook_secrets) if snapshot else 0,
                "age_seconds": None,
                "ttl_seconds": 0.0,
                "stale": False,
                "oversized": bool(snapshot and snapshot.oversized),
                "revision": snapshot.revision if snapshot else 0,
                "last_error": self._last_error,
            }
        if snapshot is None:
            return {
                "enabled": True,
                "loaded": False,
                "repositories": 0,
                "secrets": 0,
                "age_seconds": None,
                "ttl_seconds": self._ttl,
                "stale": True,
                "oversized": False,
                "revision": 0,
                "last_error": self._last_error,
            }
        report = snapshot.describe(now=self._clock(), ttl_seconds=self._ttl, dirty=self._dirty)
        report["last_error"] = self._last_error
        return report

    # -- refresh ---------------------------------------------------------------

    async def refresh(self, *, force: bool = False) -> AuthSnapshot | None:
        """Refresh the snapshot when it is stale, returning what is current now.

        The first caller after the TTL performs the read; concurrent callers
        keep using the previous snapshot while that read is in flight. A failed
        read leaves the previous snapshot in place and backs off, so a database
        hiccup does not turn into one failed query per delivery.
        """
        if self._loader is None:
            return self._snapshot
        snapshot = self._snapshot
        if not force and snapshot is not None and self._is_fresh(snapshot):
            return snapshot
        if not force and snapshot is not None and self._clock() < self._next_retry_at:
            return snapshot

        async with self._lock:
            if not force and self._refreshing:
                return self._snapshot
            snapshot = self._snapshot
            if not force and snapshot is not None and self._is_fresh(snapshot):
                return snapshot
            if not force and snapshot is not None and self._clock() < self._next_retry_at:
                return snapshot
            self._refreshing = True

        try:
            data = await self._loader()
        except Exception as exc:  # noqa: BLE001 - a refresh must not fail a request
            self._last_error = f"{type(exc).__name__}: refresh failed"
            self._next_retry_at = self._clock() + self._backoff
            logger.warning("authorization cache refresh failed: %s", exc)
            _count_refresh("error")
            return self._snapshot
        finally:
            async with self._lock:
                self._refreshing = False

        self._revision += 1
        self._snapshot = AuthSnapshot.from_data(
            data,
            revision=self._revision,
            loaded_at=self._clock(),
            max_entries=self._max_entries,
        )
        self._dirty = False
        self._last_error = None
        self._next_retry_at = 0.0
        _count_refresh("ok")
        logger.info(
            "authorization cache loaded %d repositories (%d with secrets), revision %d",
            len(self._snapshot.repositories),
            len(self._snapshot.webhook_secrets),
            self._snapshot.revision,
        )
        return self._snapshot

    async def _snapshot_for_verify(self) -> AuthSnapshot | None:
        """Return a snapshot for verification, refreshing if needed."""
        if not self.cache_enabled:
            return None
        snapshot = self._snapshot
        if snapshot is not None and self._is_fresh(snapshot):
            return snapshot
        if self._refreshing:
            return snapshot
        if snapshot is not None and self._clock() < self._next_retry_at:
            return snapshot
        await self.refresh()
        return self._snapshot

    def _is_fresh(self, snapshot: AuthSnapshot) -> bool:
        if not self.cache_enabled or self._dirty:
            return False
        return self._clock() - snapshot.loaded_at <= self._ttl

    @staticmethod
    def _signature_matches(payload: bytes, signature: str, secret: str) -> bool:
        """Check GitHub's HMAC without leaking the expected value."""
        if not signature.startswith("sha256="):
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature[7:])
