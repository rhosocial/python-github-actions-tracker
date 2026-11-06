# src/rhosocial/github_actions_tracker/credentials/redaction.py
"""Keep a credential's value out of the query log while it is being written.

The ORM logs every query's parameters at INFO, and adding or rotating a
credential puts the PAT itself in an ``INSERT`` or ``WHERE`` parameter. This
context manager raises the credential model's logger -- and the backend loggers
it writes through -- to WARNING for the duration; warnings and errors still come
through, and every level is restored even when the write fails.

It lives here rather than in the service because the logger belongs to the model
this package owns: the service asks for the silence without naming the model.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

from ..models.pat_credential import AsyncPatCredential


@contextlib.contextmanager
def redact_credential_logging() -> Iterator[None]:
    """Suppress query-parameter logging for one secret write.

    Two models carry a secret now: the PAT and a repository's webhook secret.
    Both are written through the ORM, whose DEBUG lines echo every parameter --
    including the secret itself. Raising the relevant loggers to WARNING for the
    duration is what keeps a recorded log from containing one.
    """
    targets = [AsyncPatCredential.get_logger()]
    try:
        from ..models.webhook_secret import AsyncRepoWebhookSecret

        targets.append(AsyncRepoWebhookSecret.get_logger())
    except Exception:  # pragma: no cover - the model always exists when used
        pass
    for name, candidate in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith("rhosocial.activerecord") and isinstance(candidate, logging.Logger):
            if candidate not in targets:
                targets.append(candidate)

    previous = [(target, target.level) for target in targets]
    for target in targets:
        target.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for target, level in previous:
            target.setLevel(level)


__all__ = ["redact_credential_logging"]
