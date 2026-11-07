# src/rhosocial/github_actions_tracker/models/mixins.py
"""Model mixins for the operations ActiveRecord leaves to the caller.

The ORM gives every model ``query()``, ``find_one()`` and ``save()``, and that is
the whole contract this project needs -- but two of its flows keep writing the
same three statements by hand:

* **ensure a parent exists** before writing a child that points at it. A
  ``workflow_job`` delivery names an attempt nobody has stored, and the foreign
  key is ``NOT NULL``: the row has to exist first, and asking "do you have it, and
  if not make it" is one operation, not two.
* **absorb a race.** Two deliveries of one run can arrive seconds apart, both read
  "no row", both insert, and the loser hits the unique constraint. The correct
  answer is to re-read and use the winner, which is what the insert path already
  does inline.

``find_one_or_create`` is that operation: look up by equality on the natural key,
return the row and whether *this call* created it. It never updates an existing
row -- refreshing a stored row from new values stays with each model's value
entry (``refresh`` / ``record``), because only the model knows which fields may
overwrite and which are history.

Sync and async are separate mixins, not one that branches: the project's
sync/async parity rule says the sync half exists for library users and this
service calls the async half, so each method is written once per half rather than
growing an ``if inspect.isawaitable`` seam inside one of them.

Usage::

    class Job(FindOrCreateMixin, DefaultTimestampMixin, ActiveRecord):
        ...

    job, created = Job.find_one_or_create(
        defaults={"name": "", "status": "queued"},
        attempt_id=attempt.id,
        github_job_id=payload["id"],
    )
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from rhosocial.activerecord.backend.errors import IntegrityError

__all__ = ["AsyncFindOrCreateMixin", "FindOrCreateMixin"]


class FindOrCreateMixin:
    """``find_one_or_create`` for a synchronous ActiveRecord model."""

    c: ClassVar[Any]  # supplied by the concrete model's FieldProxy

    @classmethod
    def _lookup_condition(cls, lookup: Mapping[str, Any]) -> Any:
        """The equality predicate for ``lookup``, built through the field proxy.

        One condition rather than SQL text: the proxy knows the column names and
        the dialect, and a key that is not a column fails loudly at build time
        instead of matching nothing at run time.
        """
        if not lookup:
            raise ValueError(
                "find_one_or_create needs at least one lookup column; without one "
                "it would return an arbitrary row"
            )
        condition: Any = None
        for field, value in lookup.items():
            comparison = getattr(cls.c, field) == value
            condition = comparison if condition is None else condition & comparison
        return condition

    @classmethod
    def find_one_or_create(
        cls,
        *,
        defaults: Mapping[str, Any] | None = None,
        **lookup: Any,
    ) -> tuple[Any, bool]:
        """Return ``(row, created)`` for the row matching ``lookup``.

        ``lookup`` is the natural key, compared by equality. ``defaults`` fill
        the remaining columns **on creation only**; an existing row is returned
        untouched.

        ``created`` is True only when this call inserted the row. Losing the
        insert race returns ``(winner, False)``, because this call did not create
        anything -- the other one did.

        Raises:
            ValueError: ``lookup`` is empty.
            IntegrityError: the insert failed for a reason the lookup does not
                explain (another unique constraint), re-raised rather than
                disguised as "already exists".
        """
        condition = cls._lookup_condition(lookup)
        existing = cls.find_one(condition)
        if existing is not None:
            return existing, False

        row = cls(**{**(defaults or {}), **lookup})
        try:
            row.save()
        except IntegrityError:
            # Someone else inserted between the read and this insert. Re-read and
            # answer with the winner; if the lookup still finds nothing, the
            # constraint that fired was not this key and must surface.
            winner = cls.find_one(condition)
            if winner is None:
                raise
            return winner, False
        return row, True


class AsyncFindOrCreateMixin:
    """``find_one_or_create`` for an asynchronous ActiveRecord twin."""

    c: ClassVar[Any]  # supplied by the concrete model's FieldProxy

    @classmethod
    def _lookup_condition(cls, lookup: Mapping[str, Any]) -> Any:
        """The equality predicate for ``lookup``; see the sync twin."""
        if not lookup:
            raise ValueError(
                "find_one_or_create needs at least one lookup column; without one "
                "it would return an arbitrary row"
            )
        condition: Any = None
        for field, value in lookup.items():
            comparison = getattr(cls.c, field) == value
            condition = comparison if condition is None else condition & comparison
        return condition

    @classmethod
    async def find_one_or_create(
        cls,
        *,
        defaults: Mapping[str, Any] | None = None,
        **lookup: Any,
    ) -> tuple[Any, bool]:
        """Return ``(row, created)``; see :meth:`FindOrCreateMixin.find_one_or_create`."""
        condition = cls._lookup_condition(lookup)
        existing = await cls.find_one(condition)
        if existing is not None:
            return existing, False

        row = cls(**{**(defaults or {}), **lookup})
        try:
            await row.save()
        except IntegrityError:
            winner = await cls.find_one(condition)
            if winner is None:
                raise
            return winner, False
        return row, True
