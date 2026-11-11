# tests/adapters/test_rest_auth_before_db.py
"""A bearer check must not cost a database connection.

The middleware order used to be the wrong way round: the connection scope was
the outermost layer, so a request with no key at all took a connection before
anyone looked at its ``Authorization`` header. An unauthenticated caller could
therefore hold a pooled connection for as long as they cared to send a body.

What these tests pin is the order, measured rather than described: the counting
database seam records every acquisition, and a request that fails its credential
check must leave it at zero.

The database here is a stub that counts and yields nothing, which is deliberate.
A route that gets *past* the check then fails on its models -- and that is fine:
the assertion is about what happened before the route ran, not about the route.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from conftest import FakeGithub
from fastapi.testclient import TestClient

from rhosocial.github_actions_tracker.adapters.rest.app import create_app
from rhosocial.github_actions_tracker.authorization import AuthorizationService
from rhosocial.github_actions_tracker.config import get_settings
from rhosocial.github_actions_tracker.container import Container

API_KEY = "test-api-key"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


class CountingDatabase:
    """A database seam that counts the connections a request takes."""

    def __init__(self) -> None:
        self.acquisitions = 0

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[None]:
        self.acquisitions += 1
        yield None

    async def ping(self, *, timeout: float = 2.0) -> None:
        return None


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):
    """The REST application over a counting database and a configured key.

    The key is read from the environment by ``get_settings``, which the container
    builds its authorization service from -- the same object the middleware
    consults, which is why this is a monkeypatch rather than a constructor
    argument. The settings are cached per process, so the cache is dropped before
    they are read.
    """
    monkeypatch.setenv("GHAT_API_KEY", API_KEY)
    get_settings.cache_clear()
    settings = get_settings()
    container = Container.build(database=CountingDatabase(), client=FakeGithub())  # type: ignore[arg-type]
    container.authorization = AuthorizationService.from_settings(settings)
    return create_app(container)


def _client(app) -> TestClient:
    """A client that reports what the server answered instead of re-raising it.

    ``raise_server_exceptions=False`` because the point of the "valid key" case
    is that the request got *past* authentication: past that point it reaches a
    route whose models have no backend, and the 500 that follows is the proof
    the scope was opened, not a failure of the assertion.
    """
    return TestClient(app, raise_server_exceptions=False)


def test_an_unauthenticated_request_takes_no_connection(app) -> None:
    with _client(app) as client:
        response = client.get("/api/v1/latest-run", params={"repository": "acme/demo"})

    assert response.status_code == 401
    assert app.state.container.database.acquisitions == 0


def test_a_wrong_key_takes_no_connection(app) -> None:
    with _client(app) as client:
        response = client.get(
            "/api/v1/latest-run",
            params={"repository": "acme/demo"},
            headers={"Authorization": "Bearer not-the-key"},
        )

    assert response.status_code == 403
    assert app.state.container.database.acquisitions == 0


def test_a_valid_key_is_what_opens_the_scope(app) -> None:
    """The proof the order is right: past the check, the scope is opened once."""
    with _client(app) as client:
        response = client.get(
            "/api/v1/latest-run",
            params={"repository": "acme/demo"},
            headers=AUTH,
        )

    assert response.status_code != 401, "a valid key passed the check"
    assert app.state.container.database.acquisitions == 1


def test_a_scrape_takes_no_connection(app) -> None:
    """/metrics is answered by the outermost middleware, before the scope.

    It used to acquire a connection on its way through, because the scope wrapped
    everything. A scrape is not a tool call and must not pay for one.
    """
    with _client(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert app.state.container.database.acquisitions == 0


def test_a_probe_takes_no_connection(app) -> None:
    with _client(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert app.state.container.database.acquisitions == 0


def test_a_probe_reports_the_snapshot_without_failing_on_it(app) -> None:
    """Readiness carries the snapshot's state, and only the webhook surface fails.

    A REST instance is not made unready by a snapshot it does not need: the
    static bearer key is what answers here.
    """
    with _client(app) as client:
        response = client.get("/readyz")

    body = response.json()
    assert response.status_code == 200
    assert body["checks"]["auth_cache"]["enabled"] is True
    assert body["checks"]["database"] == "ok"


def test_an_empty_key_is_the_open_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The local-development mode: no key configured, so any caller is allowed."""
    monkeypatch.setenv("GHAT_API_KEY", "")
    get_settings.cache_clear()
    container = SimpleNamespace(
        authorization=AuthorizationService.from_settings(get_settings()),
        database=CountingDatabase(),
        settings=get_settings(),
    )
    open_app = create_app(container)  # type: ignore[arg-type]

    with _client(open_app) as client:
        response = client.get("/api/v1/latest-run", params={"repository": "acme/demo"})

    assert response.status_code != 401
    # The adapter no longer owns a connection, so an open-mode request --
    # like every other one -- leaves the counting seam untouched.
    assert container.database.acquisitions == 0
