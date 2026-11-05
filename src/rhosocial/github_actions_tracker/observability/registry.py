# src/rhosocial/github_actions_tracker/observability/registry.py
"""The process's own counters, and the Prometheus text that describes them.

Everything this service does is a request: a GitHub webhook arrives, a tool is
called, a run is read back from GitHub, a connection is acquired. Counting them
is the only way to see "is the service well" in a window nobody is watching --
a log line says one thing happened, and a counter says a rate.

Three shapes, because three questions:

* a **counter** answers "how many, ever" -- webhooks received, tool calls, GitHub
  answers by status code;
* a **histogram** answers "how long, and how often the slow ones" -- rendered as
  cumulative buckets so a scraper can compute quantiles the usual way;
* a **gauge** answers "what is it right now" -- and is deliberately *absent*
  here. A gauge is a fact about shared state, and this service's shared state is
  the database: see ``ci_status.storage`` and ``ci_status.health.pool``, which
  read it for whoever asks. Putting it here would make a scrape pay for a read.

What is *not* here: persistence. Counters are process-local, which is what makes
them free to increment on the request path -- no write, no lock, no table. A
prometheus scraping several instances and summing with ``sum()`` is the standard
answer to "what does the whole deployment look like", and one this service does
not have to build. The gauges that *do* describe shared state are the ones above,
which the scraper reads from the same endpoints a caller uses.

The text format is rendered here rather than pulled from a client library: it is
a hundred lines, this project already declares fewer dependencies than it could,
and a scraper only ever sees the bytes.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: The one process-wide registry. Metrics are module state the way ``logging``
#: is: there is one, it is created on first use, and a test that wants a clean
#: one calls :func:`reset`.
_REGISTRY: Registry | None = None
_LOCK = threading.Lock()

#: Whether the declared families have been attached to a registry. A registry is
#: rebuilt from its own metric objects, so this is what stops a second attach of
#: the same object -- and what lets a ``reset()`` re-attach them.
_DECLARED = False

#: Histogram buckets by magnitude, chosen for what each family measures rather
#: than a single default: a bucket set that spans six orders of magnitude says
#: nothing about any of them.
HTTP_BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
#: ``wait_for_run`` holds a connection for as long as it waits, so its latency
#: lives on a different scale from every other tool -- and a bucket set that has
#: to cover both would flatten the small half.
WAIT_BUCKETS: tuple[float, ...] = (0.1, 0.5, 1.0, 5.0, 30.0, 60.0, 120.0, 300.0)


class Counter:
    """A count of events, per label set. Monotonic, never reset except on boot."""

    def __init__(self, name: str, documentation: str, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self.documentation = documentation
        self.labels = labels
        self._values: dict[tuple[str, ...], int] = {}
        self._lock = threading.Lock()

    def inc(self, amount: int = 1, **labels: str) -> None:
        """Record ``amount`` events with these labels.

        Takes a lock rather than relying on the GIL: ``+=`` on a dict is not
        atomic, and a webhook burst is exactly when a lost increment matters
        most.
        """
        key = tuple(str(labels.get(label, "")) for label in self.labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def clear(self) -> None:
        """Forget every count. The family stays; only the numbers go."""
        with self._lock:
            self._values.clear()

    def render(self) -> list[str]:
        """The exposition lines, one per label set."""
        lines = [f"# HELP {self.name} {self.documentation}", f"# TYPE {self.name} counter"]
        for key, value in sorted(self._values.items()):
            rendered = _labels(self.labels, key)
            lines.append(f"{self.name}{rendered} {value}")
        return lines


class Histogram:
    """A distribution: how many events fell in each bucket, and their total.

    Cumulative buckets, as the exposition format requires: a scraper computing a
    quantile subtracts neighbouring counts, and a per-bucket count would make it
    do that arithmetic itself.
    """

    def __init__(
        self,
        name: str,
        documentation: str,
        labels: tuple[str, ...] = (),
        buckets: tuple[float, ...] = HTTP_BUCKETS,
    ) -> None:
        self.name = name
        self.documentation = documentation
        self.labels = labels
        self.buckets = buckets
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        self._totals: dict[tuple[str, ...], int] = {}
        self._lock = threading.Lock()

    def observe(self, seconds: float, **labels: str) -> None:
        """Record one observation. ``seconds`` must not be negative."""
        key = tuple(str(labels.get(label, "")) for label in self.labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * (len(self.buckets) + 1))
            self._totals[key] = self._totals.get(key, 0) + 1
            self._sums[key] = self._sums.get(key, 0.0) + seconds
            for index, boundary in enumerate(self.buckets):
                if seconds <= boundary:
                    counts[index] += 1
                    return
            counts[-1] += 1

    def clear(self) -> None:
        """Forget every observation. The buckets stay; only the counts go."""
        with self._lock:
            self._counts.clear()
            self._sums.clear()
            self._totals.clear()

    def render(self) -> list[str]:
        """The exposition lines: one ``le`` bucket per boundary, plus the sum."""
        lines = [f"# HELP {self.name} {self.documentation}", f"# TYPE {self.name} histogram"]
        for key in sorted(self._counts):
            counts = self._counts[key]
            rendered = _labels(self.labels, key)
            for index, boundary in enumerate(self.buckets):
                bucket_labels = _labels(self.labels, key, le=boundary)
                lines.append(f"{self.name}_bucket{bucket_labels} {counts[index]}")
            bucket_labels = _labels(self.labels, key, le="+Inf")
            lines.append(f"{self.name}_bucket{bucket_labels} {self._totals[key]}")
            lines.append(f"{self.name}_sum{rendered} {self._sums[key]}")
            lines.append(f"{self.name}_count{rendered} {self._totals[key]}")
        return lines


@dataclass
class Registry:
    """Every metric this process reports, and the text a scraper reads."""

    metrics: list[Any] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def counter(self, name: str, documentation: str, labels: tuple[str, ...] = ()) -> Counter:
        """Declare (or fetch) a counter. Idempotent, so two modules may name one."""
        with self._lock:
            for metric in self.metrics:
                if isinstance(metric, Counter) and metric.name == name:
                    return metric
            metric = Counter(name, documentation, labels)
            self.metrics.append(metric)
            return metric

    def histogram(
        self,
        name: str,
        documentation: str,
        labels: tuple[str, ...] = (),
        buckets: tuple[float, ...] = HTTP_BUCKETS,
    ) -> Histogram:
        """Declare (or fetch) a histogram. Idempotent, as :meth:`counter`."""
        with self._lock:
            for metric in self.metrics:
                if isinstance(metric, Histogram) and metric.name == name:
                    return metric
            metric = Histogram(name, documentation, labels, buckets)
            self.metrics.append(metric)
            return metric

    def render(self) -> str:
        """The whole exposition, as one text block.

        A metric nobody has recorded yet is still declared: a scraper that sees
        the family knows what exists, and ``rate()`` on an all-zero series is an
        honest "nothing happened" rather than a missing query.
        """
        blocks = [metric.render() for metric in self.metrics]
        return "\n".join("\n".join(block) for block in blocks if block) + "\n"


def metrics() -> Registry:
    """The process registry, created on first use."""
    global _REGISTRY
    if _REGISTRY is None:
        with _LOCK:
            if _REGISTRY is None:
                _REGISTRY = Registry()
    if not _DECLARED:
        _declare_defaults(_REGISTRY)
    return _REGISTRY


def reset() -> None:
    """Drop every recorded value -- for a test that wants a clean slate.

    The families are kept, because a registry rebuilt without them would forget
    what this service reports for the rest of the process's life; only the
    numbers are cleared, which is what "clean" means to a test.
    """
    global _REGISTRY
    with _LOCK:
        registry = _REGISTRY
        if registry is not None:
            for metric in registry.metrics:
                metric.clear()
        _REGISTRY = Registry()
        for metric in registry.metrics:
            _REGISTRY.metrics.append(metric)


def _declare_defaults(registry: Registry) -> None:
    """Declare the families every process reports, before anything records one.

    A family nobody has recorded yet is still a family: a scraper that sees the
    name knows what exists, and ``rate()`` over an all-zero series is an honest
    "nothing happened" rather than a missing query.

    The families are *lazily* declared: the import happens the first time a
    caller records rather than at registry construction, because
    :mod:`.definitions` registers through ``metrics()`` -- importing it from
    inside ``metrics()`` asks a half-built registry for a counter.
    """
    global _DECLARED
    if _DECLARED:
        return
    import sys

    module = sys.modules.get(__name__.rsplit(".", 1)[0] + ".definitions")
    if module is None or not hasattr(module, "ALL_METRICS"):
        # :mod:`.definitions` is still executing its module body -- which is
        # what called us -- so its families are already registering themselves
        # one at a time. Nothing to attach yet.
        return
    for metric in module.ALL_METRICS:
        if metric.name not in {existing.name for existing in registry.metrics}:
            registry.metrics.append(metric)
    _DECLARED = True


def _labels(
    names: tuple[str, ...],
    values: tuple[str, ...],
    **extra: object,
) -> str:
    """``{a="1",le="0.5"}`` -- in declaration order, so the text is stable."""
    pairs: list[tuple[str, str]] = []
    for name, value in zip(names, values, strict=True):
        pairs.append((name, _escape(str(value))))
    for name, value in extra.items():
        pairs.append((name, _escape(str(value))))
    if not pairs:
        return ""
    return "{" + ",".join(f'{name}="{value}"' for name, value in pairs) + "}"


def _escape(value: str) -> str:
    """Escape a label value the way the exposition format spells it."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


__all__ = [
    "HTTP_BUCKETS",
    "WAIT_BUCKETS",
    "Counter",
    "Histogram",
    "Registry",
    "metrics",
    "reset",
]
