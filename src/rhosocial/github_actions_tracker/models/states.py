# src/rhosocial/github_actions_tracker/models/states.py
"""State machine for workflow runs, jobs and steps.

GitHub reports ``queued`` / ``in_progress`` / ``completed`` for runs, jobs and
steps alike. Transitions are strictly forward: ``completed`` is terminal, so a
late arriving ``queued`` event for an already finished run is rejected instead
of resurrecting it (see :meth:`StateMachine.apply_transition`).

The values this module receives are already canonical: translating GitHub's
spelling into them belongs to
:class:`~rhosocial.github_actions_tracker.services.github.payloads.GithubPayloads`,
because a status synonym is a fact about an interface, not about a row.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .errors import InvalidStateTransitionError

# Canonical statuses (see .claude/plan/2026-10-10/01-data-model.md)
STATUS_QUEUED = "queued"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"

# Entity kinds the state machine knows about
KIND_RUN = "run"
KIND_JOB = "job"
KIND_STEP = "step"

# Conclusions accepted by GitHub. The documented vocabulary is a subset of
# "success / failure / cancelled / skipped / timed_out"; the remaining values
# (neutral, stale, startup_failure, action_required) are accepted as well so a
# real payload never breaks the state machine.
VALID_CONCLUSIONS: dict[str, frozenset[str]] = {
    KIND_RUN: frozenset(
        {
            "success",
            "failure",
            "cancelled",
            "skipped",
            "timed_out",
            "neutral",
            "stale",
            "startup_failure",
            "action_required",
        }
    ),
    KIND_JOB: frozenset({"success", "failure", "cancelled", "skipped", "neutral", "stale"}),
    KIND_STEP: frozenset({"success", "failure", "skipped"}),
}

#: Conclusions that mean "this did not pass". Everything else is not a failure:
#: ``None`` included, because GitHub leaves a step's conclusion unset while its
#: metadata is still being materialized, and treating that as a failure invents
#: one. Written as a list of failures rather than "not in the passing set" for
#: exactly that reason.
FAILED_CONCLUSIONS: frozenset[str] = frozenset(
    {"failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale"}
)

#: The canonical ``queued -> in_progress -> completed`` table. Runs, jobs and
#: steps share the same shape; each keeps its own dictionary so a future rule can
#: diverge without touching the others.
RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_QUEUED: frozenset({STATUS_IN_PROGRESS, STATUS_COMPLETED}),
    STATUS_IN_PROGRESS: frozenset({STATUS_COMPLETED}),
    STATUS_COMPLETED: frozenset(),
}
JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_QUEUED: frozenset({STATUS_IN_PROGRESS, STATUS_COMPLETED}),
    STATUS_IN_PROGRESS: frozenset({STATUS_COMPLETED}),
    STATUS_COMPLETED: frozenset(),
}
STEP_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_QUEUED: frozenset({STATUS_IN_PROGRESS, STATUS_COMPLETED}),
    STATUS_IN_PROGRESS: frozenset({STATUS_COMPLETED}),
    STATUS_COMPLETED: frozenset(),
}

TRANSITIONS_BY_KIND: dict[str, dict[str, frozenset[str]]] = {
    KIND_RUN: RUN_TRANSITIONS,
    KIND_JOB: JOB_TRANSITIONS,
    KIND_STEP: STEP_TRANSITIONS,
}


class StateMachine:
    """Validate and apply forward-only status transitions."""

    @staticmethod
    def transitions_for(kind: str) -> Mapping[str, frozenset[str]]:
        """Return the transition table for an entity kind.

        Args:
            kind: One of ``run``, ``job`` or ``step``.

        Returns:
            Mapping of current status to the set of reachable statuses.

        Raises:
            ValueError: If ``kind`` is unknown.
        """
        try:
            return TRANSITIONS_BY_KIND[kind]
        except KeyError:
            raise ValueError(f"Unknown state machine kind '{kind}'") from None

    @staticmethod
    def can_transition(
        current: str,
        target: str,
        transitions: Mapping[str, frozenset[str]],
    ) -> bool:
        """Check whether moving from ``current`` to ``target`` is allowed.

        A transition to the same status is always allowed (idempotent replay
        of a GitHub event must not fail).
        """
        if current == target:
            return True
        return target in transitions.get(current, frozenset())

    @staticmethod
    def validate_conclusion(kind: str, conclusion: str | None) -> None:
        """Validate a conclusion against the vocabulary of ``kind``.

        Raises:
            InvalidStateTransitionError: If the conclusion is not accepted.
        """
        if conclusion is None:
            return
        allowed = VALID_CONCLUSIONS.get(kind, frozenset())
        if conclusion not in allowed:
            raise InvalidStateTransitionError(
                f"Invalid {kind} conclusion '{conclusion}' " f"(expected one of {sorted(allowed)})"
            )

    @classmethod
    def apply_transition(
        cls,
        entity: Any,
        new_status: str | None,
        new_conclusion: str | None = None,
        kind: str = KIND_RUN,
    ) -> bool:
        """Apply a state transition to a model instance.

        Args:
            entity: Model instance exposing ``status`` (and, for runs/jobs/steps,
                ``conclusion``, ``started_at`` and ``completed_at``).
            new_status: Status reported by GitHub.
            new_conclusion: Conclusion reported by GitHub (only set on completion).
            kind: Entity kind, selecting the transition table and vocabulary.

        Returns:
            ``True`` when the status actually changed, ``False`` when the
            reported state matched the stored one.

        Raises:
            InvalidStateTransitionError: If the transition is not allowed or
                the conclusion is not accepted for ``kind``.
        """
        current = getattr(entity, "status", None) or STATUS_QUEUED
        target = new_status or STATUS_QUEUED
        if target == current:
            # Idempotent replay: refresh the conclusion only.
            if target == STATUS_COMPLETED:
                cls._apply_conclusion(entity, kind, new_conclusion)
            return False

        if not cls.can_transition(current, target, cls.transitions_for(kind)):
            raise InvalidStateTransitionError(
                f"Cannot transition {kind} from '{current}' to '{target}'"
            )

        if target == STATUS_COMPLETED:
            cls._apply_conclusion(entity, kind, new_conclusion)
            cls._stamp(entity, "completed_at")
        elif target == STATUS_IN_PROGRESS:
            cls._stamp(entity, "started_at")

        entity.status = target
        return True

    @staticmethod
    def _apply_conclusion(entity: Any, kind: str, conclusion: str | None) -> None:
        """Attach a conclusion to a completed entity, keeping the old one."""
        if conclusion is None:
            return
        StateMachine.validate_conclusion(kind, conclusion)
        entity.conclusion = conclusion

    @staticmethod
    def _stamp(entity: Any, field: str) -> None:
        """Set a timestamp field to now() unless it already holds a value."""
        if not hasattr(entity, field):
            return
        if getattr(entity, field, None) is None:
            setattr(entity, field, datetime.now(timezone.utc))


__all__ = [
    "JOB_TRANSITIONS",
    "KIND_JOB",
    "KIND_RUN",
    "KIND_STEP",
    "RUN_TRANSITIONS",
    "STATUS_COMPLETED",
    "STATUS_IN_PROGRESS",
    "STATUS_QUEUED",
    "STEP_TRANSITIONS",
    "TRANSITIONS_BY_KIND",
    "FAILED_CONCLUSIONS",
    "VALID_CONCLUSIONS",
    "StateMachine",
]
