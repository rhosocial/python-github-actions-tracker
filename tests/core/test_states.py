# tests/core/test_states.py
"""StateMachine transition tables.

Pure-function tests: no database, no client. The matrix pins the forward-only
contract (``queued -> in_progress -> completed``), the idempotent replay rule
and the conclusion vocabularies. GitHub's spelling is translated before it gets
here; that translation is tested in ``tests/services/github/test_payloads.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rhosocial.github_actions_tracker.core.exceptions import InvalidStateTransitionError
from rhosocial.github_actions_tracker.models.states import (
    JOB_TRANSITIONS,
    KIND_JOB,
    KIND_RUN,
    KIND_STEP,
    RUN_TRANSITIONS,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_QUEUED,
    STEP_TRANSITIONS,
    TRANSITIONS_BY_KIND,
    VALID_CONCLUSIONS,
    StateMachine,
)


class _Entity:
    """Minimal stand-in for a run / job / step record."""

    def __init__(self, status: str = STATUS_QUEUED, conclusion: str | None = None) -> None:
        self.status = status
        self.conclusion = conclusion
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None


def test_transition_tables_are_forward_only() -> None:
    """Every kind moves queued -> in_progress -> completed and never back."""
    for table in (RUN_TRANSITIONS, JOB_TRANSITIONS, STEP_TRANSITIONS):
        assert set(table[STATUS_QUEUED]) == {STATUS_IN_PROGRESS, STATUS_COMPLETED}
        assert set(table[STATUS_IN_PROGRESS]) == {STATUS_COMPLETED}
        assert table[STATUS_COMPLETED] == frozenset()


def test_transitions_for_all_kinds_share_the_table() -> None:
    """Runs, jobs and steps use the same canonical table."""
    assert StateMachine.transitions_for(KIND_RUN) is RUN_TRANSITIONS
    assert StateMachine.transitions_for(KIND_JOB) is JOB_TRANSITIONS
    assert StateMachine.transitions_for(KIND_STEP) is STEP_TRANSITIONS
    assert set(TRANSITIONS_BY_KIND) == {KIND_RUN, KIND_JOB, KIND_STEP}


def test_transitions_for_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="Unknown state machine kind"):
        StateMachine.transitions_for("pipeline")


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (STATUS_QUEUED, STATUS_IN_PROGRESS),
        (STATUS_QUEUED, STATUS_COMPLETED),
        (STATUS_IN_PROGRESS, STATUS_COMPLETED),
    ],
)
def test_can_transition_allows_forward_paths(current: str, target: str) -> None:
    assert StateMachine.can_transition(current, target, RUN_TRANSITIONS) is True


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (STATUS_COMPLETED, STATUS_QUEUED),
        (STATUS_COMPLETED, STATUS_IN_PROGRESS),
        (STATUS_IN_PROGRESS, STATUS_QUEUED),
    ],
)
def test_can_transition_rejects_backwards(current: str, target: str) -> None:
    assert StateMachine.can_transition(current, target, RUN_TRANSITIONS) is False


@pytest.mark.parametrize("status", [STATUS_QUEUED, STATUS_IN_PROGRESS, STATUS_COMPLETED])
def test_can_transition_allows_idempotent_replay(status: str) -> None:
    """Re-delivering the same state must not fail (GitHub retries events)."""
    assert StateMachine.can_transition(status, status, RUN_TRANSITIONS) is True


def test_can_transition_rejects_unknown_current_status() -> None:
    assert StateMachine.can_transition("frozen", STATUS_QUEUED, RUN_TRANSITIONS) is False


def test_apply_transition_stamps_started_at() -> None:
    entity = _Entity()
    assert StateMachine.apply_transition(entity, STATUS_IN_PROGRESS) is True
    assert entity.status == STATUS_IN_PROGRESS
    assert entity.started_at is not None
    assert entity.completed_at is None


def test_apply_transition_completes_with_conclusion() -> None:
    entity = _Entity(STATUS_IN_PROGRESS)
    assert StateMachine.apply_transition(entity, STATUS_COMPLETED, "success") is True
    assert entity.status == STATUS_COMPLETED
    assert entity.conclusion == "success"
    assert entity.completed_at is not None


def test_apply_transition_allows_skip_to_completed() -> None:
    """A run observed only at completion still lands on the terminal state."""
    entity = _Entity()
    assert StateMachine.apply_transition(entity, STATUS_COMPLETED, "failure") is True
    assert entity.status == STATUS_COMPLETED
    assert entity.conclusion == "failure"
    assert entity.completed_at is not None


def test_apply_transition_replays_are_noops() -> None:
    entity = _Entity(STATUS_IN_PROGRESS)
    started = datetime.now(timezone.utc)
    entity.started_at = started
    assert StateMachine.apply_transition(entity, STATUS_IN_PROGRESS) is False
    assert entity.status == STATUS_IN_PROGRESS
    assert entity.started_at is started


def test_apply_transition_replays_refresh_conclusion() -> None:
    """A same-status completed payload may still correct the conclusion."""
    entity = _Entity(STATUS_COMPLETED, "success")
    assert StateMachine.apply_transition(entity, STATUS_COMPLETED, "failure") is False
    assert entity.conclusion == "failure"


def test_apply_transition_rejects_backwards() -> None:
    entity = _Entity(STATUS_COMPLETED, "success")
    with pytest.raises(InvalidStateTransitionError):
        StateMachine.apply_transition(entity, STATUS_QUEUED)
    assert entity.status == STATUS_COMPLETED
    assert entity.conclusion == "success"


def test_apply_transition_rejects_invalid_conclusion() -> None:
    entity = _Entity()
    with pytest.raises(InvalidStateTransitionError):
        StateMachine.apply_transition(entity, STATUS_COMPLETED, "green")
    assert entity.status == STATUS_QUEUED


def test_validate_conclusion_follows_kind_vocabulary() -> None:
    """Runs accept the full GitHub vocabulary; steps only three values."""
    StateMachine.validate_conclusion(KIND_RUN, "timed_out")
    StateMachine.validate_conclusion(KIND_JOB, "stale")
    StateMachine.validate_conclusion(KIND_STEP, "skipped")
    with pytest.raises(InvalidStateTransitionError):
        StateMachine.validate_conclusion(KIND_STEP, "timed_out")
    with pytest.raises(InvalidStateTransitionError):
        StateMachine.validate_conclusion(KIND_RUN, "green")
    StateMachine.validate_conclusion(KIND_RUN, None)
    assert "success" in VALID_CONCLUSIONS[KIND_STEP]
