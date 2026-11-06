# src/rhosocial/github_actions_tracker/adapters/cli/commands/serve.py
"""rhosocial-github-actions-tracker serve command."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from typing import Any

from rhosocial.activerecord.backend.impl.postgres.async_support import run_coroutine

log = logging.getLogger(__name__)


def _dual_stack_socket(host: str, port: int) -> socket.socket:
    """Bind the wildcard IPv6 address so it also accepts IPv4.

    ``host="::"`` reads as "every address" to anyone configuring a listener, and
    on Linux a socket bound there is normally dual-stack. It is not on every
    kernel: ``IPV6_V6ONLY`` defaults to 1 in some container namespaces, and
    uvicorn does not touch the flag, so the server ends up reachable on IPv6 only
    and an IPv4 client is refused with "connection refused" -- which looks like
    the service is down rather than bound to the wrong family.

    Clearing the flag before the bind is the difference between "IPv6" and
    "both". A kernel that refuses the option (it is not settable everywhere) is
    left alone: making the listen fail for a nicety would be worse than the
    narrow binding.
    """
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    except OSError:
        log.debug("this kernel refuses to clear IPV6_V6ONLY; binding IPv6 only")
    sock.bind((host, port))
    return sock


def run(args: argparse.Namespace, api_key: str) -> None:
    """Execute the serve command.

    Args:
        args: Parsed command-line arguments
        api_key: API key for authentication
    """
    from ....config import get_settings

    settings = get_settings().service
    _resolve_bind_defaults(args, settings)

    if args.service == "rest":
        _serve_rest(args, settings)
    elif args.service == "mcp":
        _serve_mcp(args, settings)
    elif args.service == "webhook":
        _serve_webhook(args, settings)
    elif args.service == "all":
        _serve_all(args, settings)


def _resolve_bind_defaults(args: argparse.Namespace, settings: Any) -> None:
    """Fill unspecified --host/--port from the configured values."""
    defaults = {
        "rest": (settings.rest_host, settings.rest_port),
        "mcp": (settings.mcp_host, settings.mcp_port),
        "webhook": (settings.webhook_host, settings.webhook_port),
        "all": (settings.rest_host, settings.rest_port),
    }
    host, port = defaults.get(args.service, (settings.rest_host, settings.rest_port))
    if args.host is None:
        args.host = host
    if args.port is None:
        args.port = port


def _serve(
    coro_factory,
    host: str,
    port: int,
    *,
    ssl_certfile: str = "",
    ssl_keyfile: str = "",
) -> None:
    """Run a uvicorn server on a loop psycopg accepts on Windows.

    psycopg cannot drive sockets on the ProactorEventLoop, so the server runs on
    a SelectorEventLoop. Which loop that is, and how it is installed and closed,
    belongs to the backend's ``async_support`` module -- it is the same question
    the backend's own CLIs answer, and one answer here would drift from theirs.
    Uvicorn is driven with its ``Server.serve()`` API instead of the blocking
    ``uvicorn.run()`` (which installs its own loop and would undo the choice).

    ``ssl_certfile`` / ``ssl_keyfile`` are a full-chain PEM and its key; both
    empty (the default) serves plain HTTP, which is what the local-only REST
    and MCP listeners use. The webhook listener needs them because GitHub
    refuses an http callback URL.
    """
    import uvicorn

    config = uvicorn.Config(
        app=coro_factory(),
        host=host,
        port=port,
        log_level="info",
        ssl_certfile=ssl_certfile or None,
        ssl_keyfile=ssl_keyfile or None,
    )
    server = uvicorn.Server(config)

    # ``::`` means both families; see :func:`_dual_stack_socket`. Every other
    # host (including 0.0.0.0) keeps uvicorn's own binding.
    sockets = [_dual_stack_socket(host, port)] if ":" in host else None
    run_coroutine(server.serve(sockets=sockets))


def _serve_rest(args: argparse.Namespace, settings: Any) -> None:
    """Start REST API server."""
    from ...rest.app import create_app

    _warn_if_unauthenticated(settings)
    _serve(
        create_app,
        args.host,
        args.port,
        ssl_certfile=settings.rest_ssl_certfile,
        ssl_keyfile=settings.rest_ssl_keyfile,
    )


def _warn_if_unauthenticated(settings: Any) -> None:
    """Announce that the REST surface is open.

    Empty authorization is a supported local-development mode; the warning is
    what keeps it from being discovered in production by a stranger.
    """
    if not settings.api_key:
        log.warning(
            "REST API authentication is disabled: GHAT_API_KEY is empty, "
            "so any caller can use /api/v1. Set a key before exposing this "
            "port publicly."
        )


def _serve_mcp(args: argparse.Namespace, settings: Any) -> None:
    """Start MCP server.

    The MCP listener can terminate TLS as well: a deployment that gives it its
    own name and certificate wants the agents' traffic encrypted too, and the
    alternative -- publishing a plain-HTTP admin surface to the internet --
    is worse than one more certificate path in the environment.
    """
    from ...mcp.server import create_mcp_app

    _serve(
        create_mcp_app,
        args.host,
        args.port,
        ssl_certfile=settings.mcp_ssl_certfile,
        ssl_keyfile=settings.mcp_ssl_keyfile,
    )


def _serve_webhook(args: argparse.Namespace, settings: Any) -> None:
    """Start webhook receiver server (same app as REST, different port).

    The receiver terminates TLS itself: GitHub delivers only to an https URL
    with a publicly trusted certificate, so the configured full-chain PEM and
    its key are handed to uvicorn here. Without them the listener serves plain
    HTTP and GitHub refuses to register the callback.
    """
    from ...rest.app import create_app

    _serve(
        create_app,
        args.host,
        args.port,
        ssl_certfile=settings.webhook_ssl_certfile,
        ssl_keyfile=settings.webhook_ssl_keyfile,
    )


def _serve_all(args: argparse.Namespace, settings: Any) -> None:
    """Start the REST API and webhook receiver on separate ports.

    REST stays plain HTTP on purpose -- it is a local-only surface. The webhook
    receiver carries the TLS material GitHub demands for its callback URL.
    """
    import uvicorn

    from ...rest.app import create_app

    _warn_if_unauthenticated(settings)

    async def serve_both() -> None:
        from ....container import Container
        from ....database import Database

        # One database, one container, two servers. uvicorn runs the lifespan
        # once per server, so handing both the same container is what keeps one
        # pool and one set of controllers: a second startup would otherwise build
        # its own while the first server is serving.
        database = await Database.create()
        try:
            async with database.connection():
                container = await Container.start(database)
            rest_config = uvicorn.Config(
                create_app(container),
                host=settings.rest_host,
                port=settings.rest_port,
                log_level="info",
            )
            webhook_config = uvicorn.Config(
                create_app(container),
                host=settings.webhook_host,
                port=settings.webhook_port,
                log_level="info",
                ssl_certfile=settings.webhook_ssl_certfile or None,
                ssl_keyfile=settings.webhook_ssl_keyfile or None,
            )
            # Both listeners take the dual-stack treatment for the same reason
            # ``_serve`` does: ``::`` is meant to mean "every address", and a
            # kernel that defaults IPV6_V6ONLY to 1 would otherwise leave the
            # IPv4 half closed.
            rest_sockets = (
                [_dual_stack_socket(settings.rest_host, settings.rest_port)]
                if ":" in settings.rest_host
                else None
            )
            webhook_sockets = (
                [_dual_stack_socket(settings.webhook_host, settings.webhook_port)]
                if ":" in settings.webhook_host
                else None
            )
            await asyncio.gather(
                uvicorn.Server(rest_config).serve(sockets=rest_sockets),
                uvicorn.Server(webhook_config).serve(sockets=webhook_sockets),
            )
        finally:
            await database.close()

    run_coroutine(serve_both())
