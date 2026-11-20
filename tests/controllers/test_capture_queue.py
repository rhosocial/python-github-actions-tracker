# tests/controllers/test_capture_queue.py
"""The webhook capture executor: bounded, coalescing, best effort.

These tests use no database: the queue is a pure in-memory mechanism and its
handlers are fakes. What matters is the policy -- duplicates collapse, the
pending set does not grow without limit, concurrency has a hard cap, a failing
handler does not kill the drain, and shutdown waits for work in progress.
"""

from __future__ import annotations

import asyncio

from rhosocial.github_actions_tracker.config import CaptureSettings
from rhosocial.github_actions_tracker.controllers.capture import CaptureQueue, CaptureRequest


def _settings(**overrides: object) -> CaptureSettings:
    base = CaptureSettings(concurrency=2, max_pending=1000, drain_timeout_seconds=2.0)
    return CaptureSettings(**{**base.__dict__, **overrides})


async def _settle() -> None:
    """Let a drain iteration take queued work and finish it."""
    await asyncio.sleep(0.05)


async def test_duplicate_keys_coalesce_into_one_handler_call() -> None:
    calls: list[str] = []

    async def handler(payload: object) -> None:
        calls.append(str(payload))

    queue = CaptureQueue(_settings(), {"run": handler})
    await queue.start()
    queue.submit(CaptureRequest("run", "run:1", "one"))
    queue.submit(CaptureRequest("run", "run:1", "two"))
    await _settle()
    await queue.drain()

    assert calls == ["one"]


async def test_concurrency_is_capped() -> None:
    active = 0
    peak = 0

    async def handler(payload: object) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1

    queue = CaptureQueue(_settings(concurrency=2), {"run": handler})
    await queue.start()
    for number in range(10):
        queue.submit(CaptureRequest("run", f"run:{number}", number))
    await asyncio.sleep(0.2)
    await queue.drain()

    assert peak == 2


async def test_overflow_drops_the_oldest_and_counts_it() -> None:
    calls: list[int] = []

    async def handler(payload: object) -> None:
        calls.append(int(payload))

    queue = CaptureQueue(_settings(concurrency=1, max_pending=2), {"run": handler})
    # Not started: everything stays pending, so the cap is visible immediately.
    for number in range(5):
        queue.submit(CaptureRequest("run", f"run:{number}", number))

    health = queue.snapshot()
    assert health.dropped == 3
    assert health.pending == 2


async def test_a_failing_handler_is_counted_and_drain_survives() -> None:
    calls = 0

    async def broken(payload: object) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("github refused")

    async def healthy(payload: object) -> None:
        return None

    queue = CaptureQueue(_settings(), {"run": broken, "job": healthy})
    await queue.start()
    queue.submit(CaptureRequest("run", "run:1", 1))
    queue.submit(CaptureRequest("job", "job:1", 1))
    await _settle()
    await queue.drain()

    assert calls == 1
    assert queue.snapshot().outcomes["RuntimeError"] == 1
    assert queue.snapshot().outcomes["ok"] == 1


async def test_drain_waits_for_in_flight_work() -> None:
    finished = False

    async def handler(payload: object) -> None:
        nonlocal finished
        await asyncio.sleep(0.05)
        finished = True

    queue = CaptureQueue(_settings(), {"run": handler})
    await queue.start()
    queue.submit(CaptureRequest("run", "run:1", 1))
    await queue.drain()

    assert finished is True


async def test_a_completed_delivery_does_not_wait_for_capture(bound, monkeypatch) -> None:
    """The webhook answers while the archive download is still running.

    This is the reason the queue exists. The controller is wired with the real
    flow and only the slow handler is replaced, so the timing measured is the
    response path rather than a stand-in for it.
    """
    import time
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from conftest import make_webhook_service

    class _Source:
        async def connection(self):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def cm():
                yield None

            return cm()

    service = make_webhook_service(database=_Source())
    repo = SimpleNamespace(id=1, full_name="acme/demo")
    run = SimpleNamespace(
        id=1,
        github_run_id=42,
        run_number=1,
        latest_status="completed",
        latest_conclusion="success",
        latest_started_at=None,
        latest_completed_at=None,
    )
    store = SimpleNamespace(
        run=AsyncMock(return_value=run),
        update_run=AsyncMock(return_value=run),
        note_webhook_received=AsyncMock(return_value=run),
    )
    monkeypatch.setattr(service, "_repos", SimpleNamespace(by_name=AsyncMock(return_value=None)))
    monkeypatch.setattr(service, "_repository", AsyncMock(return_value=repo))
    monkeypatch.setattr(service, "_store", store)
    monkeypatch.setattr(service, "_tracker", None)
    monkeypatch.setattr(service, "_logs", SimpleNamespace(ensure=AsyncMock()))

    started = asyncio.Event()

    async def slow_ensure(repository, run, **kwargs) -> object:
        started.set()
        await asyncio.sleep(0.3)
        return SimpleNamespace(skipped_reason="not_step_archive")

    monkeypatch.setattr(service._logs, "ensure", slow_ensure)
    await service.start_captures()
    try:
        began = time.perf_counter()
        applied = await service.handle_workflow_run(
            {
                "action": "completed",
                "repository": {"id": 1, "full_name": "acme/demo"},
                "workflow_run": {"id": 42, "name": "CI", "path": "ci.yml"},
            }
        )
        elapsed = time.perf_counter() - began

        assert applied is True
        assert elapsed < 0.1, f"the delivery waited {elapsed:.2f}s for the archive"
        await asyncio.wait_for(started.wait(), timeout=1)
    finally:
        await service.drain_captures()


async def test_shutdown_logs_which_captures_it_cancelled(caplog) -> None:
    """A cancelled capture names its key, so an operator can go repair it."""
    import logging

    started = asyncio.Event()

    async def slow(payload: object) -> None:
        started.set()
        await asyncio.sleep(5)

    queue = CaptureQueue(_settings(drain_timeout_seconds=0.01), {"run": slow})
    await queue.start()
    queue.submit(CaptureRequest("run", "run:77", 77))
    await asyncio.wait_for(started.wait(), timeout=1)

    with caplog.at_level(
        logging.WARNING, logger="rhosocial.github_actions_tracker.controllers.capture"
    ):
        await queue.drain()

    assert "Capture cancelled at shutdown: run:77" in caplog.text


async def test_submit_after_drain_is_dropped() -> None:
    async def handler(payload: object) -> None:
        return None

    queue = CaptureQueue(_settings(), {"run": handler})
    await queue.start()
    await queue.drain()
    queue.submit(CaptureRequest("run", "run:1", 1))

    health = queue.snapshot()
    assert health.pending == 0
    assert health.dropped == 1
