# src/rhosocial/github_actions_tracker/credentials/pat/provider.py
"""The PAT provider: resolve, add, rotate and release fine-grained tokens.

This is where the core's credential questions get answered:

* ``resolve`` returns the token string plus its expiry, so a repository-scoped
  read can put it in a header;
* ``capabilities`` reports what the token may do, probing GitHub at add time and
  refreshing on demand;
* ``count_bindings`` / ``release`` implement the discipline that replaces the
  foreign key the core deliberately does not have.

The exceptions raised here are the *core's* exceptions rather than private ones,
because the protocol adapters and the MCP error envelope both key off them. A
provider boundary translates; it never leaks its own vocabulary.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ...core.exceptions import (
    TokenAlreadyExistsError,
    TokenDeactivatedError,
    TokenInUseError,
    TokenInvalidError,
    TokenNotFoundError,
    ValidationError,
)
from ...models.pat_credential import PROVIDER_KIND, AsyncPatCredential, PatCredential
from ..base import (
    Capabilities,
    ResolvedCredential,
    normalize_capabilities,
    register_provider,
)
from .schema import DDL, TABLES

if TYPE_CHECKING:
    from .client import AsyncGithubClient

logger = logging.getLogger(__name__)


def _iso(value: datetime | None) -> str | None:
    """A credential timestamp as UTC ISO-8601.

    The rows come back in the connection's timezone; the REST contract and the
    MCP status payload both read these strings, so they are normalized here
    rather than in two adapters.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _clean(value: Any, default: str = "") -> str:
    """Return a trimmed string, falling back to ``default``."""
    if value is None:
        return default
    text = str(value).strip()
    return text or default


class _PatProviderBase:
    """Shared logic between the sync and async twins.

    Only the database calls differ; the validation, exception mapping and
    credential shape are written once so the twins cannot drift.
    """

    kind = PROVIDER_KIND

    @staticmethod
    def _check_usable(credential: Any, repository: str = "") -> None:
        """Raise the core exception that describes why a credential is unusable.

        A bound-but-dead credential never degrades to anonymous: the operator
        bound it deliberately, which usually means the repository is private and
        an anonymous read would be meaningless.

        Only ``is_active`` refuses. An expiry is recorded for reference and never
        blocks: GitHub enforces it on its own side, so a token past its date
        already fails at the first call with an error that names the real
        problem, while refusing here would also close a path the operator may
        have deliberately kept (a grace window after re-issuing the token, a
        clock that disagrees). The warning is emitted instead -- see
        :meth:`_warn_if_expired`.
        """
        name = getattr(credential, "name", "") or ""
        where = f" for repository '{repository}'" if repository else ""
        if not getattr(credential, "is_active", False):
            raise TokenDeactivatedError(
                f"Credential '{name}'{where} is deactivated; rotate it or rebind " "the repository",
                token_name=name,
                repository=repository,
            )
        _PatProviderBase._warn_if_expired(credential, repository)

    @staticmethod
    def _warn_if_expired(credential: Any, repository: str = "") -> None:
        """Log once per resolve when a credential is past its recorded expiry."""
        expires_at = getattr(credential, "expires_at", None)
        if expires_at is None:
            return
        # The row's value comes back in the session's zone; compare in UTC.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > datetime.now(timezone.utc):
            return
        name = getattr(credential, "name", "") or ""
        where = f" for repository '{repository}'" if repository else ""
        logger.warning(
            "Credential 'pat/%s'%s expired at %s; GitHub will reject it at the "
            "next call. Rotate it: credential pat rotate --name %s",
            name,
            where,
            expires_at.isoformat(),
            name,
        )

    def _to_resolved(self, credential: Any) -> ResolvedCredential:
        """Project a stored credential onto the core's credential shape.

        The provider pair travels with the token: the ambient PAT of a process
        is resolved once and then used to *write* bindings, and it must not
        have to look its own row up again to learn which credential it is.
        """
        return ResolvedCredential(
            token=credential.token,
            identity=credential.owner_login or credential.name or "",
            expires_at=credential.expires_at,
            can_read=bool(credential.can_read_actions is not False),
            can_write=bool(credential.can_write_actions),
            can_manage_webhooks=bool(credential.can_manage_webhooks),
            kind=self.kind,
            ref=credential.id,
        )

    @staticmethod
    def _capabilities(credential: Any) -> Capabilities:
        """Read the cached probe results as a :class:`Capabilities`."""
        return Capabilities(
            is_valid=bool(credential.is_active),
            identity=credential.owner_login or credential.name or "",
            can_read_actions=bool(credential.can_read_actions),
            can_write_actions=bool(credential.can_write_actions),
            can_manage_webhooks=bool(credential.can_manage_webhooks),
            probed_at=credential.capabilities_probed_at,
        )

    @staticmethod
    def _apply_probe(credential: Any, probed: Capabilities) -> None:
        """Store a probe result on the credential row.

        Registration fields, not logic: the account and the expiry exist so a
        human can tell two fine-grained PATs apart, and neither gates anything.
        The expiry GitHub reports wins over the one a caller passed, because
        GitHub is the authority on when its own token stops working; a caller
        value is kept when no header came back (a classic PAT has no expiry).
        """
        credential.owner_login = probed.identity or credential.owner_login
        credential.can_read_actions = probed.can_read_actions
        credential.can_write_actions = probed.can_write_actions
        credential.can_manage_webhooks = probed.can_manage_webhooks
        credential.capabilities_probed_at = probed.probed_at
        if probed.expires_at is not None:
            credential.expires_at = probed.expires_at

    @staticmethod
    def _validate_new(token: str, name: str) -> tuple[str, str]:
        """Validate the pair a caller wants to store."""
        token_value = _clean(token)
        alias = _clean(name)
        if not token_value:
            raise ValidationError("A GitHub token string is required")
        if not alias:
            raise ValidationError("A credential name is required")
        return token_value, alias


class AsyncPatProvider(_PatProviderBase):
    """Async PAT provider: the twin the service actually uses."""

    # ---- resolve / capabilities --------------------------------------------

    async def resolve(self, ref: int, repository: str = "") -> ResolvedCredential:
        """Materialize the token of credential ``ref``.

        Args:
            ref: ``ghat_pat_credentials.id``.
            repository: Repository name, used only for error messages.

        Raises:
            TokenNotFoundError: The row does not exist.
            TokenDeactivatedError: It was deactivated.
        """
        credential = await AsyncPatCredential.find_one(AsyncPatCredential.c.id == ref)
        if credential is None:
            raise TokenNotFoundError(
                f"Credential pat/{ref} does not exist"
                + (f" (bound to '{repository}')" if repository else "")
            )
        self._check_usable(credential, repository)
        return self._to_resolved(credential)

    async def capabilities(
        self,
        ref: int,
        refresh: bool = False,
        client: AsyncGithubClient | None = None,
        probe_repo: str | None = None,
    ) -> Capabilities:
        """Report what ``ref`` may do.

        Args:
            ref: ``ghat_pat_credentials.id``.
            refresh: Probe GitHub instead of using the cached result.
            client: GitHub client used for a refresh; a default one is created
                and closed when omitted.
            probe_repo: Repository used to test Actions write permission.
                Omitted leaves ``can_write_actions`` at its cached value, because
                the answer is repository-scoped and there is nothing to ask
                without one.
        """
        credential = await AsyncPatCredential.find_one(AsyncPatCredential.c.id == ref)
        if credential is None:
            raise TokenNotFoundError(f"Credential pat/{ref} does not exist")

        if not refresh and credential.capabilities_probed_at is not None:
            return self._capabilities(credential)

        if client is None:
            from .client import AsyncGithubClient as _Client

            owned = _Client()
            try:
                raw = await owned.verify_token(credential.token, probe_repo=probe_repo)
            finally:
                await owned.close()
        else:
            raw = await client.verify_token(credential.token, probe_repo=probe_repo)

        probed = normalize_capabilities(raw)
        if not probed.is_valid:
            raise TokenInvalidError(f"GitHub rejected credential '{credential.name}'")
        self._apply_probe(credential, probed)
        await credential.save()
        return probed

    async def describe(self, ref: int) -> dict[str, Any]:
        """Describe ``ref`` for status output, never exposing the secret."""
        credential = await AsyncPatCredential.find_one(AsyncPatCredential.c.id == ref)
        if credential is None:
            raise TokenNotFoundError(f"Credential pat/{ref} does not exist")
        return {
            "kind": self.kind,
            "ref": credential.id,
            "credential": f"{self.kind}/{credential.name}",
            "name": credential.name,
            "owner_login": credential.owner_login,
            "scope": credential.scope,
            "is_active": credential.is_active,
            "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
            "capabilities": self._capabilities(credential).as_dict(),
        }

    # ---- binding discipline ------------------------------------------------

    async def count_bindings(self, ref: int) -> int:
        """How many repositories this credential answers for."""
        from ...models.binding import AsyncRepositoryBinding

        return await AsyncRepositoryBinding.count_for_provider(self.kind, ref)

    async def release(self, ref: int) -> int:
        """Drop every binding of this credential; returns how many were removed."""
        from ...models.binding import AsyncRepositoryBinding

        return await AsyncRepositoryBinding.release_for_provider(self.kind, ref)

    # ---- lifecycle ---------------------------------------------------------

    async def add(
        self,
        token: str,
        name: str,
        client: AsyncGithubClient,
        scope: str = "repo,workflow",
        expires_at: datetime | None = None,
        verify: bool = True,
        probe_repo: str | None = None,
        github_name: str | None = None,
    ) -> AsyncPatCredential:
        """Register a credential, probing it first unless ``verify`` is false.

        Raises:
            ValidationError: The token or name is empty.
            TokenAlreadyExistsError: The name or the value is already stored.
            TokenInvalidError: ``verify`` was requested and GitHub rejected it.
        """
        token_value, alias = self._validate_new(token, name)

        by_name = await AsyncPatCredential.find_one(AsyncPatCredential.c.name == alias)
        if by_name is not None:
            raise TokenAlreadyExistsError(
                f"Credential '{alias}' already exists; use "
                f"'credential pat rotate --name {alias}' to replace its value",
                token_name=alias,
            )
        by_value = await AsyncPatCredential.find_one(AsyncPatCredential.c.token == token_value)
        if by_value is not None:
            raise TokenAlreadyExistsError(
                f"This token value is already stored as '{by_value.name}'",
                token_name=by_value.name,
            )

        probed = Capabilities()
        probed_fields: dict[str, Any] = {}
        if verify:
            probed = normalize_capabilities(
                await client.verify_token(token_value, probe_repo=probe_repo)
            )
            if not probed.is_valid:
                raise TokenInvalidError("GitHub rejected the token")
            # Only a real probe writes these columns. Leaving them NULL when
            # verify is off is what distinguishes "not probed yet" from
            # "probed and denied", which gating depends on.
            probed_fields = {
                "owner_login": probed.identity or None,
                "can_read_actions": probed.can_read_actions,
                "can_write_actions": probed.can_write_actions,
                "can_manage_webhooks": probed.can_manage_webhooks,
                "capabilities_probed_at": probed.probed_at,
            }
            # GitHub's header beats the caller's value: it is the authority on
            # its own token's expiry. A caller value stands when no header came
            # back, which is exactly the classic-PAT case.
            if probed.expires_at is not None:
                probed_fields["expires_at"] = probed.expires_at

        credential = AsyncPatCredential(
            name=alias,
            token=token_value,
            scope=_clean(scope, "repo,workflow"),
            github_name=_clean(github_name) or None,
            is_active=True,
            **{**probed_fields, "expires_at": probed_fields.get("expires_at", expires_at)},
        )
        await credential.save()
        logger.info("Registered credential 'pat/%s'", alias)
        return credential

    async def rotate(
        self,
        ref: int,
        new_token: str,
        client: AsyncGithubClient,
        expires_at: datetime | None = None,
        verify: bool = True,
        probe_repo: str | None = None,
    ) -> AsyncPatCredential:
        """Replace the value of a credential, keeping its identity.

        Rotation updates in place: the row -- and therefore its id -- survives,
        so every repository bound to it keeps working without re-binding.
        """
        credential = await AsyncPatCredential.find_one(AsyncPatCredential.c.id == ref)
        if credential is None:
            raise TokenNotFoundError(f"Credential pat/{ref} does not exist")

        new_value = _clean(new_token)
        if not new_value:
            raise ValidationError("A new token string is required")

        duplicate = await AsyncPatCredential.find_one(
            (AsyncPatCredential.c.token == new_value) & (AsyncPatCredential.c.id != ref)
        )
        if duplicate is not None:
            raise TokenAlreadyExistsError(
                f"The new value is already stored as '{duplicate.name}'",
                token_name=duplicate.name,
            )

        if verify:
            probed = normalize_capabilities(
                await client.verify_token(new_value, probe_repo=probe_repo)
            )
            if not probed.is_valid:
                raise TokenInvalidError("GitHub rejected the new token")
            self._apply_probe(credential, probed)

        credential.token = new_value
        if expires_at is not None:
            credential.expires_at = expires_at
        credential.is_active = True
        await credential.save()
        logger.info("Rotated credential 'pat/%s'", credential.name)
        return credential

    async def refresh(
        self,
        ref: int,
        client: AsyncGithubClient,
        probe_repo: str | None = None,
    ) -> AsyncPatCredential:
        """Re-measure a stored credential's capabilities and expiry.

        The reason this exists: a fine-grained PAT's permissions can be changed
        on GitHub at any time, and nothing tells us. Without a re-probe the row
        keeps answering with what the token could do on the day it was
        registered -- including the webhook permission, which is what decides
        whether a hook can be created automatically.

        It never changes the token value, and it never raises for a GitHub
        failure that the probe itself reports; a token GitHub rejects does raise,
        because that is a fact about the credential rather than about the probe.
        """
        credential = await AsyncPatCredential.find_one(AsyncPatCredential.c.id == ref)
        if credential is None:
            raise TokenNotFoundError(f"Credential pat/{ref} does not exist")
        probed = normalize_capabilities(
            await client.verify_token(credential.token, probe_repo=probe_repo)
        )
        if not probed.is_valid:
            raise TokenInvalidError(
                f"GitHub rejected the stored token of '{credential.name}'; "
                f"rotate it: credential pat rotate --name {credential.name}"
            )
        self._apply_probe(credential, probed)
        await credential.save()
        logger.info(
            "Re-probed credential 'pat/%s': read=%s write=%s webhooks=%s expires=%s",
            credential.name,
            probed.can_read_actions,
            probed.can_write_actions,
            probed.can_manage_webhooks,
            probed.expires_at.isoformat() if probed.expires_at else None,
        )
        return credential

    async def deactivate(self, ref: int) -> AsyncPatCredential:
        """Deactivate a credential (soft delete); existing bindings are kept."""
        credential = await AsyncPatCredential.find_one(AsyncPatCredential.c.id == ref)
        if credential is None:
            raise TokenNotFoundError(f"Credential pat/{ref} does not exist")
        credential.is_active = False
        await credential.save()
        return credential

    async def delete(self, ref: int) -> None:
        """Delete a credential, refusing while repositories still use it.

        Raises:
            TokenNotFoundError: The row does not exist.
            TokenInUseError: Bindings still point at it; unbind them first.
        """
        credential = await AsyncPatCredential.find_one(AsyncPatCredential.c.id == ref)
        if credential is None:
            raise TokenNotFoundError(f"Credential pat/{ref} does not exist")

        bound = await self.count_bindings(ref)
        if bound:
            raise TokenInUseError(
                f"Credential '{credential.name}' is used by {bound} repository "
                "subscription(s); unbind them first",
                token_id=ref,
                repository_count=bound,
            )
        await credential.delete()
        logger.info("Deleted credential 'pat/%s'", credential.name)

    async def list_metadata(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """List credentials without exposing the token values."""
        rows = await (
            AsyncPatCredential.query()
            .order_by((AsyncPatCredential.c.id, "ASC"))
            .limit(limit)
            .offset(offset)
            .all()
        )
        return [
            {
                "id": row.id,
                "kind": self.kind,
                "name": row.name,
                # ``token_name`` is kept as an alias for the REST contract,
                # which predates the provider split.
                "token_name": row.name,
                "credential": f"{self.kind}/{row.name}",
                "owner_login": row.owner_login,
                "github_name": row.github_name,
                "scope": row.scope,
                "is_active": row.is_active,
                "expires_at": _iso(row.expires_at),
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
                "can_read_actions": row.can_read_actions,
                "can_write_actions": row.can_write_actions,
                "can_manage_webhooks": row.can_manage_webhooks,
                "capabilities_probed_at": _iso(row.capabilities_probed_at),
            }
            for row in rows
        ]

    async def find_by_name(self, name: str) -> AsyncPatCredential | None:
        """Return the credential registered under ``name``, or ``None``."""
        return await AsyncPatCredential.find_one(AsyncPatCredential.c.name == name)


class PatProvider(_PatProviderBase):
    """Sync PAT provider, for parity with the async twin.

    The running service uses :class:`AsyncPatProvider`; this twin exists because
    the project keeps sync and async APIs behavior-identical.
    """

    def resolve(self, ref: int, repository: str = "") -> ResolvedCredential:
        """Materialize the token of credential ``ref`` (sync twin)."""
        credential = PatCredential.find_one(PatCredential.c.id == ref)
        if credential is None:
            raise TokenNotFoundError(
                f"Credential pat/{ref} does not exist"
                + (f" (bound to '{repository}')" if repository else "")
            )
        self._check_usable(credential, repository)
        return self._to_resolved(credential)

    def describe(self, ref: int) -> dict[str, Any]:
        """Describe ``ref`` for status output, never exposing the secret."""
        credential = PatCredential.find_one(PatCredential.c.id == ref)
        if credential is None:
            raise TokenNotFoundError(f"Credential pat/{ref} does not exist")
        return {
            "kind": self.kind,
            "ref": credential.id,
            "credential": f"{self.kind}/{credential.name}",
            "name": credential.name,
            "owner_login": credential.owner_login,
            "scope": credential.scope,
            "is_active": credential.is_active,
            "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
            "capabilities": self._capabilities(credential).as_dict(),
        }

    def capabilities(self, ref: int, refresh: bool = False) -> Capabilities:
        """Report what ``ref`` may do, using the cached probe."""
        credential = PatCredential.find_one(PatCredential.c.id == ref)
        if credential is None:
            raise TokenNotFoundError(f"Credential pat/{ref} does not exist")
        return self._capabilities(credential)

    def count_bindings(self, ref: int) -> int:
        """How many repositories this credential answers for."""
        from ...models.binding import RepositoryBinding

        return RepositoryBinding.count_for_provider(self.kind, ref)

    def release(self, ref: int) -> int:
        """Drop every binding of this credential; returns how many were removed."""
        from ...models.binding import RepositoryBinding

        return RepositoryBinding.release_for_provider(self.kind, ref)


#: The instance the core resolves through the registry.
async_provider = AsyncPatProvider()
sync_provider = PatProvider()

register_provider(async_provider, sync_provider)

__all__ = [
    "DDL",
    "TABLES",
    "AsyncPatProvider",
    "PatProvider",
    "async_provider",
    "sync_provider",
]
