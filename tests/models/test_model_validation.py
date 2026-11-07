# tests/models/test_model_validation.py
"""Pydantic checks that guard the row boundary.

ActiveRecord models are Pydantic models, so a malformed row is refused at
construction rather than discovered as a failed insert or a join that silently
matches nothing. These tests pin the checks that are true regardless of GitHub's
vocabulary; status and conclusion are deliberately not among them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from rhosocial.github_actions_tracker.models import AsyncRepository, AsyncRunAttempt
from rhosocial.github_actions_tracker.models.pat_credential import AsyncPatCredential
from rhosocial.github_actions_tracker.models.webhook_secret import AsyncRepoWebhookSecret


class TestIdentityNumbers:
    def test_an_attempt_starts_at_one(self) -> None:
        with pytest.raises(ValidationError, match="attempt_number must be >= 1"):
            AsyncRunAttempt(attempt_number=0)

    def test_a_negative_identity_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="github_repo_id cannot be negative"):
            AsyncRepository(github_repo_id=-1, full_name="acme/demo")

    def test_zero_still_means_not_learned_yet(self) -> None:
        """An unresolved workflow id and an unknown GitHub id are 0, not an error."""
        run = AsyncRunAttempt(run_id=0, attempt_number=1)
        assert run.run_id == 0


class TestRepositoryIdentity:
    def test_full_name_is_owner_and_name(self) -> None:
        with pytest.raises(ValidationError, match="full_name must be 'owner/name'"):
            AsyncRepository(github_repo_id=1, full_name="acme")

    def test_a_well_formed_name_passes(self) -> None:
        repository = AsyncRepository(github_repo_id=1, full_name="acme/demo")
        assert repository.full_name == "acme/demo"


class TestWebhookSecretValues:
    def test_a_secret_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError, match="a webhook secret cannot be empty"):
            AsyncRepoWebhookSecret(repository_id=1, secret="")

    def test_the_state_is_our_vocabulary(self) -> None:
        with pytest.raises(ValidationError, match="state must be one of"):
            AsyncRepoWebhookSecret(repository_id=1, secret="abc", state="disabled")


class TestCredentialValues:
    def test_a_credential_needs_a_name(self) -> None:
        with pytest.raises(ValidationError, match="a credential needs a name"):
            AsyncPatCredential(name="", token="ghp_test")

    def test_a_credential_needs_a_token(self) -> None:
        with pytest.raises(ValidationError, match="a credential needs a token"):
            AsyncPatCredential(name="laptop", token="")


class TestTimestamps:
    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        attempt = AsyncRunAttempt(run_id=1, started_at=datetime(2026, 10, 10, 12, 0))
        assert attempt.started_at is not None
        assert attempt.started_at.tzinfo == timezone.utc

    def test_an_aware_timestamp_keeps_its_zone(self) -> None:
        moment = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        attempt = AsyncRunAttempt(run_id=1, started_at=moment)
        assert attempt.started_at is moment
