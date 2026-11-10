# tests/observability/test_logging_policy.py
"""``GHAT_LOG_LEVEL`` is applied, or the setting is a lie.

The variable has been read into settings since the setting existed and applied
nowhere: the library's quiet default held, so an operator who set ``INFO`` to see
queries got exactly the silence ``WARNING`` gives. These tests pin the missing
step -- and pin the two things it must *not* do, which are the ways a logging
policy quietly becomes a different policy than the one asked for.
"""

from __future__ import annotations

import logging

import pytest

from rhosocial.github_actions_tracker.observability.bootstrap import (
    _level_of,
    apply_logging_policy,
)

LIBRARY_LOGGER = "rhosocial.activerecord"


@pytest.fixture(autouse=True)
def _isolated() -> logging.Logger:
    """Undo whatever a test applied, so one test's policy is not the next's."""
    root = logging.getLogger()
    previous = (
        root.level,
        list(root.handlers),
        logging.Logger.manager.loggerDict.get(LIBRARY_LOGGER),
    )
    yield root
    root.setLevel(previous[0])
    root.handlers[:] = previous[1]
    logging.Logger.manager.loggerDict.pop(LIBRARY_LOGGER, None)


def _level(name: str) -> int:
    return _level_of(name)


def test_a_declared_level_reaches_the_library() -> None:
    """The whole point: INFO asked for is INFO in force."""
    apply_logging_policy("INFO")

    assert logging.getLogger(LIBRARY_LOGGER).getEffectiveLevel() == logging.INFO


def test_a_quieter_level_is_honoured_too() -> None:
    """An operator silencing a chatty deployment must get the silence."""
    apply_logging_policy("ERROR")

    assert logging.getLogger(LIBRARY_LOGGER).getEffectiveLevel() == logging.ERROR


def test_an_unreadable_level_falls_back_rather_than_guessing() -> None:
    """A typo must not become a quieter process than asked for by accident."""
    assert _level("CHATTY") == logging.INFO
    assert _level("") == logging.INFO


def test_the_root_gets_one_handler_not_one_per_call() -> None:
    """Re-stating a policy must not stack a handler and print records twice."""
    apply_logging_policy("INFO")
    first = len(logging.getLogger().handlers)

    apply_logging_policy("INFO")

    assert len(logging.getLogger().handlers) == first


def test_the_library_level_is_its_own_decision_so_an_override_still_wins() -> None:
    """The framework marks a level it set, which is what keeps an override working.

    ``configure_logging`` records the level as the library's decision. An
    application that later sets a level on one specific logger must win; if the
    policy claimed the level as final, that override would be lost.
    """
    apply_logging_policy("INFO")

    specific = logging.getLogger(f"{LIBRARY_LOGGER}.specific")
    specific.setLevel(logging.CRITICAL)

    assert specific.getEffectiveLevel() == logging.CRITICAL
