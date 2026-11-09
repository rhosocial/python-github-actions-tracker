# src/rhosocial/github_actions_tracker/adapters/rest/routes/webhooks.py
"""Webhook routes.

The order of this route is the whole point. A delivery is answered in four
steps, and only the last one touches the database:

1. the body is read, bounded in size and in time -- with no connection held;
2. the repository it names is parsed, and nothing else from the body is read;
3. the signature is verified from the in-memory authorization snapshot;
4. an accepted delivery opens its own connection scope and is processed.

Why the order matters: the middleware opens a per-request connection scope for
every other path, but not for this one. An unauthenticated caller -- one that
cannot produce a valid signature -- must not be able to hold a pooled connection
for as long as they care to stream a body, which is what ten slow deliveries did
to the webhook pool before this was reordered.

A refused delivery writes nothing to the ledger on the cached path. "Zero
database" is a property of the refusal, not a best effort: an attacker who can
make the service write one row per forged delivery has turned authentication
failure into a write amplifier. The refusal is counted and logged instead, and
the ledger keeps recording what it always did once a delivery *is* real.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.requests import ClientDisconnect

from ....authorization import AuthorizationService, WebhookDecision
from ....config import AuthSettings
from ....observability.definitions import AUTH_VERIFY_TOTAL

router = APIRouter()

logger = logging.getLogger(__name__)

#: HTTP status per authentication outcome. ``unavailable`` is the one that is
#: not the caller's fault: the process could not read its snapshot, so it cannot
#: tell a real delivery from a forged one. Answering 401 there would blame the
#: client for the service's own state, and would hide a dead reader.
STATUS_OF: dict[WebhookDecision, int] = {
    WebhookDecision.OK: 200,
    WebhookDecision.UNKNOWN_REPOSITORY: 401,
    WebhookDecision.SECRET_NOT_CONFIGURED: 401,
    WebhookDecision.BAD_SIGNATURE: 401,
    WebhookDecision.UNAVAILABLE: 503,
}

MESSAGE_OF: dict[WebhookDecision, str] = {
    WebhookDecision.UNKNOWN_REPOSITORY: "Unknown repository",
    WebhookDecision.SECRET_NOT_CONFIGURED: "No webhook secret for this repository",
    WebhookDecision.BAD_SIGNATURE: "Invalid webhook signature",
    WebhookDecision.UNAVAILABLE: "Webhook verification is unavailable",
}


class BodyTooLargeError(Exception):
    """The delivery body was larger than the configured limit."""


class BodyReadTimeoutError(Exception):
    """Reading the delivery body took longer than the configured budget."""


async def read_bounded_body(request: Request, *, limit: int, timeout: float) -> bytes:
    """Read the whole body, refusing one that is too big or too slow.

    A webhook body is attacker-controlled, so the receiver's contract with it is
    the opposite of trust: it may be enormous, and it may arrive one byte at a
    time. Reading it unbounded is what lets a single caller exhaust the process.
    The bound costs nothing for a real delivery, which GitHub caps at a few
    megabytes.

    Raises:
        BodyTooLargeError: The body exceeded ``limit`` bytes.
        BodyReadTimeoutError: It did not finish within ``timeout`` seconds.
        ClientDisconnect: The caller hung up before sending it.
    """
    chunks: list[bytes] = []
    size = 0

    async def pump() -> None:
        nonlocal size
        async for chunk in request.stream():
            if not chunk:
                continue
            size += len(chunk)
            if size > limit:
                # Stop reading: the answer is already decided, and continuing
                # would spend the very budget the limit exists to protect.
                raise BodyTooLargeError(limit)
            chunks.append(chunk)

    try:
        await asyncio.wait_for(pump(), timeout if timeout > 0 else None)
    except asyncio.TimeoutError as exc:
        raise BodyReadTimeoutError(timeout) from exc
    return b"".join(chunks)


def repository_full_name(payload: bytes) -> str:
    """The ``owner/repo`` a delivery names, or ``""`` when it cannot be read.

    Read *only* to choose which secret verifies the body; the body is otherwise
    untrusted until the signature check passes, which is why nothing else from
    this parse is returned or trusted.
    """
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    repository = parsed.get("repository")
    if not isinstance(repository, dict):
        return ""
    name = repository.get("full_name")
    return name.strip() if isinstance(name, str) else ""


def get_webhook_service(request: Request):
    """Dependency: the process's webhook controller.

    The controller carries the app's database seam, so work that outlives the
    request -- the eager log fetch of a failed job -- can take its own connection
    instead of borrowing the one the response is about to return.
    """
    return request.app.state.container.webhook


def _container_of(request: Request) -> Any:
    """The process's container, for its authorization service and its database."""
    return request.app.state.container


def _authorization_of(request: Request) -> AuthorizationService | None:
    """The process's authorization service, or ``None`` before one is wired."""
    return getattr(_container_of(request), "authorization", None)


def _auth_settings_of(request: Request) -> AuthSettings:
    """The deployment's auth settings, or the defaults for a bare container."""
    settings = getattr(_container_of(request), "settings", None)
    return getattr(settings, "auth", None) or AuthSettings()


def _database_of(request: Request) -> Any:
    """The process's database seam, when it has one."""
    return getattr(_container_of(request), "database", None)


def _count_verify(outcome: str) -> None:
    """Count one credential check. Never raises."""
    try:
        AUTH_VERIFY_TOTAL.inc(surface="webhook", outcome=outcome)
    except Exception as exc:  # noqa: BLE001 - a metric must not fail a request
        logger.debug("could not count a webhook verification: %s", exc)


def _refuse(decision: WebhookDecision, delivery_id: str) -> HTTPException:
    """The refusal for one outcome: counted and logged, never stored."""
    _count_verify(decision.value)
    logger.info(
        "Webhook delivery %s refused (%s)",
        delivery_id or "-",
        decision.value,
    )
    return HTTPException(status_code=STATUS_OF[decision], detail=MESSAGE_OF[decision])


def _signature_matches(payload: bytes, signature: str, secret: str) -> bool:
    """GitHub's HMAC, compared without leaking the expected value."""
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[7:])


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> dict:
    """Receive GitHub webhook events.

    This endpoint bypasses API key authentication and uses GitHub's HMAC
    signature verification instead.

    **The secret is per repository, and there is no shared fallback.** A delivery
    is verified with the secret registered for the repository it names; a
    repository with no secret is refused with ``secret_not_configured``, which
    is a different fact from ``bad_signature`` and a different operator action:
    register one, rather than correct one.

    A client that hangs up mid-body is not an error to report: GitHub does that
    when its own send times out, and the next attempt (or the next ``find_runs``)
    is what repairs the gap. Without this the disconnect surfaces as an
    unhandled ASGI exception and a traceback per aborted delivery, which crowds
    the log with something nobody can act on.

    With the cache disabled (``GHAT_AUTH_CACHE_TTL_SECONDS=0``) the route falls
    back to the per-request lookup it used before: one short connection scope
    around the secret read, and a refused delivery is still recorded in the
    ledger. That is the rollback path, kept working rather than removed.
    """
    auth_settings = _auth_settings_of(request)

    try:
        payload = await read_bounded_body(
            request,
            limit=auth_settings.webhook_max_body_bytes,
            timeout=auth_settings.webhook_read_timeout_seconds,
        )
    except BodyTooLargeError:
        raise HTTPException(status_code=413, detail="Webhook payload too large") from None
    except BodyReadTimeoutError:
        raise HTTPException(status_code=408, detail="Webhook body read timed out") from None
    except ClientDisconnect:
        logger.info(
            "Webhook delivery %s: client disconnected before the body arrived",
            request.headers.get("X-GitHub-Delivery", ""),
        )
        return {"status": "disconnected"}

    signature = request.headers.get("X-Hub-Signature-256", "")
    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    repository = repository_full_name(payload)
    service = get_webhook_service(request)
    authorization = _authorization_of(request)

    if authorization is not None and authorization.cache_enabled:
        repository = await _verify_cached(
            authorization, repository, payload, signature, delivery_id
        )
    else:
        repository = await _verify_by_lookup(
            request, service, repository, payload, signature, event_type, delivery_id
        )

    database = _database_of(request)
    result_args = {
        "event_type": event_type,
        "payload": payload,
        "delivery_id": delivery_id,
        "repository_full_name": repository,
    }
    if database is None:
        result = await service.process_webhook(**result_args)
    else:
        async with database.connection():
            result = await service.process_webhook(**result_args)
    return {"status": "processed", "event": event_type, **result}


async def _verify_cached(
    authorization: AuthorizationService,
    repository: str,
    payload: bytes,
    signature: str,
    delivery_id: str,
) -> str:
    """Verify a delivery from the snapshot, returning the repository to use.

    No connection is opened here on any path: the snapshot is in memory, and a
    refresh -- which is the only thing that could open one -- happens at most
    once per TTL, in one coroutine, while everyone else keeps the old snapshot.
    """
    verdict = await authorization.verify_webhook(repository, payload, signature)
    if not verdict.ok:
        raise _refuse(verdict.decision, delivery_id)
    _count_verify(WebhookDecision.OK.value)
    return verdict.repository


async def _verify_by_lookup(
    request: Request,
    service: Any,
    repository: str,
    payload: bytes,
    signature: str,
    event_type: str,
    delivery_id: str,
) -> str:
    """The pre-cache lookup, kept for ``GHAT_AUTH_CACHE_TTL_SECONDS=0``.

    The connection scope covers the secret read and nothing else -- not the body
    (already read above) and not the processing (opened by the caller after this
    returns). This is the rollback path: the same questions, asked per request.
    """
    database = _database_of(request)
    if database is None:
        resolved = await service.resolve_secret(repository)
    else:
        async with database.connection():
            resolved = await service.resolve_secret(repository)

    if resolved is None:
        await service.record_missing_secret(event_type or "unknown", delivery_id, repository)
        raise HTTPException(status_code=401, detail="No webhook secret for this repository")

    secret, name = resolved
    if not _signature_matches(payload, signature, secret):
        await service.record_signature_failure(event_type or "unknown", delivery_id)
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    _count_verify(WebhookDecision.OK.value)
    return name
