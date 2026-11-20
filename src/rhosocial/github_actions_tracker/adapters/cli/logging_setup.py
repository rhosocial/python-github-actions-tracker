# src/rhosocial/github_actions_tracker/adapters/cli/logging_setup.py
"""Logging setup shared by the two CLI entry points.

The ORM configures its own loggers at DEBUG, and a command that writes a line
per query makes its JSON and text output unusable when redirected. Both binaries
call this once, before any command runs, so the rule lives in one place.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

__all__ = ["CliLogging", "PreviousLogging"]


@dataclass
class PreviousLogging:
    """What a command changed, so an in-process caller can put it back.

    A CLI normally runs in its own process, where restoring anything is
    pointless. The test suite calls ``main()`` in-process, though, and a level
    left at WARNING silently wins over the framework's later policy -- which is
    why a test that asks for DEBUG saw nothing when it ran after a CLI test.
    """

    disable: int
    root_level: int
    levels: dict[str, int] = field(default_factory=dict)


class CliLogging:
    """Configure library loggers for one CLI invocation."""

    #: Level used when ``--verbose`` is absent.
    DEFAULT_LEVEL = "WARNING"

    @classmethod
    def configure(cls, verbose: bool = False) -> PreviousLogging:
        """Keep library diagnostics out of a command's output.

        Args:
            verbose: Emit DEBUG lines, which is what ``--verbose`` asks for.

        Returns:
            The previous global disable level and every library logger level
            this call changed, to pass to :meth:`restore`.
        """
        # `GHAT_LOG_LEVEL` belongs to the service process; a command has its own
        # switch, so a container's INFO setting cannot make the CLI noisy.
        level = logging.DEBUG if verbose else logging.WARNING
        logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
        previous = PreviousLogging(
            disable=logging.root.manager.disable,
            root_level=logging.getLogger().level,
        )
        logging.getLogger().setLevel(level)
        for name, candidate in list(logging.Logger.manager.loggerDict.items()):
            if name.startswith("rhosocial") and isinstance(candidate, logging.Logger):
                previous.levels[name] = candidate.level
                candidate.setLevel(level)

        # The ORM also sets the level of loggers it creates *after* this call.
        # `disable` is the one switch those cannot undo: it is consulted by
        # every `isEnabledFor`, so a child left at DEBUG still stays silent.
        logging.disable(level - 1 if level > logging.DEBUG else logging.NOTSET)
        return previous

    @classmethod
    def restore(cls, previous: PreviousLogging) -> None:
        """Return the global and per-logger levels to what they were."""
        logging.disable(previous.disable)
        logging.getLogger().setLevel(previous.root_level)
        for name, level in previous.levels.items():
            candidate = logging.getLogger(name)
            candidate.setLevel(level)
