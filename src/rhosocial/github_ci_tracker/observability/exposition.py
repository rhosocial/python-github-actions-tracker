# src/rhosocial/github_ci_tracker/observability/exposition.py
"""``/metrics``, and the middleware that fills the registry.

Two things, one module, because they are two halves of one question: what a
scraper reads, and what the request path writes.

**The endpoint.** A plain ``GET /metrics``, answered with the Prometheus text
format and nothing else -- no authentication, because a scraper's credential is
network reachability, and a deployment that wants more keeps the port private;
no database, because every number here was counted on the request path and
waiting for a query would make the scrape cost what the work costs.

**The middleware.** Counts every request that carries a tool, by path template
and outcome, and times it. It deliberately does *not* count the probes or
``/metrics`` itself: a probe is not a tool call, and counting the scraper would
turn monitoring into load.

The shape is a plain ASGI middleware rather than a Starlette ``BaseHTTPMiddleware``
because both listeners need it -- the REST app is Starlette, the MCP listener is
the SDK's own ASGI app -- and one implementation answering ``(scope, receive,
send)`` is the one thing they both are.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .definitions import TOOL_CALLS_TOTAL, TOOL_SECONDS
from .registry import metrics

logger = logging.getLogger(__name__)

#: The path the exposition is served from.
METRICS_PATH = "/metrics"

#: Paths this middleware does not count: the probes, and itself.
_UNCOUNTED = (METRICS_PATH, "/livez", "/readyz", "/healthz", "/health")

#: How a path names itself in a metric label. Literal segments stay, and a
#: variable one becomes its name in braces -- the same shape a router uses, so
#: ``/runs/123`` and ``/runs/456`` are one series rather than one per run id.
_MAX_LABEL_SEGMENTS = 8


def _template_of(path: str) -> str:
    """A path's label shape, or ``""`` when it is not a tool path."""
    parts = [part for part in str(path).split("/") if part]
    rendered = []
    for part in parts[:_MAX_LABEL_SEGMENTS]:
        rendered.append(part if not part.isdigit() else "{id}")
    return "/" + "/".join(rendered)


def metrics_app_text() -> str:
    """The registry as one text block -- what ``/metrics`` answers with."""
    return metrics().render()


async def metrics_app(scope: Any, receive: Any, send: Any) -> None:
    """Answer ``/metrics`` and nothing else -- an ASGI app, not a middleware."""
    body = metrics_app_text().encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", "text/plain; version=0.0.4; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", "no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class MetricsMiddleware:
    """Count and time each request, and answer ``/metrics`` from the registry.

    Args:
        app: The ASGI app to wrap.
        protocol: Which listener this is -- a label, so one chart can show both.
    """

    def __init__(self, app: Any, protocol: str) -> None:
        self._app = app
        self._protocol = protocol

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))

        if method in ("GET", "HEAD") and path == METRICS_PATH:
            await metrics_app(scope, receive, send)
            return
        if path in _UNCOUNTED:
            await self._app(scope, receive, send)
            return

        status_holder = {"code": 0}

        async def counted(message: Any) -> None:
            if message.get("type") == "http.response.start":
                status_holder["code"] = int(message.get("status", 0) or 0)
            await send(message)

        started = time.monotonic()
        try:
            await self._app(scope, receive, counted)
        finally:
            _record(
                path=path,
                method=method,
                status=status_holder["code"],
                protocol=self._protocol,
                seconds=time.monotonic() - started,
            )


def _record(*, path: str, method: str, status: int, protocol: str, seconds: float) -> None:
    """Add one request to the registry, or say why it could not be added.

    Never raises. A metric must not be the reason a request fails, and the
    request has already answered by the time this runs -- the only thing left to
    protect is the *next* one.
    """
    try:
        outcome = "ok" if 200 <= status < 500 else "error"
        tool = _template_of(path) or method.lower()
        TOOL_CALLS_TOTAL.inc(tool=tool, protocol=protocol, outcome=outcome)
        TOOL_SECONDS.observe(seconds, tool=tool, protocol=protocol, outcome=outcome)
    except Exception as exc:  # noqa: BLE001 - a metric must not fail a request
        logger.debug("could not record %s: %s", path, exc)


__all__ = [
    "METRICS_PATH",
    "MetricsMiddleware",
    "logger",
    "metrics_app",
]
