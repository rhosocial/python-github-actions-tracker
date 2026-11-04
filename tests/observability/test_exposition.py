# tests/observability/test_exposition.py
"""``/metrics`` answers with the registry, and every request is counted.

The registry's own shapes are pinned in :mod:`tests.observability.test_registry`;
what is pinned here is the two halves a scraper depends on: the text arrives with
the right content type, and the requests that made the numbers are counted with
the tool and the protocol they came through.
"""

from __future__ import annotations

from typing import Any

import pytest

from rhosocial.github_ci_tracker.observability import reset
from rhosocial.github_ci_tracker.observability.definitions import (
    GITHUB_API_SECONDS,
    GITHUB_API_TOTAL,
    TOOL_CALLS_TOTAL,
    TOOL_SECONDS,
    WEBHOOK_DELIVERIES_TOTAL,
)
from rhosocial.github_ci_tracker.observability.exposition import (
    METRICS_PATH,
    MetricsMiddleware,
    _template_of,
    metrics_app,
    metrics_app_text,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """Each test gets its own registry, so counts do not leak between them."""
    reset()
    yield
    reset()


def _receive() -> Any:
    async def receive() -> Any:
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


def _discard() -> Any:
    async def discard(_message: Any) -> None:
        return None

    return discard


def _get(path: str, method: str = "GET") -> dict[str, Any]:
    """One ASGI request's scope."""
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
    }


class _App:
    """Answers 200 with a small body, or the failure it was built with."""

    def __init__(self, *, fails: bool = False) -> None:
        self._fails = fails

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self._fails:
            raise OSError("the app is down")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})


class TestTheText:
    def test_the_registry_renders_one_line_per_series(self) -> None:
        WEBHOOK_DELIVERIES_TOTAL.inc(event_type="workflow_run", result="stored")

        text = metrics_app_text()

        assert "# HELP citracker_webhook_deliveries_total" in text
        assert "# TYPE citracker_webhook_deliveries_total counter" in text
        assert (
            'citracker_webhook_deliveries_total{event_type="workflow_run",result="stored"} 1'
            in text
        )

    def test_an_unrecorded_metric_is_still_declared(self) -> None:
        """A scraper that sees the family knows what exists; ``rate()`` on an
        all-zero series is "nothing happened", not a missing query."""
        text = metrics_app_text()

        assert "citracker_github_api_total" in text

    async def test_the_endpoint_answers_prometheus_text(self) -> None:
        sent: list[dict[str, Any]] = []

        async def send(message: Any) -> None:
            sent.append(message)

        await metrics_app(_get(METRICS_PATH), _receive(), send)

        assert sent[0]["status"] == 200
        headers = dict(sent[0]["headers"])
        assert "text/plain; version=0.0.4" in str(headers[b"content-type"])
        body = sent[1]["body"]
        assert isinstance(body, bytes), body
        assert b"citracker_webhook_deliveries_total" in body


class TestThePathTemplate:
    def test_a_variable_segment_is_a_name_not_a_value(self) -> None:
        """One series per path, not one per run id."""
        assert _template_of("/api/v1/runs/123") == "/api/v1/runs/{id}"
        assert _template_of("/api/v1/runs/123/jobs/456") == "/api/v1/runs/{id}/jobs/{id}"
        assert _template_of("/api/v1/latest-run") == "/api/v1/latest-run"


class TestTheMiddleware:
    async def test_a_request_is_counted_by_tool_and_protocol(self) -> None:
        middleware = MetricsMiddleware(_App(), protocol="rest")

        await middleware(_get("/api/v1/runs/123"), _receive(), _discard())

        assert TOOL_CALLS_TOTAL._values[("/api/v1/runs/{id}", "rest", "ok")] == 1
        assert TOOL_SECONDS._totals[("/api/v1/runs/{id}", "rest", "ok")] == 1

    async def test_the_metrics_path_is_not_counted(self) -> None:
        """Counting the scraper would turn monitoring into load."""
        middleware = MetricsMiddleware(_App(), protocol="rest")

        await middleware(_get(METRICS_PATH), _receive(), _discard())

        assert TOOL_CALLS_TOTAL._values == {}

    async def test_probes_are_not_counted(self) -> None:
        """A probe is not a tool call; a restart loop must not read as traffic."""
        middleware = MetricsMiddleware(_App(), protocol="rest")

        for path in ("/livez", "/readyz", "/health"):
            await middleware(_get(path), _receive(), _discard())

        assert TOOL_CALLS_TOTAL._values == {}

    async def test_a_failure_is_counted_as_an_error(self) -> None:
        """A tool that fails is the number an operator most wants to see."""
        middleware = MetricsMiddleware(_App(fails=True), protocol="mcp")

        with pytest.raises(OSError):
            await middleware(_get("/api/v1/runs/123"), _receive(), _discard())

        assert TOOL_CALLS_TOTAL._values[("/api/v1/runs/{id}", "mcp", "error")] == 1


class TestTheGithubAnswer:
    """The outbound side: the latency a caller feels carries its status code."""

    async def test_a_call_is_counted_by_the_answer_it_got(self) -> None:
        from rhosocial.github_ci_tracker.services.github.client import (
            _note_status,
            _observed_async,
        )

        async with _observed_async("GET", "/repos/o/r/actions/runs/1"):
            _note_status(_Response(200))

        assert GITHUB_API_TOTAL._values[("runs", "200")] == 1
        assert GITHUB_API_SECONDS._totals[("runs", "200")] == 1

    async def test_a_failed_call_is_counted_as_an_error(self) -> None:
        """A call that never answered is a status worth counting: the transport
        failing is not the same as GitHub refusing, and both differ from a 200."""
        from rhosocial.github_ci_tracker.services.github.client import _observed_async

        with pytest.raises(OSError):
            async with _observed_async("GET", "/repos/o/r/actions/runs/1"):
                raise OSError("network is down")

        assert GITHUB_API_TOTAL._values[("runs", "error")] == 1

    def test_the_operation_omits_the_repository_and_the_id(self) -> None:
        """One series per resource group, not one per repository: a repository
        name in a label turns a metric into a table."""
        from rhosocial.github_ci_tracker.services.github.client import _operation_of

        assert _operation_of("/repos/o/r/actions/runs/123") == "runs"
        assert _operation_of("/repos/o/r/actions/jobs/9/logs") == "jobs"
        assert _operation_of("/orgs/o/actions/runs/1") == "runs"


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        return self._response


class _NoAnswerClient:
    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        raise OSError("network is down")
