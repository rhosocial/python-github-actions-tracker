# tests/core/test_authorization_cache.py
"""The authorization snapshot: what it caches, and when it re-reads.

No database and no HTTP here. The service takes its reader as a callable and
its clock as a parameter, which is what makes the interesting cases -- one
refresh per TTL, one refresh at a time, a failed read that keeps the old
snapshot -- testable without either.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any

import pytest

from rhosocial.github_actions_tracker.authorization import (
    AuthData,
    AuthorizationService,
    WebhookDecision,
    WebhookSecret,
)


class FakeLoader:
    """A reader with a clock of its own, so a test can make it slow or broken."""

    def __init__(
        self,
        data: AuthData | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self._data = data or AuthData()
        self._error = error
        self._delay = delay
        self.calls = 0

    async def __call__(self) -> AuthData:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._data


def signed(payload: bytes, secret: str) -> str:
    """The header GitHub would have sent for this body and secret."""
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def data_with(repositories: dict[str, str], *, secrets: dict[str, str] | None = None) -> AuthData:
    """An ``AuthData`` with these ``owner/repo`` names and their secrets.

    ``repositories`` is keyed by any handle a test wants to refer to it by; the
    snapshot itself is keyed by the normalized name, which is the only key the
    verifier looks up.
    """
    return AuthData(
        repositories=frozenset(name.lower() for name in repositories.values()),
        webhook_secrets={
            full_name.lower(): WebhookSecret(
                repository=repositories[handle], secret=secrets[handle], state="pushed"
            )
            for handle, full_name in repositories.items()
            if secrets and handle in secrets
        },
    )


class Clock:
    """A monotonic clock a test moves by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


def service_with(
    loader: FakeLoader,
    clock: Clock,
    *,
    ttl_seconds: float = 30.0,
    api_key: str = "",
    backoff_seconds: float = 5.0,
) -> AuthorizationService:
    service = AuthorizationService(
        api_key=api_key,
        ttl_seconds=ttl_seconds,
        backoff_seconds=backoff_seconds,
        clock=clock,
    )
    service.attach_loader(loader)
    return service


# -- TTL and single flight ----------------------------------------------------


async def test_a_fresh_snapshot_is_not_re_read(clock: Clock) -> None:
    """The whole point: one read per TTL, not one per delivery."""
    loader = FakeLoader(data_with({"acme": "acme/demo"}))
    service = service_with(loader, clock)
    await service.start()

    for _ in range(5):
        await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))
    clock.advance(5.0)
    await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))

    assert loader.calls == 1


async def test_an_expired_snapshot_is_re_read_once(clock: Clock) -> None:
    """Past the TTL the next request refreshes, and the one after it does not."""
    loader = FakeLoader(data_with({"acme": "acme/demo"}))
    service = service_with(loader, clock)
    await service.start()

    clock.advance(31.0)
    await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))
    await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))

    assert loader.calls == 2


async def test_concurrent_verifications_share_one_refresh(clock: Clock) -> None:
    """Single flight: the readers wait for the one read, they do not each make one.

    Without this, a burst of deliveries arriving the second a TTL expires would
    open as many connections as there were deliveries -- the saturation the
    snapshot exists to prevent, reintroduced by its own refresh.
    """
    loader = FakeLoader(data_with({"acme": "acme/demo"}), delay=0.05)
    service = service_with(loader, clock)
    await service.start()
    clock.advance(31.0)

    await asyncio.gather(
        *(service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret")) for _ in range(8))
    )

    assert loader.calls == 2


async def test_a_refresh_that_fails_keeps_the_previous_snapshot(clock: Clock) -> None:
    """A database hiccup must not turn every delivery into a refusal."""
    loader = FakeLoader(data_with({"acme": "acme/demo"}, secrets={"acme": "s3cret"}))
    service = service_with(loader, clock)
    await service.start()

    loader._error = RuntimeError("connection reset")
    clock.advance(31.0)
    first = await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))

    assert first.ok, "the old snapshot still verifies a delivery"
    assert service.status()["last_error"] == "RuntimeError: refresh failed"

    # And it backs off: the next request does not retry inside that window.
    clock.advance(1.0)
    await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))
    assert loader.calls == 2

    loader._data = data_with({"acme": "acme/demo"})
    clock.advance(10.0)
    loader._error = None
    loader._data = data_with({"acme": "acme/demo"}, secrets={"acme": "s3cret"})
    await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))
    assert loader.calls == 3


async def test_a_local_write_invalidates_the_snapshot(clock: Clock) -> None:
    """Rotating a secret must take effect now, not after a TTL.

    The callback the repository controller calls is this method; a rotation that
    waited out the TTL would refuse the next real delivery with the key it just
    replaced.
    """
    loader = FakeLoader(data_with({"acme": "acme/demo"}))
    service = service_with(loader, clock)
    await service.start()

    service.invalidate()
    await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))

    assert loader.calls == 2


async def test_a_replaced_snapshot_leaves_the_old_one_usable(clock: Clock) -> None:
    """Copy-on-write: a request holding the old snapshot keeps a consistent view."""
    loader = FakeLoader(data_with({"acme": "acme/demo"}))
    service = service_with(loader, clock)
    await service.start()
    before = service.snapshot
    assert before is not None

    loader._data = data_with({"acme": "acme/demo"}, secrets={"acme": "rotated"})
    service.invalidate()
    await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))
    after = service.snapshot

    assert after is not before
    assert before.revision == 1 and after.revision == 2
    assert "rotated" in [entry.secret for entry in after.webhook_secrets.values()]


# -- verification -------------------------------------------------------------


async def test_a_valid_signature_is_accepted(clock: Clock) -> None:
    loader = FakeLoader(data_with({"acme": "acme/demo"}, secrets={"acme": "s3cret"}))
    service = service_with(loader, clock)
    await service.start()

    verdict = await service.verify_webhook("acme/demo", b'{"a":1}', signed(b'{"a":1}', "s3cret"))

    assert verdict.ok
    assert verdict.repository == "acme/demo"


async def test_the_repository_name_is_matched_case_insensitively(clock: Clock) -> None:
    """GitHub spells the same repository two ways; both must find the secret."""
    loader = FakeLoader(data_with({"acme": "acme/Demo"}, secrets={"acme": "s3cret"}))
    service = service_with(loader, clock)
    await service.start()

    verdict = await service.verify_webhook("ACME/demo", b"{}", signed(b"{}", "s3cret"))

    assert verdict.ok
    assert verdict.repository == "acme/Demo", "the stored spelling is what processing uses"


async def test_an_unknown_repository_is_refused(clock: Clock) -> None:
    loader = FakeLoader(data_with({"acme": "acme/demo"}, secrets={"acme": "s3cret"}))
    service = service_with(loader, clock)
    await service.start()

    verdict = await service.verify_webhook("evil/other", b"{}", "sha256=00")

    assert verdict.decision is WebhookDecision.UNKNOWN_REPOSITORY


async def test_a_repository_without_a_secret_is_its_own_answer(clock: Clock) -> None:
    """A different operator action from a wrong signature: create one, not fix one."""
    loader = FakeLoader(data_with({"acme": "acme/demo"}))
    service = service_with(loader, clock)
    await service.start()

    verdict = await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))

    assert verdict.decision is WebhookDecision.SECRET_NOT_CONFIGURED


async def test_a_wrong_signature_is_refused(clock: Clock) -> None:
    loader = FakeLoader(data_with({"acme": "acme/demo"}, secrets={"acme": "s3cret"}))
    service = service_with(loader, clock)
    await service.start()

    verdict = await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "wrong"))

    assert verdict.decision is WebhookDecision.BAD_SIGNATURE


async def test_a_signature_signed_with_another_secret_is_refused(clock: Clock) -> None:
    loader = FakeLoader(data_with({"acme": "acme/demo"}, secrets={"acme": "s3cret"}))
    service = service_with(loader, clock)
    await service.start()

    verdict = await service.verify_webhook(
        "acme/demo", b"{}", signed(b'{"action":"completed"}', "the-old-key")
    )

    assert verdict.decision is WebhookDecision.BAD_SIGNATURE


async def test_a_malformed_signature_header_is_refused(clock: Clock) -> None:
    loader = FakeLoader(data_with({"acme": "acme/demo"}, secrets={"acme": "s3cret"}))
    service = service_with(loader, clock)
    await service.start()

    verdict = await service.verify_webhook("acme/demo", b"{}", "sha1=deadbeef")

    assert verdict.decision is WebhookDecision.BAD_SIGNATURE


async def test_an_empty_repository_name_is_refused_before_any_lookup(clock: Clock) -> None:
    loader = FakeLoader(data_with({"acme": "acme/demo"}, secrets={"acme": "s3cret"}))
    service = service_with(loader, clock)
    await service.start()

    verdict = await service.verify_webhook("", b"{}", signed(b"{}", "s3cret"))

    assert verdict.decision is WebhookDecision.UNKNOWN_REPOSITORY


async def test_a_snapshot_that_never_loaded_says_unavailable(clock: Clock) -> None:
    """Not a 401: the service cannot tell a real delivery from a forged one."""
    loader = FakeLoader(error=RuntimeError("no database"))
    service = service_with(loader, clock)
    await service.start()

    verdict = await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))

    assert service.status()["loaded"] is False
    assert verdict.decision is WebhookDecision.UNAVAILABLE


# -- the rollback switch ------------------------------------------------------


async def test_zero_ttl_disables_the_snapshot(clock: Clock) -> None:
    """``GHAT_AUTH_CACHE_TTL_SECONDS=0`` is the rollback path, not a broken one."""
    loader = FakeLoader(data_with({"acme": "acme/demo"}, secrets={"acme": "s3cret"}))
    service = service_with(loader, clock, ttl_seconds=0)

    assert service.cache_enabled is False
    assert service.status()["enabled"] is False

    verdict = await service.verify_webhook("acme/demo", b"{}", signed(b"{}", "s3cret"))

    assert verdict.decision is WebhookDecision.UNAVAILABLE
    assert loader.calls == 0, "a disabled cache loads nothing at startup"


# -- the static bearer key ----------------------------------------------------


def test_an_empty_key_is_the_open_development_mode() -> None:
    service = AuthorizationService(api_key="")

    assert service.api_key_configured is False
    assert service.verify_bearer("anything") is True
    assert service.verify_bearer("") is True


def test_a_configured_key_accepts_only_itself() -> None:
    service = AuthorizationService(api_key="topsecret")

    assert service.verify_bearer("topsecret") is True
    assert service.verify_bearer("topsecre") is False
    assert service.verify_bearer("") is False
    assert service.verify_bearer("topsecret ") is False


def test_a_non_ascii_token_is_refused_rather_than_raising() -> None:
    """The comparison is on bytes, so an unencodable candidate cannot 500."""
    service = AuthorizationService(api_key="topsecret")

    assert service.verify_bearer("töpsecrèt") is False


# -- status ------------------------------------------------------------------


async def test_the_status_carries_no_secret(clock: Clock) -> None:
    """Readiness is a public endpoint; the snapshot's report must be safe to print."""
    loader = FakeLoader(data_with({"acme": "acme/demo"}, secrets={"acme": "s3cret"}))
    service = service_with(loader, clock)
    await service.start()

    report: dict[str, Any] = service.status()

    assert report["loaded"] is True
    assert report["repositories"] == 1
    assert report["secrets"] == 1
    assert report["stale"] is False
    assert report["ttl_seconds"] == 30.0
    assert report["age_seconds"] == 0.0
    assert report["last_error"] is None
    assert "s3cret" not in repr(report)


async def test_a_stale_but_loaded_snapshot_is_reported_as_stale(clock: Clock) -> None:
    """Stale is a report, not a failure: the old snapshot still verifies."""
    loader = FakeLoader(data_with({"acme": "acme/demo"}))
    service = service_with(loader, clock)
    await service.start()
    clock.advance(45.0)

    report = service.status()

    assert report["loaded"] is True
    assert report["stale"] is True
    assert report["age_seconds"] == 45.0


async def test_an_oversized_snapshot_is_reported_not_dropped(clock: Clock) -> None:
    """A deployment bigger than the configured limit is a fact to alert on."""
    loader = FakeLoader(AuthData(repositories=frozenset(f"acme/repo{index}" for index in range(4))))
    service = service_with(loader, clock, backoff_seconds=1.0)
    service._max_entries = 3
    await service.start()

    report = service.status()

    assert report["oversized"] is True
    assert report["repositories"] == 4
