# tests/services/github/test_webhook_probe.py
"""Reading hooks is the only honest way to know the Administration permission.

GitHub has no "what may this token do" endpoint, so the permission is measured by
doing something that needs it. The subtlety is what each answer means:

* 200 -- allowed;
* 403 -- refused;
* 404 -- *either* refused or a repository the token cannot see. GitHub reports
  both identically, so it is a "no": a false "yes" fails later, at the moment a
  hook is actually created, which is the failure this replaced;
* anything else -- including no answer at all -- is **unknown**, and must never
  be stored as "no". Recording a timeout as a denial disables the automatic path
  permanently and says nothing anywhere.

Seen once: the field was ``is_valid`` under another name, so every credential
claimed webhook management and the first real attempt answered 401.
"""

from __future__ import annotations

from rhosocial.github_actions_tracker.core.exceptions import GithubAPIError
from rhosocial.github_actions_tracker.services.github.client import AsyncGithubClient


class _RefusedError(GithubAPIError):
    def __init__(self, status: int) -> None:
        super().__init__(f"status {status}", status_code=status)
        self.status_code = status


class _Client(AsyncGithubClient):
    """A client whose only behaviour is the one answer under test."""

    def __init__(self, answer):  # noqa: D107 - a test double
        self.answer = answer
        self.calls: list[str] = []

    async def _request(self, method, url, token, **kwargs):  # type: ignore[override]
        self.calls.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def _probe(answer) -> bool | None:
    return await _Client(answer).probe_manage_webhooks("ghp_x", "acme/demo")


class TestTheThreeAnswers:
    async def test_listing_hooks_means_allowed(self) -> None:
        assert await _probe(object()) is True

    async def test_forbidden_means_refused(self) -> None:
        assert await _probe(_RefusedError(403)) is False

    async def test_not_found_means_refused(self) -> None:
        """GitHub cannot distinguish 'no permission' from 'no such repository'."""
        assert await _probe(_RefusedError(404)) is False

    async def test_a_server_error_is_unknown(self) -> None:
        assert await _probe(_RefusedError(500)) is None

    async def test_a_transport_failure_is_unknown(self) -> None:
        assert await _probe(ConnectionError("no route")) is None

    async def test_no_repository_is_unknown(self) -> None:
        """A first boot has no repository to ask about, so nothing is learned."""
        client = _Client(object())
        assert await client.probe_manage_webhooks("ghp_x", None) is None
        assert client.calls == [], "with nothing to ask about, nothing is called"


class TestItAsksTheRightQuestion:
    async def test_it_lists_hooks_of_the_probe_repository(self) -> None:
        client = _Client(object())
        await client.probe_manage_webhooks("ghp_x", "acme/demo")
        assert client.calls == ["/repos/acme/demo/hooks"], (
            "the cheapest endpoint that needs Administration is the hook list; "
            "creating a hook as a probe would leave garbage behind"
        )
