# src/rhosocial/github_actions_tracker/adapters/cli/logging_setup.py
"""Logging setup shared by the two CLI entry points.

The ORM configures its own loggers at DEBUG, and a command that writes a line
per query makes its JSON and text output unusable when redirected. Both binaries
call this once, before any command runs, so the rule lives in one place.
"""

from __future__ import annotations

import logging
import os


class CliLogging:
    """Configure library loggers for one CLI invocation."""

    #: Level used when neither ``--verbose`` nor ``GHAT_LOG_LEVEL`` says otherwise.
    DEFAULT_LEVEL = "WARNING"

    @classmethod
    def configure(cls, verbose: bool = False) -> int:
        """Keep library diagnostics out of a command's output.

        Args:
            verbose: Emit DEBUG lines, which is what ``--verbose`` asks for.

        Returns:
            The previous global disable level, to pass to :meth:`restore`.
        """
        level_name = "DEBUG" if verbose else os.getenv("GHAT_LOG_LEVEL", cls.DEFAULT_LEVEL)
        level = getattr(logging, level_name.upper(), logging.WARNING)
        logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
        logging.getLogger().setLevel(level)
        for name, candidate in list(logging.Logger.manager.loggerDict.items()):
            if name.startswith("rhosocial") and isinstance(candidate, logging.Logger):
                candidate.setLevel(level)
        # The ORM also sets the level of loggers it creates *after* this call.
        # `disable` is the one switch those cannot undo: it is consulted by
        # every `isEnabledFor`, so a child left at DEBUG still stays silent.
        previous = logging.root.manager.disable
        if level > logging.DEBUG:
            logging.disable(level - 1)
        else:
            logging.disable(logging.NOTSET)
        return previous

    @classmethod
    def restore(cls, previous_disable: int) -> None:
        """Return the global disable level to what it was before a command.

        A command runs in its own process, so restoring is only necessary for
        an in-process caller (the test suite calls ``main``); it keeps one
        command's logging policy from leaking into the next.
        """
        logging.disable(previous_disable)


__all__ = ["CliLogging"]
