# src/rhosocial/github_actions_tracker/models/validators.py
"""Pydantic checks shared by the row models.

ActiveRecord models are Pydantic models, so an invariant that holds for every
row can be refused at construction instead of discovered as a bad insert, a
failed join or a comparison between a naive and an aware timestamp.

Only invariants that are true regardless of GitHub's vocabulary live here. A
status or conclusion is *not* validated: GitHub may add one, and the state
machine is where an unknown value is rejected explicitly rather than silently.

The "every timestamp is timezone-aware" rule used to be checked by a local
``UtcTimestampMixin``; it is not here any more, because the library now ships
one in ``rhosocial.activerecord.field`` and it does strictly more -- it also
normalizes the naive values a database driver hands back, which the local copy
never saw. The models import it from there.
"""

from __future__ import annotations

from pydantic import ValidationInfo, field_validator


class IdentityMixin:
    """Identity numbers a row carries: never negative, attempts start at one.

    ``0`` is allowed where it means "not learned yet" (an unresolved workflow
    id, a payload that carried no GitHub id). A negative number is never a real
    identity, so it is refused at the boundary.
    """

    @field_validator(
        "workflow_id",
        "repository_id",
        "run_id",
        "attempt_id",
        "job_id",
        "step_id",
        "github_run_id",
        "github_job_id",
        "github_step_id",
        "github_repo_id",
        "run_number",
        "number",
        "pr_number",
        "webhook_id",
        check_fields=False,
    )
    @classmethod
    def _identity_not_negative(cls, value: int | None, info: ValidationInfo) -> int | None:
        """Refuse a negative identity; ``None`` and ``0`` pass."""
        if value is not None and value < 0:
            raise ValueError(f"{info.field_name} cannot be negative")
        return value

    @field_validator("attempt_number", check_fields=False)
    @classmethod
    def _attempt_number_at_least_one(cls, value: int) -> int:
        """GitHub numbers executions from 1; there is no attempt 0."""
        if value < 1:
            raise ValueError("attempt_number must be >= 1")
        return value


__all__ = ["IdentityMixin"]
