# src/rhosocial/github_actions_tracker/adapters/rest/middleware/auth.py
"""API Key authentication middleware for REST API.

The check itself lives in :mod:`rhosocial.github_actions_tracker.authorization`:
one comparison, in one place, for REST and MCP alike, done on bytes so a
non-ASCII token is refused instead of raising. What this middleware adds is only
the routing of the question -- which paths need a key, and which are exempt.

Webhook endpoints (under /webhooks/) bypass API key validation
and use GitHub signature verification instead.
Health/readiness endpoints are exempt.

An empty ``GHAT_API_KEY`` disables REST authentication entirely: the
local-development mode the CLI warns about at startup. Requiring a header
that compares against "" would accept ``Authorization: Bearer `` and reject
an absent header, which is neither authenticated nor open.

Note: the middleware returns ``JSONResponse`` instead of raising
``HTTPException``. Under ``BaseHTTPMiddleware`` a raised exception escapes
the exception-handler chain and surfaces as a 500, so the status code is
produced directly.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

__all__ = ["ApiKeyMiddleware"]


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """API Key authentication middleware.

    Validates the Bearer token in the Authorization header against the static
    operator key the process was configured with.

    The key comes from the application's authorization service when the app
    published one, so the REST listener and the MCP listener compare against
    the same value; the environment variable is the fallback for an app that
    never wired a container (a unit test driving the middleware directly).
    """

    #: Probes, unauthenticated by necessity: an orchestrator holds no API key,
    #: and a 401 on a probe path reads as "the service is down".
    HEALTH_PATHS = frozenset({"/health", "/healthz", "/livez", "/readyz"})

    def __init__(self, app: object, api_key: str | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.api_key = api_key or os.getenv("GHAT_API_KEY", "")

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Process the request through the middleware."""
        # Webhook endpoints use signature verification, skip API key check
        if request.url.path.startswith("/webhooks/"):
            return await call_next(request)

        # Health check endpoints require no authentication
        if request.url.path in self.HEALTH_PATHS:
            return await call_next(request)

        # Empty authorization is a supported local-development mode. The
        # protocol endpoint is then open, and the CLI logs a warning at start.
        # Comparing against an empty key would instead accept "Bearer " and
        # reject an absent header, which is neither authenticated nor open.
        if not self._configured(request):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return self._unauthorized("Missing or invalid Authorization header")

        token = auth_header[len("Bearer ") :]
        if not self._verify(request, token):
            return self._forbidden("Invalid API key")

        return await call_next(request)

    def _configured(self, request: Request) -> bool:
        """Whether this deployment answers with a key at all."""
        authorization = _authorization_of(request)
        if authorization is not None:
            return authorization.api_key_configured
        return bool(self.api_key)

    def _verify(self, request: Request, token: str) -> bool:
        """Compare a presented bearer token against the configured key.

        Delegated to the authorization service when the application published
        one, so there is exactly one implementation of the question -- and it is
        a constant-time comparison on bytes, which the ``!=`` this replaces was
        not. The fallback keeps this middleware testable on its own, with no
        application wired.
        """
        authorization = _authorization_of(request)
        if authorization is not None:
            return authorization.verify_bearer(token)
        from ....authorization import AuthorizationService

        return AuthorizationService(api_key=self.api_key).verify_bearer(token)

    @staticmethod
    def _unauthorized(message: str) -> JSONResponse:
        return JSONResponse(status_code=401, content={"error": "unauthorized", "message": message})

    @staticmethod
    def _forbidden(message: str) -> JSONResponse:
        return JSONResponse(status_code=403, content={"error": "forbidden", "message": message})


def _authorization_of(request: Request):
    """The application's authorization service, or ``None`` before one exists."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    container = getattr(state, "container", None)
    return getattr(container, "authorization", None)
