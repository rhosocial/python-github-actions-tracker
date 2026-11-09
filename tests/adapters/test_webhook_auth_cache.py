# tests/adapters/test_webhook_auth_cache.py
"""The webhook receiver's contract with a caller it has not authenticated.

Every case here is a request that must cost the database nothing: no connection
acquired, no ledger row, no delivery handler reached. That is the whole claim of
the snapshot -- the receiver used to open its per-request connection before it
knew whose delivery this was, so ten slow forged bodies were enough to keep real
deliveries waiting on the pool.

The last case is the rollback switch: with the cache disabled the route falls
back to the per-request lookup it used before, and that path *does* take one
short connection, on purpose, so that turning the cache off cannot break
verification.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect, Request

from rhosocial.github_actions_tracker.adapters.rest.routes.webhooks import github_webhook
from rhosocial.github_actions_tracker.authorization import AuthorizationService
from rhosocial.github_actions_tracker.authorization.snapshot import AuthData
from rhosocial.github_actions_tracker.config import AuthSettings

SECRET = "s3cret"
BODY = b'{"repository": {"full_name": "acme/demo"}}'


def _signature(payload: bytes, secret: str = SECRET) -> bytes:
    return b"sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest().encode()


class _Webhook:
    """Records every call the delivery handler makes, so a test can count them."""

    def __init__(self, *, secret: str | None = SECRET, failing: bool = False) -> None:
        self.secret = secret
        self.failing = failing
        self.resolve_calls = 0
        self.recorded: list[tuple[str, str]] = []
        self.missing: list[tuple[str, str]] = []
        self.processed: list[dict] = []

    async def resolve_secret(self, repo_full_name: str):
        self.resolve_calls += 1
        if self.failing:
            raise RuntimeError("the lookup path must not be reached")
        if self.secret is None:
            return None
        return self.secret, repo_full_name

    async def record_missing_secret(
        self, event_type: str, delivery_id: str = "", repo_full_name: str = ""
    ) -> dict:
        self.missing.append((event_type, delivery_id))
        return {"handled": False, "reason": "secret_not_configured"}

    async def record_signature_failure(self, event_type: str, delivery_id: str) -> None:
        self.recorded.append((event_type, delivery_id))

    async def process_webhook(self, **kwargs: object) -> dict:
        self.processed.append(dict(kwargs))
        return {"handled": True, "reason": ""}


class _CountingDatabase:
    """A database seam whose only job is to count the connections taken."""

    def __init__(self) -> None:
        self.acquisitions = 0

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[None]:
        self.acquisitions += 1
        yield None


class _Loader:
    """A reader the service refreshes from, with the failure it is asked for."""

    def __init__(self, *, secret: str | None = SECRET, error: Exception | None = None) -> None:
        self.secret = secret
        self.error = error
        self.calls = 0

    async def __call__(self) -> AuthData:
        self.calls += 1
        if self.error is not None:
            raise self.error
        repositories = frozenset({"acme/demo"})
        if self.secret is None:
            return AuthData(repositories=repositories)
        from rhosocial.github_actions_tracker.authorization import WebhookSecret

        return AuthData(
            repositories=repositories,
            webhook_secrets={
                "acme/demo": WebhookSecret(
                    repository="acme/demo", secret=self.secret, state="pushed"
                )
            },
        )


def _container(
    webhook: _Webhook,
    *,
    loader: _Loader | None = None,
    ttl_seconds: float = 30.0,
    auth: AuthSettings | None = None,
) -> SimpleNamespace:
    authorization = AuthorizationService(api_key="", ttl_seconds=ttl_seconds)
    if ttl_seconds > 0:
        authorization.attach_loader(loader or _Loader(secret=webhook.secret))
    return SimpleNamespace(
        webhook=webhook,
        authorization=authorization,
        settings=SimpleNamespace(auth=auth or AuthSettings()),
        database=_CountingDatabase(),
    )


class _App:
    """The smallest application the route reads from: ``app.state.container``."""

    def __init__(self, container: SimpleNamespace) -> None:
        self.state = SimpleNamespace(container=container)


def _request(
    payload: bytes = BODY,
    *,
    signature: bytes | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
    repository: str = "acme/demo",
    slow: float = 0.0,
) -> Request:
    body = (
        payload
        if repository is None
        else b'{"repository": {"full_name": "' + repository.encode() + b'"}}'
    )
    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        if slow:
            await asyncio.sleep(slow)
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/webhooks/github",
        "raw_path": b"/webhooks/github",
        "query_string": b"",
        "headers": headers
        or [
            (b"x-github-event", b"workflow_run"),
            (b"x-github-delivery", b"d-cache"),
            (b"x-hub-signature-256", signature if signature is not None else _signature(body)),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("tracker.test", 443),
        "app": _App(SimpleNamespace()),
    }
    return Request(scope, receive=receive)


def _run(request: Request, container: SimpleNamespace) -> dict:
    request.scope["app"] = _App(container)
    return asyncio.run(github_webhook(request))


def _refusal(request: Request, container: SimpleNamespace) -> HTTPException:
    request.scope["app"] = _App(container)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(github_webhook(request))
    return caught.value


# -- the four refusals --------------------------------------------------------


def test_an_unknown_repository_is_refused_without_a_connection() -> None:
    webhook = _Webhook()
    container = _container(webhook)

    error = _refusal(_request(repository="evil/other"), container)

    assert error.status_code == 401
    assert "Unknown repository" in str(error.detail)
    assert container.database.acquisitions == 0
    assert webhook.recorded == [] and webhook.missing == []
    assert webhook.processed == []


def test_a_repository_with_no_secret_is_refused_without_a_connection() -> None:
    webhook = _Webhook(secret=None)
    container = _container(webhook)

    error = _refusal(_request(), container)

    assert error.status_code == 401
    assert "No webhook secret" in str(error.detail)
    assert container.database.acquisitions == 0
    assert webhook.missing == [], "the cached path reports the reason; it does not store a row"


def test_a_forged_signature_is_refused_without_a_connection() -> None:
    webhook = _Webhook()
    container = _container(webhook)

    error = _refusal(_request(signature=b"sha256=" + b"0" * 64), container)

    assert error.status_code == 401
    assert "Invalid webhook signature" in str(error.detail)
    assert container.database.acquisitions == 0
    assert webhook.recorded == [], "a refused delivery must not write to the ledger"


def test_a_snapshot_that_never_loaded_answers_unavailable() -> None:
    """Not a 401: blaming the client for the service's own state hides the outage."""
    webhook = _Webhook()
    container = _container(webhook, loader=_Loader(error=RuntimeError("no database")))

    error = _refusal(_request(), container)

    assert error.status_code == 503
    assert container.database.acquisitions == 0


# -- the body bounds ----------------------------------------------------------


def test_an_oversized_body_is_refused_before_anything_else() -> None:
    """A body too big to be a delivery is answered without a connection or a secret."""
    webhook = _Webhook()
    container = _container(webhook, auth=AuthSettings(webhook_max_body_bytes=8))

    error = _refusal(_request(payload=BODY, repository=None), container)

    assert error.status_code == 413
    assert container.database.acquisitions == 0
    assert webhook.resolve_calls == 0


def test_a_body_that_takes_too_long_is_refused() -> None:
    """The other half of "do not hold the pool while a client streams"."""
    webhook = _Webhook()
    container = _container(webhook, auth=AuthSettings(webhook_read_timeout_seconds=0.01))

    error = _refusal(_request(slow=0.2), container)

    assert error.status_code == 408
    assert container.database.acquisitions == 0


# -- the accepted delivery ----------------------------------------------------


def test_a_real_delivery_is_processed_in_one_connection_of_its_own() -> None:
    webhook = _Webhook()
    container = _container(webhook)

    result = _run(_request(), container)

    assert result["status"] == "processed"
    assert container.database.acquisitions == 1
    assert webhook.processed and webhook.processed[0]["repository_full_name"] == "acme/demo"


def test_the_loader_is_called_once_for_many_deliveries() -> None:
    """One refresh per TTL: the cost of the snapshot does not scale with traffic."""
    webhook = _Webhook()
    loader = _Loader()
    container = _container(webhook, loader=loader)

    for _ in range(5):
        _run(_request(), container)

    assert loader.calls == 1


# -- the rollback switch ------------------------------------------------------


def test_with_the_cache_disabled_the_old_lookup_still_verifies() -> None:
    """``GHAT_AUTH_CACHE_TTL_SECONDS=0`` must not break the receiver.

    The fallback takes one connection of its own, around the secret read and
    nothing else, and keeps recording the reasons in the ledger -- which is what
    turning the cache off is for.
    """
    webhook = _Webhook()
    container = _container(webhook, ttl_seconds=0)

    result = _run(_request(), container)

    assert result["status"] == "processed"
    assert webhook.resolve_calls == 1
    # Two, not one: the secret read, and then the processing itself. The cached
    # path needs only the second, and that difference is the whole point.
    assert container.database.acquisitions == 2
    assert webhook.processed[0]["repository_full_name"] == "acme/demo"


def test_with_the_cache_disabled_a_refusal_is_still_recorded() -> None:
    """The rollback path keeps the ledger behaviour it always had."""
    webhook = _Webhook(secret=None)
    container = _container(webhook, ttl_seconds=0)

    error = _refusal(_request(), container)

    assert error.status_code == 401
    assert webhook.missing == [("workflow_run", "d-cache")]
    assert webhook.resolve_calls == 1


def test_a_disconnect_is_not_an_error() -> None:
    webhook = _Webhook()
    container = _container(webhook)

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/webhooks/github",
            "headers": [(b"x-github-delivery", b"d-gone")],
            "app": _App(container),
        },
        receive=receive,
    )

    assert asyncio.run(github_webhook(request)) == {"status": "disconnected"}


def test_client_disconnect_mid_body_is_swallowed() -> None:
    """The route answers ``disconnected`` rather than raising ``ClientDisconnect``."""
    webhook = _Webhook()
    container = _container(webhook)
    calls = {"count": 0}

    async def receive() -> dict:
        calls["count"] += 1
        if calls["count"] == 1:
            return {"type": "http.request", "body": BODY, "more_body": True}
        raise ClientDisconnect(1)

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/webhooks/github",
            "headers": [(b"x-github-delivery", b"d-mid")],
            "app": _App(container),
        },
        receive=receive,
    )

    assert asyncio.run(github_webhook(request)) == {"status": "disconnected"}
