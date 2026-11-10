# src/rhosocial/github_actions_tracker/credentials/redaction.py
"""Keep a credential's value out of the query log while it is being written.

The ORM logs every query's parameters, and adding or rotating a credential puts
the PAT itself in an ``INSERT`` or ``WHERE`` parameter. Two models carry one:
:class:`~rhosocial.github_actions_tracker.models.pat_credential.AsyncPatCredential`
and :class:`~rhosocial.github_actions_tracker.models.webhook_secret.AsyncRepoWebhookSecret`.

**This is a workaround for the release line as it stands, not a design.** The
framework's ``feature/logging-execution`` branch changes all three facts this
module exists for: parameters are no longer logged at INFO at all, an unnamed
positional tuple renders as ``<N params: types>`` rather than its values, and a
caller that supplies ``ParamMeta`` gets per-field masking through the same
``LogPayload`` contract the rest of the library uses. Until that branch merges,
none of it is available here -- so this module keeps doing the crude thing and
says so, rather than pretending the library already solved it.

When it does merge, the honest move is to delete this file and pass
``ExecutionOptions(log_context=LogContext(param_meta=...))`` on the two write
paths. Nothing else in the service should keep working by raising logger levels.

It lives here rather than in the service because the logger belongs to the model
this package owns: the service asks for the silence without naming the model.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

from ..models.pat_credential import AsyncPatCredential

__all__ = ["redact_credential_logging"]


def _credential_loggers() -> list[logging.Logger]:
    """Every logger a credential write's statement can be logged by.

    The two models' own loggers, and the backend loggers the queries go out on.
    The backend is the one that matters: a credential row is written through the
    ORM like any other row, so the line carrying its value is emitted by the
    *backend* logger, and catching only the model would miss exactly the record
    this module is for.
    """
    targets: list[logging.Logger] = [AsyncPatCredential.get_logger()]
    try:
        from ..models.webhook_secret import AsyncRepoWebhookSecret

        targets.append(AsyncRepoWebhookSecret.get_logger())
    except Exception:  # pragma: no cover - the model always exists when used
        pass

    seen = {id(target) for target in targets}
    for name, candidate in sorted(logging.Logger.manager.loggerDict.items()):
        if not isinstance(candidate, logging.Logger):
            continue
        if not name.startswith("rhosocial.activerecord"):
            continue
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        targets.append(candidate)
    return targets


@contextlib.contextmanager
def redact_credential_logging() -> Iterator[None]:
    """Suppress query-parameter logging for one secret write.

    Every level is restored on the way out, including when the write fails, so a
    raised level cannot outlive the statement it was raised for. ``WARNING`` is
    the level the library itself defaults to, so this returns those loggers to
    the state the process would already be in had the operator not asked for
    detail -- it is not a second level of its own.
    """
    targets = _credential_loggers()
    previous = [(target, target.level) for target in targets]
    for target in targets:
        target.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for target, level in previous:
            target.setLevel(level)
