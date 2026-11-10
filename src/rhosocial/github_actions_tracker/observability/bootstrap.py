# src/rhosocial/github_actions_tracker/observability/bootstrap.py
"""Where the process's logging policy is applied.

``GHAT_LOG_LEVEL`` has been read into settings since the setting existed and
applied nowhere: the library's quiet default (``WARNING``) held, so an operator
who set ``INFO`` to see queries got the same silence as ``WARNING``. This module
is the missing step, and it lives in ``observability`` because logging is the
service's own concern -- the same package that owns ``/metrics`` owns this.

Two levels are configured, and they answer different questions:

* the **root** logger gets a handler, because a process's records have to reach
  its output somewhere. The library never installs one of its own, by design;
* the **library** namespace gets the level, through the framework's own
  ``configure_logging``. The framework marks the level as its own decision, so
  a later application override on a specific logger still wins.

``configure_logging(level=WARNING)`` is the one spelling that is *not* written:
the framework keeps loggers at ``NOTSET`` then, so they inherit the root. Setting
it explicitly would freeze the library at WARNING whatever the root says, which
is the opposite of "the application owns logging".
"""

from __future__ import annotations

import logging

__all__ = ["apply_logging_policy"]

#: The framework's own logger namespace. Nothing outside it is touched: the
#: tracker's own loggers inherit the root like any other library's.
LIBRARY_NAMESPACE = "rhosocial.activerecord"

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _level_of(name: str, default: int = logging.INFO) -> int:
    """The level a settings string names, or the default when it names nothing.

    A typo must not silently become a quieter or louder process than asked, so
    an unreadable value falls back rather than guessing between the two.
    """
    text = (name or "").strip().upper()
    if not text:
        return default
    resolved = logging.getLevelName(text)
    return resolved if isinstance(resolved, int) else default


def _root_handler() -> logging.Handler:
    """The root's stream handler, installed once and then reused.

    A process's records have to reach its output somewhere, and the library
    never installs a handler of its own by design -- so this is the one place
    the service does. A marker attribute rather than a formatter comparison,
    because "does this handler print the way I would" is not a question worth
    asking of an existing one.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_ghat_root", False):
            return handler
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler._ghat_root = True
    root.addHandler(handler)
    return handler


def apply_logging_policy(level_name: str) -> int:
    """Configure this process's logging and return the level it put in force.

    Idempotent: a second call re-states the same policy rather than stacking a
    handler, which is what a test or a re-entrant import needs.

    Args:
        level_name: ``GHAT_LOG_LEVEL`` -- ``DEBUG`` / ``INFO`` / ``WARNING`` /
            ``ERROR``.

    Returns:
        The numeric level applied to the library namespace.
    """
    level = _level_of(level_name)
    root = logging.getLogger()
    root.addHandler(_root_handler())
    root.setLevel(level)

    from rhosocial.activerecord.logging import configure_logging

    configure_logging(level=level)
    return level
