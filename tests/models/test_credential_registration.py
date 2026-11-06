# tests/models/test_credential_registration.py
"""The credential row tells a human which PAT it is, and what it may do.

Three fields exist only for people, and one measurement decides an automatic
path. They are pinned together because they fail in the same way: silently.

* **The account and the PAT's own name** are how two fine-grained tokens are told
  apart. GitHub never returns the name, so it is registered by hand; the account
  is probed.
* **The expiry** is reported by GitHub as a header. A credential past its expiry
  is not refused -- GitHub enforces that on its own side, and refusing locally
  would also close a path the operator may have kept open. It warns.
* **``can_manage_webhooks``** used to be ``is_valid`` under another name, which
  is why a missing Administration permission was only discovered when creating a
  hook failed. It is measured now, and it is tri-state on purpose: "we could not
  tell" must never be recorded as "denied", because that closes the automatic
  path with nothing in any log to say so.

Seen once: every credential reported webhook management as available, and the
first attempt to create a hook answered 401 from GitHub.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rhosocial.github_actions_tracker.credentials import get_provider
from rhosocial.github_actions_tracker.credentials.base import (
    _parse_expires_at,
    normalize_capabilities,
)

FUTURE = datetime.now(timezone.utc) + timedelta(days=30)
SOON = datetime.now(timezone.utc) + timedelta(days=2)


def _provider():
    return get_provider("pat")


class _Probe:
    """A client that answers the probe, recording what it was asked."""

    def __init__(self, *, webhooks: bool | None, expires: str = "") -> None:
        self.webhooks = webhooks
        self.expires = expires
        self.probed_repos: list[str | None] = []

    async def verify_token(self, token: str, probe_repo: str | None = None) -> dict:
        self.probed_repos.append(probe_repo)
        return {
            "is_valid": True,
            "username": "vistart",
            "can_read_actions": True,
            "can_write_actions": True,
            "can_manage_webhooks": self.webhooks,
            "expires_at": self.expires,
            "accessible_repos": 12,
        }

    async def probe_actions_write(self, repo: str) -> bool:
        return True


class TestRegistrationFields:
    async def test_the_account_and_pat_name_are_stored_and_listed(self, bound) -> None:
        probe = _Probe(webhooks=True)
        record = await _provider().add(
            token="ghp_x",
            name="work",
            client=probe,  # type: ignore[arg-type]
            github_name="vistart-ci-reader",
        )

        assert record.owner_login == "vistart", "the account comes from the probe"
        assert record.github_name == "vistart-ci-reader", "the PAT's name is what a human typed"

        listed = await _provider().list_metadata()
        assert listed[0]["owner_login"] == "vistart"
        assert listed[0]["github_name"] == "vistart-ci-reader"

    async def test_an_expiry_reported_by_github_overrides_the_caller(self, bound) -> None:
        """GitHub is the authority on when its own token stops working."""
        probe = _Probe(webhooks=True, expires=SOON.isoformat())
        record = await _provider().add(
            token="ghp_x",
            name="work",
            client=probe,  # type: ignore[arg-type]
            expires_at=FUTURE,
        )
        assert record.expires_at is not None
        assert abs((record.expires_at - SOON).total_seconds()) < 60

    async def test_no_header_keeps_the_callers_value(self, bound) -> None:
        """A classic PAT has no expiry; the header is simply absent."""
        probe = _Probe(webhooks=True, expires="")
        record = await _provider().add(
            token="ghp_x",
            name="work",
            client=probe,  # type: ignore[arg-type]
            expires_at=FUTURE,
        )
        assert record.expires_at is not None
        assert abs((record.expires_at - FUTURE).total_seconds()) < 60


class TestWebhookCapabilityIsMeasured:
    async def test_the_answer_is_stored_when_github_says_no(self, bound) -> None:
        probe = _Probe(webhooks=False)
        record = await _provider().add(
            token="ghp_x", name="work", client=probe  # type: ignore[arg-type]
        )
        assert record.can_manage_webhooks is False

    async def test_unknown_stays_unknown(self, bound) -> None:
        """A transport failure must not be recorded as a denial."""
        probe = _Probe(webhooks=None)
        record = await _provider().add(
            token="ghp_x", name="work", client=probe  # type: ignore[arg-type]
        )
        assert record.can_manage_webhooks is None, (
            "collapsing 'we could not tell' to False silently disables the " "automatic hook path"
        )

    def test_the_normalizer_keeps_the_tri_state(self) -> None:
        assert normalize_capabilities({"can_manage_webhooks": None}).can_manage_webhooks is None
        assert normalize_capabilities({"can_manage_webhooks": True}).can_manage_webhooks is True
        assert normalize_capabilities({}).can_manage_webhooks is False


class TestRefresh:
    async def test_refresh_picks_up_permissions_changed_on_github(self, bound) -> None:
        """A permission change on GitHub is invisible until we ask again."""
        provider = _provider()
        before = _Probe(webhooks=False)
        record = await provider.add(
            token="ghp_x", name="work", client=before  # type: ignore[arg-type]
        )
        assert record.can_manage_webhooks is False

        after = _Probe(webhooks=True)
        refreshed = await provider.refresh(
            record.id or 0, client=after, probe_repo="acme/demo"  # type: ignore[arg-type]
        )
        assert refreshed.can_manage_webhooks is True
        assert after.probed_repos == ["acme/demo"], "the probe needs a repository to ask about"

    async def test_refresh_does_not_change_the_token(self, bound) -> None:
        provider = _provider()
        record = await provider.add(
            token="ghp_secret_value", name="work", client=_Probe(webhooks=True)  # type: ignore[arg-type]
        )
        await provider.refresh(record.id or 0, client=_Probe(webhooks=False))  # type: ignore[arg-type]
        stored = await provider.find_by_name("work")
        assert stored is not None
        assert stored.token == "ghp_secret_value"


class TestTheWireFormatOfTheExpiry:
    """GitHub does not send ISO 8601, and reading it wrong stores nothing."""

    def test_it_reads_githubs_own_spelling(self) -> None:
        # Copy of a real header value, from GitHub's response to /user.
        parsed = _parse_expires_at("2027-10-11 11:31:04 UTC")
        assert parsed is not None, (
            "GitHub sends 'YYYY-MM-DD HH:MM:SS UTC'; fromisoformat alone returns "
            "nothing, which is how the first version stored None for a token "
            "whose expiry was right there in the header"
        )
        assert parsed.year == 2027 and parsed.month == 10 and parsed.day == 11
        assert parsed.tzinfo is not None

    def test_iso_is_still_accepted(self) -> None:
        assert _parse_expires_at("2027-10-11T11:31:04Z") is not None

    def test_a_classic_pat_sends_nothing(self) -> None:
        assert _parse_expires_at("") is None
        assert _parse_expires_at(None) is None

    def test_unparseable_values_are_dropped_not_guessed(self) -> None:
        assert _parse_expires_at("next tuesday") is None
