"""Probe: does holding a connection across a wait break other requests?

Two endpoints, same work, different order:

* ``/request-scope``:  borrow a connection, wait 8 seconds (standing in for a
  GitHub call), then read the database, then give it back.
* ``/operation-scope``: wait 8 seconds first, then borrow, read, give back.

The pool is capped at 2 connections. Ten requests are fired at once, so with the
first shape ten requests want two connections for eight seconds each and queue
in waves; with the second, all ten wait at the same time and only touch the
database briefly.

What is measured: how many requests succeed, how long they take, and the pool's
own acquire-timeout counter. No rows are written -- the read is a COUNT.

Usage:
    python scripts/probe_connection_scope.py
    python scripts/probe_connection_scope.py --requests 20 --hold 5
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import statistics
import time
from typing import Any

import httpx
from fastapi import FastAPI

POOL_SIZE = 2
HOLD_SECONDS = 8.0


def _settings_with_small_pool() -> Any:
    """The deployment's settings, with the pool cut down to POOL_SIZE."""
    from rhosocial.github_actions_tracker.config import PoolSettings, get_settings

    settings = get_settings()
    return dataclasses.replace(
        settings,
        pool=PoolSettings(min_size=1, max_size=POOL_SIZE),
    )


def _build_app(database: Any) -> FastAPI:
    """One app, two routes, both reading the same row count."""
    from rhosocial.github_actions_tracker.models import AsyncRepository

    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """No database at all, so it must answer even when the pool is empty."""
        return {"ok": True}

    @app.get("/request-scope")
    async def request_scope() -> dict[str, Any]:
        """Borrow, then wait, then read: the tracker's current shape."""
        async with database.connection():
            await asyncio.sleep(HOLD_SECONDS)
            count = await AsyncRepository.query().count()
        return {"count": count}

    @app.get("/operation-scope")
    async def operation_scope() -> dict[str, Any]:
        """Wait first; borrow only for the read."""
        await asyncio.sleep(HOLD_SECONDS)
        async with database.connection():
            count = await AsyncRepository.query().count()
        return {"count": count}

    return app


async def _fire(app: FastAPI, path: str, count: int) -> dict[str, Any]:
    """Send ``count`` requests at the same moment and report what came back."""

    async def one(index: int) -> tuple[int, float, str]:
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://probe",
                timeout=120.0,
            ) as client:
                response = await client.get(path)
                return response.status_code, time.perf_counter() - started, ""
        except Exception as exc:  # a client-side timeout is a failed request too
            return 0, time.perf_counter() - started, type(exc).__name__

    results = await asyncio.gather(*(one(i) for i in range(count)))
    ok = [r for r in results if r[0] == 200]
    failed = [r for r in results if r[0] != 200]
    return {
        "ok": len(ok),
        "failed": len(failed),
        "elapsed": round(max(r[1] for r in results), 1),
        "median": round(statistics.median(r[1] for r in results), 1),
        "error_names": sorted({f"{r[0]} {r[2]}".strip() for r in failed}),
    }


async def _variant(path: str, requests: int) -> None:
    """Fresh pool, fresh app, one shape."""
    from rhosocial.github_actions_tracker.database import Database

    database = await Database.create(_settings_with_small_pool())
    app = _build_app(database)
    try:
        stats = database.pool.get_stats()
        print(f"  pool: available={stats.current_available} created={stats.total_created}")
        result = await _fire(app, path, requests)
        stats_after = database.pool.get_stats()
        print(
            f"  {path}: ok={result['ok']} failed={result['failed']} "
            f"slowest={result['elapsed']}s median={result['median']}s"
        )
        if result["error_names"]:
            print(f"    failures: {', '.join(result['error_names'])}")
        print(
            f"    pool after: available={stats_after.current_available} "
            f"in_use={stats_after.current_in_use} "
            f"acquire_timeouts={stats_after.total_timeouts}"
        )
    finally:
        await database.close()


async def main_async(requests: int) -> None:
    print(f"{requests} concurrent requests, pool of {POOL_SIZE}, each holds {HOLD_SECONDS}s")
    print("\nA. borrow -> wait -> read   (the tracker's current shape)")
    await _variant("/request-scope", requests)
    print("\nB. wait -> borrow -> read   (the library's FastAPI example)")
    await _variant("/operation-scope", requests)


def main(argv: list[str] | None = None) -> int:
    global HOLD_SECONDS
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--hold", type=float, default=HOLD_SECONDS)
    args = parser.parse_args(argv)
    HOLD_SECONDS = args.hold

    from rhosocial.activerecord.backend.impl.postgres.async_support import run_coroutine

    run_coroutine(main_async(args.requests))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
