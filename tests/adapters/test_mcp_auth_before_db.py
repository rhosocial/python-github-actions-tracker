# tests/adapters/test_mcp_auth_before_db.py
"""The MCP listener's credential check happens before its connection.

``serve mcp`` is its own process with its own pool, and the wrapper that binds a
connection to every request used to be the *first* thing that ran. A caller with
no bearer token therefore held a pooled connection for as long as the SDK took
to work out that they had none -- the same shape the REST listener had, in a
process where that pool serves every tool call.

The static-bearer comparison is an in-memory ``hmac.compare_digest``, so moving
it ahead of the pool costs nothing. The SDK still verifies the request itself
afterwards; that is defense in depth, not redundancy.

The pool is patched in every case, including the ones expected to refuse: a test
that reached the real one would spend its life waiting for a server that is not
there instead of failing the assertion it was written for.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from rhosocial.github_actions_tracker.adapters.mcp import server as mcp_server
from rhosocial.github_actions_tracker.adapters.mcp.server import (
    _DatabaseBoundApp,
    _UnboundContainer,
)

db_module = importlib.import_module("rhosocial.github_actions_tracker.database")


class CountingDatabase:
    """A database seam that counts the connections a request takes."""

    def __init__(self) -> None:
        self.acquisitions = 0

    @asynccontextmanager
    async def connection(self):
        self.acquisitions += 1
        yield object()

    async def close(self) -> None:
        return None


class _Inner:
    """The SDK's app: it must not be reached by an unauthenticated request."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> CountingDatabase:
    """A counting pool, installed where the app would open its own."""
    counting = CountingDatabase()

    async def fake_create(cls, settings: Any = None) -> CountingDatabase:
        return counting

    monkeypatch.setattr(db_module.Database, "create", classmethod(fake_create))
    return counting


def _scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers or [],
        "scheme": "https",
    }


def _probe_scope(path: str) -> dict[str, Any]:
    return {"type": "http", "method": "GET", "path": path, "headers": [], "scheme": "https"}


async def _receive() -> dict[str, Any]:
    return {"type": "http.request"}


async def _noop(message: dict[str, Any]) -> None:
    return None


def _verify(token: str) -> bool:
    """The same check :class:`AuthorizationService` performs, by hand."""
    import hmac

    return hmac.compare_digest(token.encode(), b"the-key")


def _app(
    inner: _Inner,
    verify_bearer: Callable[[str], bool] | None = _verify,
    *,
    bound: bool = True,
) -> Any:
    """The listener under test, with its services already built when asked.

    ``bound=False`` is the cold-start case: the first request builds the
    container, which takes connections of its own. Every other case binds first,
    so the count is only what the request itself did.
    """
    services = _UnboundContainer()
    if bound:
        services.bind(_built_container())
    return _DatabaseBoundApp(inner, services, verify_bearer=verify_bearer)


def _built_container() -> SimpleNamespace:
    """A container the lazy startup would have built, already in place."""
    return SimpleNamespace(readiness=_readiness, bound=True)


async def _readiness(**kwargs: Any) -> dict[str, Any]:
    return {"ready": True, "checks": {"database": "ok"}}


def _run(app: Any, scope: dict[str, Any]) -> dict[str, Any]:
    """Drive one request and return the response it started, as ``{"status": N}``."""
    started: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            started.append(message)

    asyncio.run(app(scope, _receive, send))
    return {"status": started[0]["status"]} if started else {}


# -- when the check runs ------------------------------------------------------


def test_without_a_verifier_the_request_passes_through(database: CountingDatabase) -> None:
    """An OAuth deployment has no static key, so this layer must not guess.

    The SDK is the authority there; a wrapper that second-guessed a token it
    cannot read would refuse every legitimate OAuth request.
    """
    inner = _Inner()
    app = _app(inner, verify_bearer=None)

    asyncio.run(app(_scope(), _receive, _noop))

    assert inner.calls == 1
    assert database.acquisitions == 1, "the request itself, and nothing else"


def test_a_request_without_a_bearer_is_refused_before_the_pool(database: CountingDatabase) -> None:
    inner = _Inner()
    app = _app(inner)

    assert _run(app, _scope()) == {"status": 401}
    assert inner.calls == 0
    assert database.acquisitions == 0, "a refused request must not take a connection"


def test_a_request_with_the_wrong_key_is_refused_before_the_pool(
    database: CountingDatabase,
) -> None:
    inner = _Inner()
    app = _app(inner)

    answered = _run(app, _scope([(b"authorization", b"Bearer not-the-key")]))

    assert answered == {"status": 401}
    assert inner.calls == 0
    assert database.acquisitions == 0


def test_a_request_with_the_right_key_reaches_the_inner_app(database: CountingDatabase) -> None:
    inner = _Inner()
    app = _app(inner)

    answered = _run(app, _scope([(b"authorization", b"Bearer the-key")]))

    assert answered == {}, "the inner app answers, not this wrapper"
    assert inner.calls == 1
    assert database.acquisitions == 1


def test_a_malformed_authorization_header_is_refused_before_the_pool(
    database: CountingDatabase,
) -> None:
    """A header without the ``Bearer `` scheme is not a credential at all."""
    inner = _Inner()
    app = _app(inner)

    answered = _run(app, _scope([(b"authorization", b"Basic dXNlcjpwYXNz")]))

    assert answered == {"status": 401}
    assert inner.calls == 0
    assert database.acquisitions == 0


# -- the probes, which are exempt by necessity ---------------------------------


def test_a_probe_is_not_subject_to_the_bearer_check(database: CountingDatabase) -> None:
    """Liveness must answer for an orchestrator that holds no credential."""
    inner = _Inner()
    app = _app(inner)

    assert _run(app, _probe_scope("/livez")) == {"status": 200}
    assert database.acquisitions == 0


def test_the_mcp_readiness_names_its_surface() -> None:
    """MCP answers with the static key, so its readiness is not the webhook's.

    The webhook receiver can be unable to answer when its snapshot never loaded;
    this one cannot, and reporting the same failure there would take the listener
    out of rotation for something it does not depend on.
    """
    asked: list[str] = []

    async def readiness(**kwargs: Any) -> dict[str, Any]:
        asked.append(kwargs.get("surface", ""))
        return {"ready": True, "checks": {"database": "ok"}}

    app = _DatabaseBoundApp(_Inner(), SimpleNamespace(bound=True, readiness=readiness))

    assert _run(app, _probe_scope("/healthz")) == {"status": 200}
    assert asked == ["mcp"]


def test_a_probe_is_answered_before_the_pool_is_opened(database: CountingDatabase) -> None:
    """A readiness probe with services already built takes no connection at all.

    The first request of a cold process *does* build the container -- a probe is
    often the first thing a load balancer sends, and answering "not ready until
    somebody calls a tool" would keep the instance out of rotation forever. What
    must not happen is the probe taking a connection of its own every time: this
    is the second probe, and it answers from the report it is handed.
    """
    app = _app(_Inner())

    assert _run(app, _probe_scope("/readyz")) == {"status": 200}
    assert database.acquisitions == 0


def test_the_static_bearer_mode_is_the_one_that_pre_authenticates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_mcp_app`` wires the pre-auth only in static-bearer mode.

    Under OAuth the SDK is the authority, so the wrapper is left out of it; under
    no credential at all there is nothing to compare. Getting this wrong would
    either refuse every OAuth request or admit an unauthenticated one.
    """
    monkeypatch.setenv("GHAT_API_KEY", "the-key")
    app = mcp_server.create_mcp_app(container=None)

    assert isinstance(app, _DatabaseBoundApp)
    assert app._verify_bearer is not None
    assert app._verify_bearer("the-key") is True
    assert app._verify_bearer("wrong") is False
