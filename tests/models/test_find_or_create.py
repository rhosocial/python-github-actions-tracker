# tests/models/test_find_or_create.py
"""``find_one_or_create``: one operation for "the parent must exist".

The five-level chain writes children that point at parents, and the foreign keys
are NOT NULL: a child cannot be stored before its parent. Asking "do you have it,
and if not make it" as two statements is where the races live -- two deliveries
of one run read "no row" together, both insert, and the loser used to surface an
``IntegrityError`` for a row that exists.

What this mixin promises, and what these tests pin:

* an absent row is created with the caller's defaults, and ``created`` says so;
* a present row is returned untouched (refreshing it stays with the model's
  value entry, which knows which fields may be overwritten);
* an empty lookup is refused rather than matching an arbitrary row;
* the async and sync halves answer identically.
"""

from __future__ import annotations

import pytest

from rhosocial.github_actions_tracker.models import AsyncRepository, AsyncWorkflow


async def _repository() -> AsyncRepository:
    repository = AsyncRepository(github_repo_id=1, full_name="acme/demo", owner="acme", name="demo")
    await repository.save()
    return repository


class TestTheAsyncHalf:
    async def test_it_creates_the_row_when_absent(self, bound) -> None:
        repository = await _repository()

        workflow, created = await AsyncWorkflow.find_one_or_create(
            defaults={"name": "CI", "path": ".github/workflows/ci.yml"},
            repository_id=repository.id or 0,
            workflow_id=7,
        )

        assert created is True
        assert workflow.id is not None
        assert workflow.name == "CI"
        assert workflow.path == ".github/workflows/ci.yml"

    async def test_it_returns_the_existing_row_unharmed(self, bound) -> None:
        """Defaults are for creation only. A second call must not rename a
        workflow the first call created -- that would make the operation an
        upsert wearing a different name."""
        repository = await _repository()
        first, created = await AsyncWorkflow.find_one_or_create(
            defaults={"name": "CI", "path": "ci.yml"},
            repository_id=repository.id or 0,
            workflow_id=7,
        )
        assert created is True

        second, created_again = await AsyncWorkflow.find_one_or_create(
            defaults={"name": "Renamed", "path": "other.yml"},
            repository_id=repository.id or 0,
            workflow_id=7,
        )

        assert created_again is False
        assert second.id == first.id
        assert second.name == "CI"
        assert second.path == "ci.yml"

    async def test_an_empty_lookup_is_refused(self, bound) -> None:
        """Without a key it would return whatever row the planner produced."""
        with pytest.raises(ValueError):
            await AsyncWorkflow.find_one_or_create(defaults={"name": "CI"})
