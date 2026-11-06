# tests/adapters/test_webhook_route_wiring.py
"""The webhook route must take its controller from the application's container.

``get_webhook_service`` needs the request to reach ``app.state.container`` (the
webhook controller carries the database seam, so the deferred log fetch can take
a connection of its own). The signature changed once and the call site did not,
so every delivery answered 500 with ``missing 1 required positional argument`` --
a failure the unit suite could not see, because the integration tests that drive
the real route are skipped without PostgreSQL.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from rhosocial.github_actions_tracker.adapters.rest.routes.webhooks import github_webhook


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
    """Refuses to be used: a forged delivery must not reach the database."""

    def __init__(self, secret: str | None = "s3cret") -> None:
        self.secret = secret
        self.recorded: list[tuple[str, str]] = []
        self.missing: list[tuple[str, str]] = []

    async def resolve_secret(self, repo_full_name: str):
        """The repository's own secret, or None when it has none."""
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

    async def process_webhook(self, **_: object) -> dict:
        raise AssertionError("a forged delivery reached the delivery handler")


class _State:
    container = SimpleNamespace(webhook=_Webhook())


class _App:
    state = _State()


def test_the_route_reaches_its_own_signature_check() -> None:
    """A forged delivery is refused before anything reaches the database."""
    _State.container = SimpleNamespace(webhook=_Webhook())
    request = _request(
        json.dumps({"action": "completed", "repository": {"full_name": "acme/demo"}}).encode(),
        [
            (b"x-github-event", b"workflow_run"),
            (b"x-github-delivery", b"d-forged"),
            (b"x-hub-signature-256", b"sha256=deadbeef"),
            (b"content-type", b"application/json"),
        ],
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(github_webhook(request))

    assert (
        caught.value.status_code == 401
    ), "an unexpected error here means the route cannot build its controller"
    assert _State.container.webhook.recorded == [("workflow_run", "d-forged")]


def test_a_repository_without_a_secret_is_refused_for_that_reason() -> None:
    """No shared key exists any more, so "no secret" is its own answer.

    The operator's next step differs from a wrong signature: create one, rather
    than correct one. Recording the same reason for both is what made the
    distinction invisible.
    """
    _State.container = SimpleNamespace(webhook=_Webhook(secret=None))
    request = _request(
        json.dumps({"action": "completed", "repository": {"full_name": "acme/demo"}}).encode(),
        [
            (b"x-github-event", b"workflow_run"),
            (b"x-github-delivery", b"d-unconfigured"),
            (b"x-hub-signature-256", b"sha256=deadbeef"),
        ],
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(github_webhook(request))

    assert caught.value.status_code == 401
    assert _State.container.webhook.missing == [("workflow_run", "d-unconfigured")]
    assert _State.container.webhook.recorded == [], "a missing secret is not a wrong signature"


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


def test_a_client_that_hangs_up_mid_body_is_not_a_server_error(monkeypatch) -> None:
    """GitHub abandons a send now and then; that is not a 500 to report.

    Left uncaught it surfaced as ``ClientDisconnect`` inside ``Exception in ASGI
    application`` -- one traceback per aborted delivery, all of it noise the
    operator cannot act on.
    """
    monkeypatch.setenv("GHAT_WEBHOOK_SECRET", "s3cret")

    result = asyncio.run(github_webhook(_disconnected_request()))

    assert result == {"status": "disconnected"}
