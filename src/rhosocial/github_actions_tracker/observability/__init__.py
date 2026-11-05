# src/rhosocial/github_actions_tracker/observability/__init__.py
"""The service's own operational metrics: one registry, three shapes.

``metrics()`` is the process registry; the families are declared once in
:mod:`.definitions` and every recording point reads its counter from there. One
declaration site keeps a metric from being defined twice with different label
sets, which is the kind of drift that makes a dashboard quietly wrong.

``/metrics`` is rendered from the same registry by
:mod:`rhosocial.github_actions_tracker.observability.exposition`.
"""

from __future__ import annotations

from .definitions import (
    GITHUB_API_SECONDS,
    GITHUB_API_TOTAL,
    RUN_INGEST_TOTAL,
    TOOL_CALLS_TOTAL,
    TOOL_SECONDS,
    WEBHOOK_DELIVERIES_TOTAL,
    WEBHOOK_SECONDS,
)
from .registry import metrics, reset

__all__ = [
    "GITHUB_API_SECONDS",
    "GITHUB_API_TOTAL",
    "RUN_INGEST_TOTAL",
    "TOOL_CALLS_TOTAL",
    "TOOL_SECONDS",
    "WEBHOOK_DELIVERIES_TOTAL",
    "WEBHOOK_SECONDS",
    "metrics",
    "reset",
]
