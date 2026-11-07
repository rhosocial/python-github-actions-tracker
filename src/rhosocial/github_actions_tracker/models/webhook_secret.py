# src/rhosocial/github_actions_tracker/models/webhook_secret.py
"""A repository's own webhook secret, sync and async twins.

GitHub gives every hook its own secret, and the service used to hold one shared
key for all of them. That made rotation a site-wide event and made the question
"does this repository have a secret?" unanswerable: a repository with no row of
its own silently fell back to the shared key, so isolation was nominal.

**There is no fallback.** A repository without a row here cannot receive
deliveries; the receiver refuses with ``secret_not_configured`` rather than
testing the signature against something else. That refusal is the point: it is
visible in the delivery ledger, where a shared key would have made a missing
per-repository secret invisible.

**The value is plaintext**, for the same reason the PAT is (see
``models/pat_credential.py``): a single-user self-hosted deployment, where
encryption at rest trades a real operational cost for a threat this shape of
deployment does not have. That decision does not travel to a hosted multi-tenant
mode, and the logging redaction applies here too.

**``state`` says who put the secret on GitHub**, which is what decides whether an
operator has anything left to do:

* ``pushed`` -- we created the hook, so the secret is already in place;
* ``pending_user`` -- we generated it and printed it once; the operator still has
  to paste it into the repository's settings;
* ``unknown`` -- the operator set it there and told us the value.

GitHub never returns a hook's secret, so taking over an existing hook can only
mean rotating it: there is no way to read the current one.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Any, ClassVar

from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import ActiveRecord, AsyncActiveRecord
from rhosocial.activerecord.query import ActiveQuery, AsyncActiveQuery
from rhosocial.activerecord.relation.async_descriptors import AsyncBelongsTo
from rhosocial.activerecord.relation.descriptors import BelongsTo

if TYPE_CHECKING:
    from .repository import AsyncRepository, Repository

logger = logging.getLogger(__name__)

#: We created the hook; the secret is already on GitHub's side.
STATE_PUSHED = "pushed"
#: We generated it and showed it once; the operator has to paste it.
STATE_PENDING_USER = "pending_user"
#: The operator set it on GitHub and told us the value.
STATE_UNKNOWN = "unknown"

STATES = (STATE_PUSHED, STATE_PENDING_USER, STATE_UNKNOWN)

#: Bytes of entropy in a generated secret. GitHub accepts any string; 32 bytes of
#: randomness is far past guessing and stays short enough to paste by hand.
GENERATED_BYTES = 32


class _SecretValues:
    """Value helpers shared by the two webhook-secret twins."""

    @staticmethod
    def generate_secret() -> str:
        """A fresh secret, in the form GitHub stores: opaque hex."""
        return secrets.token_hex(GENERATED_BYTES)

    @staticmethod
    def clean(value: Any) -> str:
        """A trimmed secret, or an empty string when nothing was supplied."""
        return str(value).strip() if value is not None else ""


class RepoWebhookSecret(_SecretValues, DefaultTimestampMixin, ActiveRecord):
    """One repository's webhook secret, synchronous twin."""

    __table_name__ = "ghat_repo_webhook_secrets"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    #: The repository this secret signs for. Unique: one secret per repository,
    #: because GitHub has one hook per repository pointing at us.
    repository_id: int = 0
    #: The secret itself. Plaintext by design; see the module docstring.
    secret: str = ""
    #: Who put it on GitHub: ``pushed`` / ``pending_user`` / ``unknown``.
    state: str = STATE_PENDING_USER
    #: GitHub's hook id, once we created one. ``None`` when the hook was made by
    #: hand (or not yet made).
    webhook_id: int | None = None

    #: Owning repository (pairs with :attr:`Repository.webhook_secret`).
    repository: ClassVar[BelongsTo[Repository]] = BelongsTo(
        foreign_key="repository_id", inverse_of="webhook_secret"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    def for_repository(cls, repository_id: int) -> RepoWebhookSecret | None:
        """The secret of one repository, or ``None`` when it has none."""
        return cls.find_one(cls.c.repository_id == repository_id)

    @classmethod
    def for_repositories(cls, repository_ids: list[int]) -> ActiveQuery:
        """Query builder over the secrets of several repositories.

        A builder rather than a result so the receiver can ask for the one
        repository it needs and a reconciliation run can ask for all of them
        without a second method.
        """
        return cls.query().where(cls.c.repository_id.in_(repository_ids))

    @classmethod
    def states_summary(cls) -> dict[str, int]:
        """How many repositories are in each state, for an operator glance."""
        summary = dict.fromkeys(STATES, 0)
        for row in cls.query().all():
            summary[row.state] = summary.get(row.state, 0) + 1
        return summary

    # ---- behavior -------------------------------------------------------------

    @classmethod
    def set(
        cls,
        repository_id: int,
        secret: str,
        *,
        state: str = STATE_PENDING_USER,
        webhook_id: int | None = None,
    ) -> RepoWebhookSecret:
        """Store a repository's secret, replacing any previous one.

        Rotation keeps the row: the repository id is what identifies the
        subscription, and its delivery history predates the secret.
        """
        value = cls.clean(secret)
        if not value:
            raise ValueError("A webhook secret cannot be empty")
        chosen = state if state in STATES else STATE_PENDING_USER
        existing = cls.for_repository(repository_id)
        if existing is not None:
            existing.secret = value
            existing.state = chosen
            if webhook_id is not None:
                existing.webhook_id = webhook_id
            existing.save()
            return existing
        record = cls(repository_id=repository_id, secret=value, state=chosen, webhook_id=webhook_id)
        record.save()
        return record

    @classmethod
    def rotate(cls, repository_id: int, *, state: str = STATE_PENDING_USER) -> RepoWebhookSecret:
        """Replace a repository's secret with a freshly generated one."""
        return cls.set(repository_id, cls.generate_secret(), state=state)

    @classmethod
    def clear(cls, repository_id: int) -> bool:
        """Drop a repository's secret; ``True`` when one existed.

        After this the repository cannot receive deliveries at all -- there is no
        fallback to fall back to. That is deliberate: the alternative is a state
        where "no secret" and "the shared secret" are indistinguishable.
        """
        existing = cls.for_repository(repository_id)
        if existing is None:
            return False
        existing.delete()
        return True

    @property
    def pending(self) -> bool:
        """Whether an operator still has to paste this secret into GitHub."""
        return self.state == STATE_PENDING_USER

    def describe(self) -> dict[str, Any]:
        """Serialize for protocol responses -- never including the secret."""
        return {
            "state": self.state,
            "webhook_id": self.webhook_id,
            "pending": self.pending,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AsyncRepoWebhookSecret(_SecretValues, DefaultTimestampMixin, AsyncActiveRecord):
    """One repository's webhook secret, asynchronous twin.

    Behavior-identical to :class:`RepoWebhookSecret`; every method performs the
    same queries and raises the same errors.
    """

    __table_name__ = "ghat_repo_webhook_secrets"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: int | None = None
    repository_id: int = 0
    secret: str = ""
    state: str = STATE_PENDING_USER
    webhook_id: int | None = None

    repository: ClassVar[AsyncBelongsTo[AsyncRepository]] = AsyncBelongsTo(
        foreign_key="repository_id", inverse_of="webhook_secret"
    )

    # ---- query domain -------------------------------------------------------

    @classmethod
    async def for_repository(cls, repository_id: int) -> AsyncRepoWebhookSecret | None:
        """The secret of one repository, or ``None`` when it has none."""
        return await cls.find_one(cls.c.repository_id == repository_id)

    @classmethod
    def for_repositories(cls, repository_ids: list[int]) -> AsyncActiveQuery:
        """Query builder over the secrets of several repositories."""
        return cls.query().where(cls.c.repository_id.in_(repository_ids))

    @classmethod
    async def states_summary(cls) -> dict[str, int]:
        """How many repositories are in each state, for an operator glance."""
        summary = dict.fromkeys(STATES, 0)
        for row in await cls.query().all():
            summary[row.state] = summary.get(row.state, 0) + 1
        return summary

    # ---- behavior -------------------------------------------------------------

    @classmethod
    async def set(
        cls,
        repository_id: int,
        secret: str,
        *,
        state: str = STATE_PENDING_USER,
        webhook_id: int | None = None,
    ) -> AsyncRepoWebhookSecret:
        """Store a repository's secret, replacing any previous one."""
        value = cls.clean(secret)
        if not value:
            raise ValueError("A webhook secret cannot be empty")
        chosen = state if state in STATES else STATE_PENDING_USER
        existing = await cls.for_repository(repository_id)
        if existing is not None:
            existing.secret = value
            existing.state = chosen
            if webhook_id is not None:
                existing.webhook_id = webhook_id
            await existing.save()
            return existing
        record = cls(repository_id=repository_id, secret=value, state=chosen, webhook_id=webhook_id)
        await record.save()
        return record

    @classmethod
    async def rotate(
        cls, repository_id: int, *, state: str = STATE_PENDING_USER
    ) -> AsyncRepoWebhookSecret:
        """Replace a repository's secret with a freshly generated one."""
        return await cls.set(repository_id, cls.generate_secret(), state=state)

    @classmethod
    async def clear(cls, repository_id: int) -> bool:
        """Drop a repository's secret; ``True`` when one existed."""
        existing = await cls.for_repository(repository_id)
        if existing is None:
            return False
        await existing.delete()
        return True

    @property
    def pending(self) -> bool:
        """Whether an operator still has to paste this secret into GitHub."""
        return self.state == STATE_PENDING_USER

    def describe(self) -> dict[str, Any]:
        """Serialize for protocol responses -- never including the secret."""
        return {
            "state": self.state,
            "webhook_id": self.webhook_id,
            "pending": self.pending,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "AsyncRepoWebhookSecret",
    "GENERATED_BYTES",
    "RepoWebhookSecret",
    "STATES",
    "STATE_PENDING_USER",
    "STATE_PUSHED",
    "STATE_UNKNOWN",
]
