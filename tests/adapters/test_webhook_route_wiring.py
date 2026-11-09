# tests/adapters/test_webhook_route_wiring.py
"""The webhook route must take its controller from the application's container.

``get_webhook_service`` needs the request to reach ``app.state.container`` (the
webhook controller carries the database seam, so the deferred log fetch can take
a connection of its own). The signature changed once and the call site did not,
so every delivery answered 500 with ``missing 1 required positional argument`` --
a failure the unit suite could not see, because the integration tests that drive
the real route are skipped without PostgreSQL.

The credential half of the same question lives in
:mod:`tests.adapters.test_webhook_auth_cache`; what stays here is the wiring
itself, and the one behaviour that is not about credentials: a caller that hangs
up mid-body is not an error to report.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from rhosocial.github_actions_tracker.adapters.rest.routes.webhooks import github_webhook
from rhosocial.github_actions_tracker.authorization import (
    AuthorizationService,
    WebhookSecret,
)
from rhosocial.github_actions_tracker.authorization.snapshot import AuthData
from rhosocial.github_actions_tracker.config import AuthSettings


def _request(body: bytes, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/webhooks/github",
        "raw_path": b"/webhooks/github",
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 1234),
        "server": ("tracker.test", 443),
        "app": _App(),
    }
    return Request(scope, receive=receive)


class _Webhook:
    """Records what reached the delivery handler -- and refuses to be surprised."""

    def __init__(self, secret: str | None = "s3cret") -> None:
        self.secret = secret
        self.recorded: list[tuple[str, str]] = []
        self.missing: list[tuple[str, str]] = []
        self.processed: list[dict] = []
        #: Per-request lookups. The cached path must never make one.
        self.resolve_calls = 0

    async def resolve_secret(self, repo_full_name: str):
        """The repository's own secret, or None when it has none."""
        self.resolve_calls += 1
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
    """A database seam that only counts the connections a request takes."""

    def __init__(self) -> None:
        self.acquisitions = 0

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[None]:
        self.acquisitions += 1
        yield None


class _State:
    container: SimpleNamespace


class _App:
    state = _State()


class _SnapshotLoader:
    """The reader the fake service refreshes from, built from the fake secret."""

    def __init__(self, webhook: _Webhook) -> None:
        self._webhook = webhook

    async def __call__(self) -> AuthData:
        if self._webhook.secret is None:
            return AuthData(repositories=frozenset({"acme/demo"}))
        return AuthData(
            repositories=frozenset({"acme/demo"}),
            webhook_secrets={
                "acme/demo": WebhookSecret(
                    repository="acme/demo", secret=self._webhook.secret, state="pushed"
                )
            },
        )


async def _loader_for(webhook: _Webhook) -> AuthData:
    return await _SnapshotLoader(webhook)()


def _container(webhook: _Webhook, **extra: object) -> SimpleNamespace:
    """A container shaped like the real one, for the route's own reads."""
    authorization = AuthorizationService(api_key="", ttl_seconds=30.0)
    authorization.attach_loader(_SnapshotLoader(webhook))
    return SimpleNamespace(
        webhook=webhook,
        authorization=authorization,
        settings=SimpleNamespace(auth=AuthSettings()),
        database=_CountingDatabase(),
        **extra,
    )


def _install(container: SimpleNamespace) -> None:
    _State.container = container


def _delivery(body: bytes = b'{"repository": {"full_name": "acme/demo"}}') -> Request:
    return _request(
        body,
        [
            (b"x-github-event", b"workflow_run"),
            (b"x-github-delivery", b"d-wired"),
            (b"x-hub-signature-256", b"sha256=deadbeef"),
            (b"content-type", b"application/json"),
        ],
    )


def test_the_route_reaches_its_own_signature_check() -> None:
    """A forged delivery is refused before anything reaches the database."""
    webhook = _Webhook()
    container = _container(webhook)
    _install(container)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(github_webhook(_delivery()))

    assert caught.value.status_code == 401
    assert webhook.processed == [], "a forged delivery reached the delivery handler"
    assert container.database.acquisitions == 0, "a refused delivery took a connection"
    assert webhook.recorded == [] and webhook.missing == [], "a refusal wrote to the ledger"


async def test_an_accepted_delivery_reaches_the_handler_with_its_repository() -> None:
    """The repository name the snapshot holds is the one processing is given."""
    import hashlib
    import hmac

    webhook = _Webhook()
    container = _container(webhook)
    _install(container)
    body = b'{"repository": {"full_name": "ACME/demo"}}'
    signature = b"sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest().encode()
    request = _request(
        body,
        [
            (b"x-github-event", b"workflow_run"),
            (b"x-github-delivery", b"d-real"),
            (b"x-hub-signature-256", signature),
        ],
    )

    result = await github_webhook(request)

    assert result["status"] == "processed"
    assert webhook.processed[0]["repository_full_name"] == "acme/demo"
    assert container.database.acquisitions == 1, "one connection for the delivery itself"


def _disconnected_request() -> Request:
    """A request whose client hangs up before the body arrives."""

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/webhooks/github",
        "raw_path": b"/webhooks/github",
        "query_string": b"",
        "headers": [(b"x-github-delivery", b"d-aborted")],
        "client": ("127.0.0.1", 1234),
        "server": ("tracker.test", 443),
        "app": _App(),
    }
    return Request(scope, receive=receive)


def test_a_client_that_hangs_up_mid_body_is_not_a_server_error() -> None:
    """GitHub abandons a send now and then; that is not a 500 to report.

    Left uncaught it surfaced as ``ClientDisconnect`` inside ``Exception in ASGI
    application`` -- one traceback per aborted delivery, all of it noise the
    operator cannot act on.
    """
    _install(_container(_Webhook()))

    result = asyncio.run(github_webhook(_disconnected_request()))

    assert result == {"status": "disconnected"}


def test_a_repository_without_a_secret_is_refused_for_that_reason() -> None:
    """No shared key exists any more, so "no secret" is its own answer.

    The operator's next step differs from a wrong signature: create one, rather
    than correct one. Recording the same reason for both is what made the
    distinction invisible.
    """
    webhook = _Webhook(secret=None)
    container = _container(webhook)
    _install(container)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(github_webhook(_delivery()))

    assert caught.value.status_code == 401
    assert container.database.acquisitions == 0
    assert webhook.missing == [], "the cached path reports the reason, it does not store it"


def test_the_cached_path_is_the_one_that_runs() -> None:
    """The container publishes an authorization service, and the route verifies from it.

    A route that quietly fell back to the per-request lookup would still verify
    deliveries correctly -- and would take a connection to do it, which is the
    one thing this change exists to stop. The count is the proof: with a snapshot
    loaded, ``resolve_secret`` is never called.
    """
    webhook = _Webhook()
    container = _container(webhook)
    _install(container)

    with pytest.raises(HTTPException):
        asyncio.run(github_webhook(_delivery()))

    assert webhook.resolve_calls == 0, "the cached path must not look the secret up per request"
    assert container.database.acquisitions == 0
