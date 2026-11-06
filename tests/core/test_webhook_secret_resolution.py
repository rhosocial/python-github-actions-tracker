# tests/core/test_webhook_secret_resolution.py
"""Which secret verifies a delivery, and what happens when there is none.

The receiver reads the repository out of the body *only* to choose a secret; the
body stays untrusted until the signature checks out. The three outcomes are
deliberately different facts in the ledger:

* a registered secret that matches -- ``processed``;
* a registered secret that does not -- ``signature_failed``;
* no registered secret at all -- ``secret_not_configured``.

The last one is the reason this feature exists. With one shared key, a repository
that had never been given its own secret still passed, so the gap could not be
seen from anywhere.
"""

from __future__ import annotations

from conftest import make_webhook_service

from rhosocial.github_actions_tracker.models import (
    AsyncRepository,
    AsyncRepoWebhookSecret,
    AsyncWebhookDelivery,
)
from rhosocial.github_actions_tracker.models.webhook_secret import STATE_UNKNOWN


async def _subscribe(full_name: str = "acme/demo", *, secret: str | None = None) -> None:
    owner, _, name = full_name.partition("/")
    repo = AsyncRepository(github_repo_id=99, full_name=full_name, owner=owner, name=name)
    await repo.save()
    if secret is not None:
        await AsyncRepoWebhookSecret.set(repo.id or 0, secret, state=STATE_UNKNOWN)


async def _rows() -> list[AsyncWebhookDelivery]:
    return await AsyncWebhookDelivery.query().order_by((AsyncWebhookDelivery.c.id, "ASC")).all()


class TestResolution:
    async def test_a_registered_secret_is_returned_with_its_repository(self, bound) -> None:
        await _subscribe(secret="s3cret")
        service = make_webhook_service()

        resolved = await service.resolve_secret("acme/demo")

        assert resolved == ("s3cret", "acme/demo")

    async def test_a_repository_without_one_resolves_to_nothing(self, bound) -> None:
        """No row means no secret -- there is no shared key behind it."""
        await _subscribe()
        service = make_webhook_service()

        assert await service.resolve_secret("acme/demo") is None

    async def test_an_unknown_repository_resolves_to_nothing(self, bound) -> None:
        service = make_webhook_service()
        assert await service.resolve_secret("acme/never-heard-of-it") is None

    async def test_an_unreadable_name_resolves_to_nothing(self, bound) -> None:
        service = make_webhook_service()
        assert await service.resolve_secret("") is None

    async def test_the_case_of_the_name_does_not_matter_to_the_lookup(self, bound) -> None:
        """GitHub delivers the canonical name; a lookup by the stored one must win."""
        await _subscribe(secret="s3cret")
        service = make_webhook_service()

        resolved = await service.resolve_secret("acme/demo")
        assert resolved is not None
        assert resolved[1] == "acme/demo"


class TestTheLedgerReasons:
    async def test_a_missing_secret_is_recorded_as_its_own_reason(self, bound) -> None:
        service = make_webhook_service()

        await service.record_missing_secret("workflow_run", "d-1", "acme/demo")

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].reason.startswith("secret_not_configured")
        assert rows[0].handled is False
        assert rows[0].repository_full_name == "acme/demo", (
            "the body was readable enough to name the repository, so the ledger "
            "says which one needs a secret"
        )

    async def test_it_is_distinguishable_from_a_bad_signature(self, bound) -> None:
        service = make_webhook_service()
        await service.record_missing_secret("workflow_run", "d-1", "acme/demo")
        await service.record_signature_failure("workflow_run", "d-2")

        reasons = [row.reason.split(":")[0] for row in await _rows()]
        assert reasons == ["secret_not_configured", "signature_failed"]
