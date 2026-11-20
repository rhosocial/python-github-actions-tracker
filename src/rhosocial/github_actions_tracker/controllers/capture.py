# src/rhosocial/github_actions_tracker/controllers/capture.py
"""A bounded, in-process executor for log capture after a webhook delivery.

GitHub acknowledges a delivery when the request is answered, so nothing that
downloads an archive or writes many rows may happen on that path. This module
holds the work instead: a webhook adds one small request in constant time, and a
single drain task performs them with a fixed concurrency.

Three properties are deliberate:

* **Coalescing** -- a request is keyed by run or job, so a redelivery of the same
  event cannot queue a second download of the same archive.
* **Bounded** -- pending plus in-flight work is capped. When the cap is hit the
  oldest pending request is dropped, counted and *logged*, not silently lost.
* **Best effort** -- the delivery ledger already records that GitHub was heard.
  A capture dropped here is repaired by a read inside the one-hour archive
  window; after that, the step split is simply unavailable, which was always
  the contract.

There is no table, no polling and no restart promise. What survives a restart
is the delivery and the read path, which is why this is not the task queue the
architecture rules forbid.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..config import CaptureSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureRequest:
    """One piece of work, keyed so duplicates coalesce.

    ``payload`` is passed to the handler registered for ``kind``; it is a small
    tuple of already-loaded rows, because the task starts within seconds and a
    re-read would buy nothing.
    """

    kind: str
    key: str
    payload: Any


@dataclass(frozen=True)
class CaptureHealth:
    """What ``ci_status`` reports about the executor."""

    pending: int
    in_flight: int
    dropped: int
    outcomes: dict[str, int]


class CaptureQueue:
    """Coalesce and execute webhook-triggered captures under a fixed limit."""

    def __init__(
        self,
        settings: CaptureSettings,
        handlers: dict[str, Callable[[Any], Awaitable[None]]],
        *,
        enabled: bool = True,
    ) -> None:
        self._settings = settings
        self._handlers = dict(handlers)
        self._enabled = enabled
        self._pending: OrderedDict[str, CaptureRequest] = OrderedDict()
        self._in_flight: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._closing = False
        self._task: asyncio.Task[None] | None = None
        self._dropped = 0
        self._outcomes: Counter[str] = Counter()

    # -- submit ---------------------------------------------------------------

    def submit(self, request: CaptureRequest) -> None:
        """Add one request without waiting, or drop it if the queue is full."""
        if not self._enabled or self._closing or request.kind not in self._handlers:
            self._dropped += 1
            logger.warning("Capture queue is closed; deferred to read path: %s", request.key)
            return
        if request.key in self._pending or request.key in self._in_flight:
            return

        total = len(self._pending) + len(self._in_flight)
        if total >= self._settings.max_pending:
            oldest, _ = self._pending.popitem(last=False)
            self._dropped += 1
            logger.warning(
                "Capture queue full (%d); dropped '%s' (kept '%s'); deferred to read path",
                total,
                oldest,
                request.key,
            )

        self._pending[request.key] = request

        # The drain may be waiting on the event; a fresh context keeps a webhook
        # request's connection binding out of the tasks it creates.
        def wake() -> None:
            self._wake.set()

        contextvars.Context().run(wake)

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Start the drain task in an empty context, safe to call more than once.

        ``asyncio.create_task`` copies the calling context. A drain started from
        a webhook request would inherit its connection binding, and every
        handler task would inherit it in turn and keep using it after the
        response returned the connection to the pool. Starting in a fresh
        context makes all descendants clean.
        """
        if self._task is None:

            def start() -> None:
                self._task = asyncio.create_task(self._drain())

            contextvars.Context().run(start)
            logger.debug("Capture queue started (concurrency=%d)", self._settings.concurrency)

    async def drain(self) -> None:
        """Stop accepting work and let in-flight captures finish or be cancelled."""
        self._closing = True
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self._settings.drain_timeout_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        if self._in_flight:
            tasks = list(self._in_flight.values())
            done, pending = await asyncio.wait(tasks, timeout=self._settings.drain_timeout_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result() if not task.cancelled() else None

    def snapshot(self) -> CaptureHealth:
        """Current numbers for the health answer."""
        return CaptureHealth(
            pending=len(self._pending),
            in_flight=len(self._in_flight),
            dropped=self._dropped,
            outcomes=dict(self._outcomes),
        )

    # -- internals ------------------------------------------------------------

    async def _drain(self) -> None:
        """Take pending work while there is capacity; otherwise wait for an event."""
        try:
            while True:
                while self._pending and len(self._in_flight) < self._settings.concurrency:
                    key, request = self._pending.popitem(last=False)
                    task = asyncio.create_task(self._execute(key, request))
                    self._in_flight[key] = task
                    task.add_done_callback(lambda _t, k=key: self._in_flight.pop(k, None))

                if self._in_flight:
                    await asyncio.wait(
                        list(self._in_flight.values()),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    continue

                if self._closing:
                    return
                self._wake.clear()
                if not self._pending:
                    await self._wake.wait()
        except asyncio.CancelledError:
            raise

    async def _execute(self, key: str, request: CaptureRequest) -> None:
        """Run one handler; a failure is a counted outcome, never a dead drain."""
        try:
            await self._handlers[request.kind](request.payload)
        except asyncio.CancelledError:
            self._outcomes["cancelled"] += 1
            raise
        except Exception as exc:  # noqa: BLE001 - best-effort side effect
            reason = type(exc).__name__
            self._outcomes[reason] += 1
            logger.warning("Capture %s failed: %s: %s", key, reason, exc)
        else:
            self._outcomes["ok"] += 1


__all__ = ["CaptureHealth", "CaptureQueue", "CaptureRequest"]
