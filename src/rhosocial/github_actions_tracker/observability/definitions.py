# src/rhosocial/github_actions_tracker/observability/definitions.py
"""Every metric this service reports, declared once.

A recording point asks for its counter by name and gets the same object every
time, which is what stops two modules from defining ``ghat_tool_calls_total``
with different label sets -- a duplication that yields two series under one name
and a dashboard that reads half of them.

The families, and what each answers:

* **webhook ingestion** -- how many deliveries arrived, how many were stored,
  how long storing took. The push channel is this service's only inbound, so a
  drop here is invisible everywhere else.
* **run ingestion** -- run rows written, by result. The number that says
  "is GitHub still answering us".
* **GitHub API** -- every outbound call, by operation and status code. This is
  where a caller's latency *comes from* (run state is read from GitHub every
  time), and where the quota is spent.
* **tool calls** -- every MCP/REST tool, by protocol and outcome. ``wait_for_run``
  carries its own bucket scale because its latency is minutes where other tools
  are milliseconds.

Labels are chosen for the questions that actually get asked, not for every
dimension available: ``status_code`` rather than ``ok``/``error`` (403 and 404
call for different responses, and merging them loses the distinction),
``operation`` rather than a full URL (a URL carries repository names, and a label
per repository turns a metric into a table).
"""

from __future__ import annotations

from typing import Any

from .registry import WAIT_BUCKETS, metrics

# ---------------------------------------------------------------------------
# Webhook ingestion
# ---------------------------------------------------------------------------

#: Deliveries received, by event stream and what happened to them. ``result``
#: is ``stored``, ``partial`` or ``rejected``; ``event_type`` separates the two
#: GitHub streams, whose meaning differs enough that a merged count misleads.
WEBHOOK_DELIVERIES_TOTAL = metrics().counter(
    "ghat_webhook_deliveries_total",
    "GitHub webhook deliveries received, by stream and handling result",
    ("event_type", "result"),
)

#: Time from receiving a delivery to finishing with it. Not the same as
#: "how long GitHub was slow": this includes the reads and writes the delivery
#: causes, which is what an operator tuning capacity cares about.
WEBHOOK_SECONDS = metrics().histogram(
    "ghat_webhook_seconds",
    "Time from receiving a webhook delivery to finishing with it",
    ("event_type",),
)

# ---------------------------------------------------------------------------
# Run ingestion
# ---------------------------------------------------------------------------

#: Run rows written, by result. ``created`` and ``updated`` are separate because
#: a system that only ever updates is not learning about new work, and one that
#: only ever creates is re-adopting runs it already has.
RUN_INGEST_TOTAL = metrics().counter(
    "ghat_run_ingest_total",
    "Run rows written from GitHub payloads, by outcome",
    ("result",),
)

# ---------------------------------------------------------------------------
# Outbound GitHub API
# ---------------------------------------------------------------------------

#: Every outbound call, by operation and the status code it answered with.
#: ``operation`` is the resource group's name (``runs``, ``jobs``, ``logs``),
#: never a URL: a repository name in a label makes one series per repository.
GITHUB_API_TOTAL = metrics().counter(
    "ghat_github_api_total",
    "Outbound GitHub API calls, by operation and status code",
    ("operation", "status_code"),
)

#: How long each took. The user-perceived latency of almost every read path is
#: this number plus the database round trips around it.
GITHUB_API_SECONDS = metrics().histogram(
    "ghat_github_api_seconds",
    "Outbound GitHub API latency, by operation and status code",
    ("operation", "status_code"),
)

# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

#: Credential checks answered from memory, by surface and outcome. The outcome
#: is the authentication decision, never a reason code from the ledger: what an
#: operator acts on is "deliveries are being refused because the secret is
#: missing", which is ``secret_not_configured`` and not ``error``.
AUTH_VERIFY_TOTAL = metrics().counter(
    "ghat_auth_verify_total",
    "Credential checks answered without a database round trip, by surface and outcome",
    ("surface", "outcome"),
)

#: Snapshot refreshes, by outcome. ``ok`` is the lazy TTL refresh that succeeded;
#: ``error`` is one that failed, which leaves the previous snapshot in place --
#: the number that tells "the cache is stale" apart from "the cache is broken".
AUTH_CACHE_REFRESH_TOTAL = metrics().counter(
    "ghat_auth_cache_refresh_total",
    "Authorization snapshot refreshes, by outcome",
    ("outcome",),
)

# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------

#: Every tool invocation, by tool, protocol and outcome. ``outcome`` is
#: ``ok`` or ``error``; a tool that fails only for some callers is invisible
#: without the per-tool split.
TOOL_CALLS_TOTAL = metrics().counter(
    "ghat_tool_calls_total",
    "MCP and REST tool invocations, by tool, protocol and outcome",
    ("tool", "protocol", "outcome"),
)

#: How long each took. Two bucket scales: ``wait_for_run``'s latency is minutes
#: where every other tool is milliseconds, and one bucket set covering both
#: would flatten the small half. (It no longer holds a connection while waiting;
#: the reason for the split is purely the latency magnitude.)
TOOL_SECONDS = metrics().histogram(
    "ghat_tool_seconds",
    "MCP and REST tool latency, by tool, protocol and outcome",
    ("tool", "protocol", "outcome"),
    buckets=WAIT_BUCKETS,
)

__all__ = [
    "ALL_METRICS",
    "AUTH_CACHE_REFRESH_TOTAL",
    "AUTH_VERIFY_TOTAL",
    "GITHUB_API_SECONDS",
    "GITHUB_API_TOTAL",
    "RUN_INGEST_TOTAL",
    "TOOL_CALLS_TOTAL",
    "TOOL_SECONDS",
    "WEBHOOK_DELIVERIES_TOTAL",
    "WEBHOOK_SECONDS",
]

#: Every family this process reports, in the order the exposition should carry
#: them. Declared once here so the registry can re-attach them after a
#: ``reset()`` -- a registry rebuilt from its own objects would otherwise forget
#: what this service reports for the rest of the process's life.
ALL_METRICS: tuple[Any, ...] = (
    WEBHOOK_DELIVERIES_TOTAL,
    WEBHOOK_SECONDS,
    RUN_INGEST_TOTAL,
    GITHUB_API_TOTAL,
    GITHUB_API_SECONDS,
    TOOL_CALLS_TOTAL,
    TOOL_SECONDS,
    AUTH_VERIFY_TOTAL,
    AUTH_CACHE_REFRESH_TOTAL,
)
