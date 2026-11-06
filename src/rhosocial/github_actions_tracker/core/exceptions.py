# src/rhosocial/github_actions_tracker/core/exceptions.py
"""Unified exception hierarchy for the tracker."""

from __future__ import annotations

from ..models.errors import (
    GhatError,
    InvalidStateTransitionError,
    WorkflowNotFoundError,
)


class TokenNotFoundError(GhatError):
    """GitHub token not found or inactive."""

    pass


class TokenInUseError(GhatError):
    """Token cannot be deleted because repositories still reference it.

    Attributes:
        token_id: The token that is still in use.
        repository_count: Number of repositories subscribing with the token.
    """

    def __init__(self, message: str, token_id: int = 0, repository_count: int = 0) -> None:
        super().__init__(message)
        self.token_id = token_id
        self.repository_count = repository_count


class RepositoryNotFoundError(GhatError):
    """Repository not subscribed."""

    pass


class RunNotFoundError(GhatError):
    """Workflow run not found."""

    pass


class AttemptNotFoundError(GhatError):
    """The run exists, but it has no attempt with that number.

    Distinct from :class:`RunNotFoundError`: telling a caller the run is missing
    when they asked for attempt 9 of 2 sends them to check the run id, which is
    correct.
    """

    pass


class JobNotFoundError(GhatError):
    """Job not found."""

    pass


class StepNotFoundError(GhatError):
    """Step not found."""

    pass


class UpstreamUnavailableError(GhatError):
    """GitHub could not be reached, or dropped the connection.

    Distinct from every other failure here: nothing about the request is wrong,
    and nothing about the token is wrong. ``RemoteProtocolError`` (the server
    closed the connection without answering) used to escape as an untyped
    ``internal_error``, which reads to a caller as "the tracker is broken" when the
    truth is "the network between here and GitHub hiccuped; retry".
    """

    pass


class TriggerFailedError(GhatError):
    """Failed to trigger workflow.

    Attributes:
        status_code: GitHub API HTTP status code
        response_body: GitHub API response body for diagnostics
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class RunInProgressError(GhatError):
    """GitHub refused a run operation because the run is still going.

    Re-running a run that has not finished is a *concurrency* conflict: GitHub
    answers 403 with "This workflow is already running". It is not a permission
    problem -- the same credential succeeds once the run ends -- so reporting it
    as one sends the caller looking for a token to fix, when the only fix is to
    wait (or cancel).

    Attributes:
        status_code: The HTTP status GitHub answered with.
    """

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class TokenInvalidError(GhatError):
    """GitHub rejected the token.

    Raised when GitHub answers 401: the token was revoked, or it passed the
    expiry that GitHub enforces on its own side. The service does not refuse a
    credential for a recorded expiry -- that is a warning, not a gate -- so this
    always means GitHub itself said no.
    """

    pass


class TokenDeactivatedError(GhatError):
    """A repository's bound token was deactivated.

    Attributes:
        token_name: The alias of the deactivated token.
        repository: The repository whose access hit the deactivated token.
    """

    def __init__(self, message: str, token_name: str = "", repository: str = "") -> None:
        super().__init__(message)
        self.token_name = token_name
        self.repository = repository


class TokenRequiredError(GhatError):
    """The operation needs a bound credential but the repository is anonymous.

    Attributes:
        repository: The repository that had no binding, when known.
    """

    def __init__(self, message: str, repository: str = "") -> None:
        super().__init__(message)
        self.repository = repository


class TokenAlreadyExistsError(GhatError):
    """A token with the same name is already registered."""

    def __init__(self, message: str, token_name: str = "") -> None:
        super().__init__(message)
        self.token_name = token_name


class TokenPermissionError(GhatError):
    """GitHub token lacks required permissions."""

    pass


class WebhookAlreadyExistsError(GhatError):
    """Webhook already exists for this repository."""

    pass


class WebhookNotFoundError(GhatError):
    """Webhook not found."""

    pass


class GithubAPIError(GhatError):
    """GitHub API returned an unexpected error.

    Attributes:
        status_code: HTTP status code
        response_body: Response body
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class RateLimitError(GhatError):
    """GitHub API rate limit exceeded.

    Attributes:
        reset_at: Unix timestamp when rate limit resets
    """

    def __init__(self, message: str, reset_at: int = 0) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class ConcurrentUpdateError(GhatError):
    """Optimistic concurrency control conflict."""

    pass


class ValidationError(GhatError):
    """Invalid input supplied by the caller."""

    pass


#: Phrases GitHub uses when it refuses a run operation because of the run's
#: state rather than the caller's permission. Rerunning a run that is still
#: going is the common one: "This workflow is already running".
_STATE_CONFLICT_PHRASES = (
    "already running",
    "still running",
    "already in progress",
)


def is_state_conflict(message: str) -> bool:
    """Whether a 403/409 body describes a run's state, not a permission.

    Public because the protocol adapters apply it too: a transport error can
    reach them before :func:`map_api_error` does, and it must not be reported as
    a credential problem there either.
    """
    lowered = message.lower()
    return any(phrase in lowered for phrase in _STATE_CONFLICT_PHRASES)


def map_api_error(error: GhatError) -> GhatError:
    """Map a generic GitHub API error onto the typed business errors.

    Status codes are all a :class:`GithubAPIError` carries, so this is the only
    place that turns "403" into "the token lacks permission" versus "you are rate
    limited". Anything already typed passes through unchanged, which makes the
    call safe to wrap around a call site that may raise either kind.

    Args:
        error: Whatever the transport raised.

    Returns:
        A typed error: the same object when it was already typed.
    """
    if not isinstance(error, GithubAPIError):
        # Already a typed error (TokenInvalidError, RateLimitError, ...).
        return error

    message = str(error)
    lowered = message.lower()
    if error.status_code == 401:
        return TokenInvalidError("GitHub token is invalid or expired")
    if error.status_code == 403 and "rate limit" in lowered:
        return RateLimitError(message)
    if error.status_code in (403, 409) and is_state_conflict(message):
        # Concurrency, not permission: the credential is fine, the run is not
        # finished. Typed separately so no adapter can report "ask the user to
        # fix the token" for something that fixes itself in a few minutes.
        return RunInProgressError(message, error.status_code)
    if error.status_code == 403:
        return TokenPermissionError(f"Insufficient token permissions: {message}")
    if error.status_code == 422:
        return TriggerFailedError(message, error.status_code, error.response_body)
    return error


__all__ = [
    "GhatError",
    "InvalidStateTransitionError",
    "WorkflowNotFoundError",
    "TokenNotFoundError",
    "TokenInUseError",
    "RepositoryNotFoundError",
    "RunNotFoundError",
    "AttemptNotFoundError",
    "JobNotFoundError",
    "StepNotFoundError",
    "TriggerFailedError",
    "UpstreamUnavailableError",
    "TokenInvalidError",
    "TokenDeactivatedError",
    "TokenRequiredError",
    "TokenAlreadyExistsError",
    "TokenPermissionError",
    "WebhookAlreadyExistsError",
    "WebhookNotFoundError",
    "GithubAPIError",
    "RateLimitError",
    "RunInProgressError",
    "is_state_conflict",
    "ConcurrentUpdateError",
    "ValidationError",
    "map_api_error",
]
