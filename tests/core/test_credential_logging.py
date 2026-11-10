# tests/core/test_credential_logging.py
"""A credential's value must never reach the log at the level we run at.

``GHAT_LOG_LEVEL`` defaults to INFO, and INFO is what the service actually runs
at. So the property under test is narrower than "the value is never logged": it
is "the value is not logged at INFO", which is the level an operator's
deployment produces.

This is a regression test for a deleted mechanism. ``credentials/redaction.py``
used to raise the model and backend loggers to WARNING around each credential
write, because the ORM logged every statement's positional parameters at INFO
with their values. The ``feature/logging-execution`` branch moved that record to
DEBUG and renders an unnamed positional tuple as ``<N params: types>``, so the
guard is gone -- and these are the tests that say it can stay gone.

The canaries are deliberately not the words "token" or "secret": a substring
check must not pass because the *field name* appeared in a summary line. It has
to be the value.
"""

from __future__ import annotations

import io
import logging

import pytest

PAT_CANARY = "ghp_canary_pat_5a71e3"
SECRET_CANARY = "wsec_canary_secret_8d02b6"


class _Capture:
    """Everything the root logger emits, at a level we choose."""

    def __init__(self, level: int = logging.INFO) -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        self.level = level
        self._root = logging.getLogger()
        self._previous = self._root.level

    def __enter__(self) -> _Capture:
        self._root.addHandler(self.handler)
        self._root.setLevel(self.level)
        return self

    def __exit__(self, *_: object) -> None:
        self._root.removeHandler(self.handler)
        self._root.setLevel(self._previous)

    def text(self) -> str:
        return self.stream.getvalue()


@pytest.fixture(autouse=True)
def _library_at_info() -> None:
    """The level the service configures, applied for the duration of one test."""
    from rhosocial.activerecord.logging import configure_logging, reset_default_logging_config

    reset_default_logging_config()
    configure_logging(level=logging.INFO)
    yield
    reset_default_logging_config()


async def test_a_pat_write_does_not_log_its_value(bound) -> None:
    from rhosocial.github_actions_tracker.models import AsyncPatCredential

    with _Capture() as capture:
        credential = AsyncPatCredential(
            name="laptop",
            token=PAT_CANARY,
            github_name="alice",
            can_read_actions=True,
            can_write_actions=False,
            can_manage_webhooks=False,
            is_active=True,
        )
        await credential.save()

    assert PAT_CANARY not in capture.text(), (
        "the PAT reached the log at INFO; the per-write guard is gone, so the "
        "library must be the one keeping it out"
    )


async def test_a_webhook_secret_write_does_not_log_its_value(bound) -> None:
    from rhosocial.github_actions_tracker.models import AsyncRepository, AsyncRepoWebhookSecret

    repo = AsyncRepository(
        github_repo_id=4242,
        full_name="acme/secret-writer",
        owner="acme",
        name="secret-writer",
        is_private=True,
        default_branch="main",
    )
    await repo.save()

    with _Capture() as capture:
        await AsyncRepoWebhookSecret.set(repo.id or 0, SECRET_CANARY, state="pushed")

    assert SECRET_CANARY not in capture.text()


async def test_a_raw_backend_write_does_not_log_its_values(bound) -> None:
    """The path the old guard covered too: a direct execute with parameters."""
    from rhosocial.github_actions_tracker.models import AsyncRepository

    backend = AsyncRepository.__backend__
    await backend.execute("CREATE TABLE IF NOT EXISTS _ghat_canary (id int, token text)")
    try:
        with _Capture() as capture:
            await backend.execute(
                "INSERT INTO _ghat_canary (id, token) VALUES (%s, %s)",
                (1, PAT_CANARY),
            )
        assert PAT_CANARY not in capture.text()
    finally:
        await backend.execute("DROP TABLE IF EXISTS _ghat_canary")


async def test_the_value_is_visible_at_debug_on_purpose(bound) -> None:
    """DEBUG is where an operator asks for values; the shape must stay readable.

    The point is not that DEBUG leaks -- it is that a person debugging with
    ``--verbose`` sees *which statement* ran and how many parameters it had,
    which is enough to act on without the values being the default.
    """
    from rhosocial.github_actions_tracker.models import AsyncRepository

    backend = AsyncRepository.__backend__
    await backend.execute("CREATE TABLE IF NOT EXISTS _ghat_canary (id int, token text)")
    try:
        with _Capture(level=logging.DEBUG) as capture:
            await backend.execute(
                "INSERT INTO _ghat_canary (id, token) VALUES (%s, %s)",
                (1, PAT_CANARY),
            )
        text = capture.text()
        assert "Executing SQL" in text
        assert (
            PAT_CANARY not in text
        ), "an unnamed positional tuple should render its types, not its values"
    finally:
        await backend.execute("DROP TABLE IF EXISTS _ghat_canary")


async def test_named_parameters_are_masked_not_dropped(bound) -> None:
    """``LogPayload`` with names masks the sensitive one and keeps the rest.

    This is the escape hatch for a caller that *does* know which parameter is
    which: the value disappears, the shape of the statement does not. It is
    tested here so the two behaviours -- types-only by default, per-field masking
    on request -- are pinned next to each other.
    """
    from rhosocial.activerecord.logging import LogDataKind, LoggingConfig, LogPayload

    config = LoggingConfig(default_level=logging.INFO)
    redacted = config.redact(
        LogPayload(
            ("alice", PAT_CANARY),
            LogDataKind.QUERY_PARAMS,
            param_names=("name", "token"),
            sensitive_names=("token",),
        )
    )

    assert redacted == {"name": "alice", "token": "***MASKED***"}
