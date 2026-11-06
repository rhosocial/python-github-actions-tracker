# src/rhosocial/github_actions_tracker/github/client.py
"""GitHub REST API clients, sync and async twins.

The two classes are behavior-identical: same method names, same semantics.
The sync twin exists because the project rule is sync/async parity even
where only the async one is exercised by the running service.

Shared, I/O-free logic (header building, error mapping, envelope unwrapping,
Link-header parsing, rate-limit checking) lives in module-level functions so
neither twin re-implements it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import time
from collections import OrderedDict
from typing import Any

import httpx2

from ...core.exceptions import (
    GhatError,
    GithubAPIError,
    RateLimitError,
    RunInProgressError,
    TokenInvalidError,
    TokenPermissionError,
    TriggerFailedError,
    UpstreamUnavailableError,
    WebhookNotFoundError,
    is_state_conflict,
)

#: Methods a transport failure may be retried on. A POST is deliberately absent:
#: a dispatch that timed out may have been accepted, and retrying it starts a
#: second run. The caller is told "upstream unavailable" and decides.
RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

# ---------------------------------------------------------------------------
# Outbound call metrics
# ---------------------------------------------------------------------------

#: The status code of the call this scope is timing, so the histogram's label is
#: the answer GitHub gave rather than a guess made before it answered. A
#: ``ContextVar`` rather than a return value because the request path already
#: returns the response, and an exception path has no response to read at all --
#: and "it never answered" is itself a status worth counting.
_observed_status: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "tracker_observed_status", default=None
)


def _operation_of(url: str) -> str:
    """The resource group a URL names, for a metric label.

    ``/repos/o/r/actions/runs/123`` is the ``runs`` group. The repository, the
    id and the ``actions`` segment between them are dropped: a repository in a
    label turns one metric into a table of series nobody can aggregate, and
    ``actions`` is constant across this whole API.
    """
    parts = [part for part in str(url).split("/") if part]
    for index, part in enumerate(parts):
        if part in ("repos", "orgs") and index + 3 < len(parts):
            # past /repos/{owner}/{name} and the constant ``actions`` segment.
            rest = [item for item in parts[index + 3 :] if item != "actions"]
            return rest[0] if rest else "unknown"
    return parts[0] if parts else "unknown"


@contextlib.contextmanager
def _observed(method: str, url: str):
    """Time one outbound call and record the answer it got.

    Never raises: a metrics problem must not take a GitHub call down, which is
    the same rule the retry logic follows.
    """
    from ...observability.definitions import GITHUB_API_SECONDS, GITHUB_API_TOTAL

    started = time.monotonic()
    token = _observed_status.set(None)
    try:
        yield
    finally:
        # Read before resetting: the answer this scope was given is what the
        # label says, and resetting first would report every call as an error.
        status = _observed_status.get()
        _observed_status.reset(token)
        code = str(status) if status is not None else "error"
        operation = _operation_of(url)
        GITHUB_API_TOTAL.inc(operation=operation, status_code=code)
        GITHUB_API_SECONDS.observe(
            time.monotonic() - started, operation=operation, status_code=code
        )


@contextlib.asynccontextmanager
async def _observed_async(method: str, url: str):
    """The async twin of :func:`_observed`."""
    from ...observability.definitions import GITHUB_API_SECONDS, GITHUB_API_TOTAL

    started = time.monotonic()
    token = _observed_status.set(None)
    try:
        yield
    finally:
        # Read before resetting: the answer this scope was given is what the
        # label says, and resetting first would report every call as an error.
        status = _observed_status.get()
        _observed_status.reset(token)
        code = str(status) if status is not None else "error"
        operation = _operation_of(url)
        GITHUB_API_TOTAL.inc(operation=operation, status_code=code)
        GITHUB_API_SECONDS.observe(
            time.monotonic() - started, operation=operation, status_code=code
        )


def _note_status(response: Any) -> None:
    """Publish the status code of an answered call."""
    try:
        _observed_status.set(int(getattr(response, "status_code", 0)))
    except Exception:  # noqa: BLE001 - a metric label must not fail a request
        pass


#: Shared defaults; both twins read these instead of carrying their own copies.
BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT = 30.0


def _total_from_link_header(response: Any) -> int:
    """Read a collection's size from GitHub's ``Link`` header.

    GitHub does not return a total count for ``/user/repos``; it paginates and
    advertises the last page in the ``Link`` header. Combined with a
    ``per_page=1`` request, that last page number *is* the item count -- one
    cheap request instead of walking every page.

    Args:
        response: The HTTP response of a paginated collection.

    Returns:
        The item count, or the number of rows on this page when no ``Link``
        header is present (a single short page).
    """
    import re

    link = response.headers.get("link") or response.headers.get("Link") or ""
    match = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link)
    if match:
        return int(match.group(1))
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - a malformed body is "unknown"
        return 0
    return len(body) if isinstance(body, list) else 0


#: The ref the Actions-write probe dispatches to. It never exists, so the
#: dispatch cannot start a run: GitHub validates the ref after the permission
#: check, which is exactly the signal the probe reads.
PROBE_REF = "refs/heads/__tracker_permission_probe__"
DEFAULT_PER_PAGE = 30
MAX_PER_PAGE = 100
DEFAULT_WEBHOOK_EVENTS = ["workflow_run", "workflow_job"]


# ---------------------------------------------------------------------------
# Shared helpers (pure, no I/O)
# ---------------------------------------------------------------------------


class _ConditionalCache:
    """ETag -> body for GET requests, bounded and keyed per credential.

    GitHub does not count a ``304 Not Modified`` against the rate limit, so a
    service that reads the source on every query can stay inside the quota: the
    request is still made, the answer still comes from GitHub, and an unchanged
    resource costs a round trip instead of a quota unit.

    The key includes a fingerprint of the credential, because the same URL
    answers differently for a token that can see a private repository and one
    that cannot.
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        self._entries: OrderedDict[tuple[str, str, str, str], tuple[str, bytes, str]] = (
            OrderedDict()
        )

    @staticmethod
    def key(method: str, url: str, params: Any, token: str | None) -> tuple[str, str, str, str]:
        """A stable identity for one conditional GET."""
        fingerprint = hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:16]
        return (method.upper(), url, repr(sorted((params or {}).items())), fingerprint)

    def etag(self, key: tuple[str, str, str, str]) -> str | None:
        entry = self._entries.get(key)
        return None if entry is None else entry[0]

    def remember(self, key: tuple[str, str, str, str], response: httpx2.Response) -> None:
        etag = response.headers.get("ETag")
        if not etag:
            return
        self._entries[key] = (etag, response.content, response.headers.get("Link", ""))
        self._entries.move_to_end(key)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)

    def replay(self, key: tuple[str, str, str, str]) -> httpx2.Response | None:
        """The stored body as a 200, with the pagination header it carried."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        _, body, link = entry
        headers = {"Link": link} if link else {}
        return httpx2.Response(200, content=body, headers=headers)

    def forget(self, key: tuple[str, str, str, str]) -> None:
        self._entries.pop(key, None)


def auth_headers(token: str | None) -> dict[str, str]:
    """Authorization headers for a token.

    An empty token means anonymous access: GitHub serves public repositories'
    CI data without authentication (60 requests/hour, shared per IP), which is
    the contract of an anonymous subscription. The header is omitted so the
    request is not misread as a malformed credential.
    """
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def parse_link_header(link_header: str) -> dict[str, str]:
    """Parse a GitHub ``Link`` header into ``{rel: url}``."""
    links: dict[str, str] = {}
    if not link_header:
        return links
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url_part = section[0].strip().strip("<>")
        rel_part = section[1].strip()
        if 'rel="next"' in rel_part:
            links["next"] = url_part
        elif 'rel="prev"' in rel_part:
            links["prev"] = url_part
        elif 'rel="last"' in rel_part:
            links["last"] = url_part
        elif 'rel="first"' in rel_part:
            links["first"] = url_part
    return links


def unwrap_envelope(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the item list from a GitHub list envelope.

    GitHub wraps list endpoints as ``{"total_count": N, "workflow_runs": [...]}``
    (same shape for jobs/workflows); single-resource endpoints return the
    object itself. The item list is returned; a single resource becomes a
    one-element list so callers always see rows.
    """
    for key in ("workflow_runs", "workflow_jobs", "workflows", "jobs", "items", "repos"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [data]


def check_rate_limit(response: httpx2.Response) -> None:
    """Raise :class:`RateLimitError` when the response reports exhaustion."""
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset = response.headers.get("X-RateLimit-Reset")
    if remaining is not None and reset is not None and int(remaining) == 0:
        raise RateLimitError(
            f"GitHub API rate limit exceeded. Resets at {reset}",
            reset_at=int(reset),
        )


def handle_error(response: httpx2.Response) -> None:
    """Map a >=400 response onto the typed exception hierarchy."""
    status_code = response.status_code
    try:
        body = response.json()
        message = body.get("message", response.text)
    except Exception:
        message = response.text

    if status_code == 401:
        raise TokenInvalidError("GitHub token is invalid or expired")
    if status_code == 403:
        if "rate limit" in message.lower():
            reset = response.headers.get("X-RateLimit-Reset", "0")
            raise RateLimitError(
                f"GitHub API rate limit exceeded. Resets at {reset}",
                reset_at=int(reset),
            )
        if is_state_conflict(message):
            # A 403 for the run's state, not the caller's permission. This has to
            # be typed here: a TokenPermissionError is already typed, so the
            # later ``map_api_error`` passes it through unchanged -- which is how
            # "This workflow is already running" reached callers as "insufficient
            # token permissions, ask the user to re-authorize".
            raise RunInProgressError(message, status_code)
        raise TokenPermissionError(f"Insufficient token permissions: {message}")
    if status_code == 404:
        raise GithubAPIError(
            f"Resource not found: {message}",
            status_code=status_code,
            response_body=response.text,
        )
    if status_code == 422:
        raise TriggerFailedError(message, status_code, response.text)
    if status_code in (502, 503, 504):
        raise UpstreamUnavailableError(f"GitHub is unavailable ({status_code}): {message}")
    if status_code >= 500:
        raise GithubAPIError(
            f"GitHub API server error: {message}",
            status_code=status_code,
            response_body=response.text,
        )
    raise GithubAPIError(
        f"GitHub API error: {message}",
        status_code=status_code,
        response_body=response.text,
    )


def _dispatch_error(exc: Exception, repo_full_name: str, hook_id: int) -> Exception:
    """Refine a raw API error from a webhook call, or return it unchanged."""
    if isinstance(exc, GithubAPIError) and exc.status_code == 404:
        return WebhookNotFoundError(f"Webhook {hook_id} not found in '{repo_full_name}'")
    return exc


# ---------------------------------------------------------------------------
# Sync client
# ---------------------------------------------------------------------------


class GithubClient:
    """Synchronous GitHub REST client.

    Behavior-identical to :class:`AsyncGithubClient`; every method performs
    the same request and raises the same exceptions. Exists for the
    project's sync/async parity rule even though the running service uses
    the async twin.
    """

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._client: httpx2.Client | None = None
        self._conditional = _ConditionalCache()

    def __enter__(self) -> GithubClient:
        self._ensure_client()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _ensure_client(self) -> None:
        if self._client is None or self._client.is_closed:
            self._client = httpx2.Client(
                base_url=BASE_URL,
                timeout=self._timeout,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )

    def close(self) -> None:
        """Close the HTTP client and release resources."""
        if self._client and not self._client.is_closed:
            self._client.close()
        self._client = None

    def _request(
        self,
        method: str,
        url: str,
        token: str | None,
        **kwargs: Any,
    ) -> httpx2.Response:
        """Execute an HTTP request with retry logic."""
        with _observed(method, url):
            return self._request_inner(method, url, token, **kwargs)

    def _request_inner(
        self,
        method: str,
        url: str,
        token: str | None,
        **kwargs: Any,
    ) -> httpx2.Response:
        self._ensure_client()
        headers = auth_headers(token)
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))
        cache_key = (
            self._conditional.key(method, url, kwargs.get("params"), token)
            if method.upper() == "GET"
            else None
        )
        if cache_key is not None:
            etag = self._conditional.etag(cache_key)
            if etag:
                headers["If-None-Match"] = etag
        for attempt in range(self._max_retries):
            try:
                response = self._client.request(method, url, headers=headers, **kwargs)
                if cache_key is not None:
                    response = self._resolve_conditional(cache_key, headers, response, kwargs)
                check_rate_limit(response)
                if response.status_code >= 400:
                    _note_status(response)
                    handle_error(response)
                _note_status(response)
                return response
            except httpx2.TransportError as exc:
                # ``TransportError`` is wider than "network error": it also covers
                # ``RemoteProtocolError`` (server closed the connection without
                # answering), which used to escape untyped.
                retryable = method.upper() in RETRYABLE_METHODS
                if retryable and attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay * (2**attempt))
                    continue
                raise UpstreamUnavailableError(
                    f"GitHub did not answer ({exc.__class__.__name__}: {exc})"
                    + (
                        f" after {self._max_retries} attempts"
                        if retryable
                        else "; the request was not retried because it is not idempotent"
                    )
                ) from exc
        raise GithubAPIError("Unexpected error in request")

    def _resolve_conditional(
        self,
        cache_key: tuple[str, str, str, str],
        headers: dict[str, str],
        response: httpx2.Response,
        kwargs: dict[str, Any],
    ) -> httpx2.Response:
        """Turn a 304 into the stored body, and remember a fresh one.

        A 304 whose body is no longer stored (a restart between the two calls)
        is retried once without the condition, so an optimisation can never turn
        a readable resource into a failure.
        """
        if response.status_code == 304:
            replayed = self._conditional.replay(cache_key)
            if replayed is not None:
                return replayed
            self._conditional.forget(cache_key)
            headers.pop("If-None-Match", None)
            return self._client.request("GET", cache_key[1], headers=headers, **kwargs)
        if response.status_code == 200:
            self._conditional.remember(cache_key, response)
        return response

    def _paginate(
        self,
        method: str,
        url: str,
        token: str | None,
        params: dict[str, Any] | None = None,
        all_pages: bool = True,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch pages of a paginated endpoint.

        Follows the ``next`` Link until every page is fetched. With
        ``all_pages=False`` only the first page is returned -- a caller
        asking for a small page size usually means an upper bound, not one
        HTTP call per row.
        """
        results: list[dict[str, Any]] = []
        current_url: str | None = url
        current_params = params.copy() if params else {}
        while current_url:
            response = self._request(
                method,
                current_url,
                token,
                params=current_params if current_url == url else None,
                **kwargs,
            )
            data = response.json()
            if isinstance(data, list):
                results.extend(data)
            elif isinstance(data, dict):
                results.extend(unwrap_envelope(data))
            if not all_pages:
                break
            links = parse_link_header(response.headers.get("Link", ""))
            current_url = links.get("next")
            current_params = {}
        return results

    # -- workflow dispatch ---------------------------------------------------

    def trigger_workflow(
        self,
        token: str,
        repo_full_name: str,
        workflow_id: str,
        ref: str,
        inputs: dict[str, str] | None = None,
        return_run_details: bool = True,
    ) -> dict[str, Any]:
        """Trigger a workflow run and, when GitHub allows, return its id.

        ``return_run_details`` makes GitHub answer ``200`` with the created run's
        identifiers instead of ``204 No Content``. That difference matters: without
        it the run id has to be guessed by listing recent runs and picking the one
        that looks new, which races with any concurrent dispatch.

        The parameter is requested but not required -- GitHub Enterprise Server may
        ignore it and answer 204 -- so the caller keeps a fallback.

        Args:
            token: Credential used for the dispatch.
            repo_full_name: ``owner/repo``.
            workflow_id: Workflow file name, path or numeric id.
            ref: Branch or tag to run on.
            inputs: ``workflow_dispatch`` inputs.
            return_run_details: Ask GitHub for the created run's identifiers.

        Returns:
            ``{"id": <run id>, "html_url": ...}`` when GitHub answered with
            details, else ``{"run_id": 0, "status": "queued"}``.
        """
        url = f"/repos/{repo_full_name}/actions/workflows/{workflow_id}/dispatches"
        payload: dict[str, Any] = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs
        if return_run_details:
            payload["return_run_details"] = True
        response = self._request("POST", url, token, json=payload)
        if response.status_code in (202, 204) or not response.content:
            return {"run_id": 0, "status": "queued"}
        body = response.json()
        # GitHub spells the id workflow_run_id; older/enterprise shapes use id.
        run_id = body.get("workflow_run_id") or body.get("id") or 0
        return {
            "id": run_id,
            "run_id": run_id,
            "html_url": body.get("html_url", ""),
            "run_url": body.get("run_url", ""),
        }

    # -- run queries ----------------------------------------------------------

    def rerun_workflow(self, token: str, repo_full_name: str, run_id: int) -> None:
        """Re-run a finished workflow run. See the async twin."""
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/rerun"
        self._request("POST", url, token)

    def rerun_failed_jobs(self, token: str, repo_full_name: str, run_id: int) -> None:
        """Re-run only the failed jobs of a finished run. See the async twin."""
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/rerun-failed-jobs"
        self._request("POST", url, token)

    def cancel_workflow_run(self, token: str, repo_full_name: str, run_id: int) -> None:
        """Cancel a queued or in-progress run. See the async twin."""
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/cancel"
        self._request("POST", url, token)

    def get_run(self, token: str | None, repo_full_name: str, run_id: int) -> dict[str, Any]:
        """Get a workflow run by id."""
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}"
        return self._request("GET", url, token).json()

    def get_run_attempt(
        self, token: str | None, repo_full_name: str, run_id: int, attempt: int
    ) -> dict[str, Any]:
        """Get one attempt of a workflow run.

        ``/runs/{id}`` answers for the *latest* attempt only, so a past attempt
        needs its own endpoint -- otherwise the answer is a different execution
        wearing the same run id.
        """
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/attempts/{attempt}"
        return self._request("GET", url, token).json()

    def list_runs(
        self,
        token: str | None,
        repo_full_name: str,
        workflow_id: str | None = None,
        status: str | None = None,
        branch: str | None = None,
        head_sha: str | None = None,
        event: str | None = None,
        per_page: int = DEFAULT_PER_PAGE,
        page: int = 1,
        all_pages: bool = False,
    ) -> list[dict[str, Any]]:
        """List workflow runs; ``all_pages=False`` returns the requested page.

        ``head_sha`` and ``event`` are the two filters a pull-request lookup
        needs: GitHub offers no PR-number filter, so runs are fetched by commit
        SHA and narrowed by the ``pull_requests`` array (populated for open
        pull requests).
        """
        # ``?workflow_id=`` on the repository-wide endpoint is **ignored** by
        # GitHub (verified: filtered and unfiltered calls return the same page).
        # The workflow-scoped path is the one that actually filters, and it
        # accepts the same query parameters.
        if workflow_id:
            url = f"/repos/{repo_full_name}/actions/workflows/{workflow_id}/runs"
        else:
            url = f"/repos/{repo_full_name}/actions/runs"
        params: dict[str, Any] = {
            "per_page": min(per_page, MAX_PER_PAGE),
            "page": page,
        }
        if status:
            params["status"] = status
        if branch:
            params["branch"] = branch
        if head_sha:
            params["head_sha"] = head_sha
        if event:
            params["event"] = event
        return self._paginate("GET", url, token, params=params, all_pages=all_pages)

    def get_run_jobs(
        self, token: str | None, repo_full_name: str, run_id: int
    ) -> list[dict[str, Any]]:
        """List all jobs of a run."""
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/jobs"
        return self._paginate("GET", url, token)

    def get_run_logs(self, token: str | None, repo_full_name: str, run_id: int) -> bytes:
        """Download a run's log archive.

        A zip, and a 302 to a signed blob URL: the redirect is followed here for
        the same reason as the single-job log -- and, unlike that endpoint, the
        archive is the only place per-step files exist, for about an hour after
        the run finishes.
        """
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/logs"
        return self._request("GET", url, token, follow_redirects=True).content

    def get_job(self, token: str | None, repo_full_name: str, job_id: int) -> dict[str, Any]:
        """One job, steps included.

        The single-job endpoint exists for the case the list cannot serve: a
        ``workflow_job`` completion whose payload carried no ``steps``, or a job
        row left behind by an earlier attempt. One job, not the whole run.
        """
        url = f"/repos/{repo_full_name}/actions/jobs/{job_id}"
        return self._request("GET", url, token).json()

    def get_job_logs(self, token: str | None, repo_full_name: str, job_id: int) -> str:
        """Download a job's logs (public repos work anonymously).

        The endpoint answers with a 302 to a signed blob URL rather than the
        log body, so the redirect has to be followed. Following it is scoped to
        this call instead of enabled globally: every other endpoint answers
        directly, and a silent redirect elsewhere would hide authorization
        problems behind a confusing empty body.
        """
        url = f"/repos/{repo_full_name}/actions/jobs/{job_id}/logs"
        return self._request("GET", url, token, follow_redirects=True).text

    # -- repository / workflow discovery --------------------------------------

    def get_repo_info(self, token: str | None, repo_full_name: str) -> dict[str, Any]:
        """Repository metadata including the default branch."""
        url = f"/repos/{repo_full_name}"
        return self._request("GET", url, token).json()

    def get_ref_sha(self, token: str | None, repo_full_name: str, ref: str) -> str:
        """The commit a branch or tag currently points at.

        This is how "is there already a run for this head" is answered: a run
        carries the sha it was started for, but only the ref says which sha that
        *should* be. ``GET /commits/{ref}`` resolves both, where the ``git/ref``
        endpoint needs an annotated tag peeled by hand.
        """
        url = f"/repos/{repo_full_name}/commits/{ref}"
        payload = self._request("GET", url, token).json()
        return str(payload.get("sha") or "")

    def get_pull_request(
        self, token: str | None, repo_full_name: str, number: int
    ) -> dict[str, Any]:
        """Get a pull request by number.

        Only ``head.sha`` (and ``head.ref``) are needed by the caller, but the
        whole object is returned so ``state`` and ``merge_commit_sha`` stay
        available.
        """
        url = f"/repos/{repo_full_name}/pulls/{number}"
        return self._request("GET", url, token).json()

    def get_repo_workflows(self, token: str | None, repo_full_name: str) -> list[dict[str, Any]]:
        """List the workflow definitions of a repository."""
        url = f"/repos/{repo_full_name}/actions/workflows"
        return self._paginate("GET", url, token)

    def list_accessible_repos(self, token: str) -> list[dict[str, Any]]:
        """List the repositories a token can see (``GET /user/repos``)."""
        return self._paginate(
            "GET", "/user/repos", token, params={"per_page": MAX_PER_PAGE}, all_pages=True
        )

    # -- webhook management -----------------------------------------------------

    def ping_webhook(self, token: str, repo_full_name: str, hook_id: int) -> None:
        """Send a test ping to a webhook."""
        url = f"/repos/{repo_full_name}/hooks/{hook_id}/pings"
        self._request("POST", url, token)

    def create_webhook(
        self,
        token: str,
        repo_full_name: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a webhook.

        ``events`` is a top-level field, not part of ``config``; nesting it
        there makes GitHub answer 422 "Validation Failed".
        """
        url = f"/repos/{repo_full_name}/hooks"
        hook_config = {k: v for k, v in config.items() if k != "events"}
        payload = {
            "name": "web",
            "active": True,
            "events": config.get("events", list(DEFAULT_WEBHOOK_EVENTS)),
            "config": hook_config,
        }
        return self._request("POST", url, token, json=payload).json()

    def delete_webhook(self, token: str, repo_full_name: str, hook_id: int) -> None:
        """Delete a webhook; a miss raises :class:`WebhookNotFoundError`."""
        url = f"/repos/{repo_full_name}/hooks/{hook_id}"
        try:
            self._request("DELETE", url, token)
        except GithubAPIError as exc:
            raise _dispatch_error(exc, repo_full_name, hook_id) from exc

    def list_webhooks(self, token: str, repo_full_name: str) -> list[dict[str, Any]]:
        """List the webhooks of a repository."""
        url = f"/repos/{repo_full_name}/hooks"
        return self._paginate("GET", url, token)

    # -- token verification (heuristic) ------------------------------------------

    def verify_token(self, token: str, probe_repo: str | None = None) -> dict[str, Any]:
        """Probe what a token can do.

        GitHub reports no permission list for fine-grained tokens, and the
        ``permissions`` block of ``/repos/{owner}/{repo}`` covers repository
        roles only -- it has no ``actions`` key, so reading it cannot tell
        whether the token may dispatch workflows. The only reliable answer comes
        from asking GitHub to *accept* a dispatch and reading the refusal.

        :meth:`probe_actions_write` does that without starting a run: it
        dispatches to a ref that cannot exist, which GitHub rejects (422) only
        after the permission check has passed. A token without Actions write gets
        403 instead.

        Args:
            token: The token to probe.
            probe_repo: Repository used for the Actions probe. Omitted skips it,
                leaving ``can_write_actions`` unknown (``None``) rather than
                guessing ``False``.

        Returns:
            ``is_valid``, ``username``, ``can_read_actions``,
            ``can_write_actions`` (``None`` when not probed),
            ``can_manage_webhooks`` and ``accessible_repos``.
        """
        result: dict[str, Any] = {
            "is_valid": False,
            "username": "",
            "can_read_actions": False,
            "can_write_actions": None,
            "can_manage_webhooks": False,
            "accessible_repos": 0,
        }
        try:
            response = self._request("GET", "/user", token)
            user_data = response.json()
            result["is_valid"] = True
            result["username"] = user_data.get("login", "")
            # GitHub reports a fine-grained PAT's expiry on every authenticated
            # response; a classic PAT never sends the header, so absence means
            # "no expiry", not "expired".
            result["expires_at"] = response.headers.get(
                "github-authentication-token-expiration", ""
            )
        except (TokenInvalidError, GithubAPIError):
            return result

        try:
            response = self._request("GET", "/user/repos", token, params={"per_page": 1})
            if isinstance(response.json(), list):
                result["can_read_actions"] = True
                # ``per_page=1`` keeps the call cheap; the count comes from the
                # pagination trailer, not from the one row returned. Without this
                # the field reported 0 or 1 and never the real total.
                result["accessible_repos"] = _total_from_link_header(response)
        except GithubAPIError:
            pass

        # The Administration permission, measured rather than assumed: listing
        # hooks is the cheapest endpoint that needs it. GitHub answers 404 for
        # a repository the token cannot see, exactly as it does for one that
        # does not exist, so a 404 is a "no" -- a false "yes" would only fail
        # later, when the hook is actually created. A transport failure leaves
        # the answer unknown: "we could not tell" must not be recorded as
        # "denied", because that closes the automatic path without a trace.
        result["can_manage_webhooks"] = self.probe_manage_webhooks(token, probe_repo)

        if probe_repo:
            result["can_write_actions"] = self.probe_actions_write(token, probe_repo)
        return result

    def probe_manage_webhooks(self, token: str, repo_full_name: str | None) -> bool | None:
        """Whether ``token`` may read this repository's webhooks (see the async twin).

        Listing hooks is the cheapest endpoint that requires the Administration
        permission, and it is a **read**. A fine-grained PAT splits that
        permission in two, so a token that lists hooks can still be refused when
        asked to create one -- which is exactly what happened live: this probe
        answered ``True`` for a token whose ``webhook create`` answered 401.
        Treat ``True`` as "creating might work", never as a guarantee: the write
        path must handle the refusal and fall back to a pasted secret.

        The answers:

        * ``True``  -- GitHub listed them;
        * ``False`` -- 403 (explicitly refused) or 404 (a repository the token
          cannot see, which GitHub reports identically to one that does not
          exist, so it is a "no" either way);
        * ``None``  -- no repository to ask about, or the request never got an
          answer. Unknown is not a denial: recording it as one would silently
          disable the automatic path, and nothing would ever say why.

        ``repo_full_name`` of ``None`` is what a first boot looks like, before
        any repository is subscribed; the answer stays unknown until one is.
        """
        if not repo_full_name:
            return None
        try:
            self._request("GET", f"/repos/{repo_full_name}/hooks", token)
        except GhatError as exc:
            status = getattr(exc, "status_code", None)
            if status in (403, 404):
                return False
            return None
        except Exception:  # noqa: BLE001 - a transport failure is not a denial
            return None
        return True

    def probe_actions_write(self, token: str, repo_full_name: str) -> bool:
        """Report whether ``token`` may dispatch workflows in a repository.

        GitHub offers no endpoint that lists a fine-grained token's permissions,
        and the ``permissions`` block of ``/repos/{owner}/{repo}`` has no
        ``actions`` key -- it describes repository roles, not token scopes. So
        the question is answered the only way it can be: by asking GitHub to
        accept a dispatch and reading what it says.

        The dispatch targets :data:`PROBE_REF`, a ref that cannot exist. GitHub
        checks the permission first and the ref second, so:

        * ``403`` -- the token lacks Actions write. Nothing was dispatched.
        * ``422`` (ref not found) -- the permission check passed; the token can
          dispatch. Nothing was dispatched either, because the ref does not
          exist.
        * ``404`` -- the repository is not visible to the token, or has no such
          workflow. Treated as "cannot", since nothing could be dispatched.

        Args:
            token: The credential to probe.
            repo_full_name: ``owner/repo`` that the credential covers.

        Returns:
            ``True`` only when the permission check demonstrably passed.
        """
        workflow = self._probe_workflow(token, repo_full_name)
        if workflow is None:
            return False
        url = f"/repos/{repo_full_name}/actions/workflows/{workflow}/dispatches"
        try:
            self._request("POST", url, token, json={"ref": PROBE_REF})
        except GhatError as exc:
            # 422 means the permission check passed and the ref was rejected --
            # the exact signal this probe is after. A 403 means no Actions write;
            # anything else (404, network) is reported as "cannot", since nothing
            # was dispatched and nothing proved otherwise.
            return getattr(exc, "status_code", 0) == 422
        # A 200/204 would mean the dispatch somehow succeeded against a
        # nonexistent ref. That should be impossible; treat it as "yes" because
        # the permission check demonstrably passed.
        return True

    def _probe_workflow(self, token: str, repo_full_name: str) -> str | None:
        """Pick a workflow file name to probe with, or ``None``.

        Uses the repository's first active workflow. The probe only needs *a*
        workflow that accepts ``workflow_dispatch``; if none does, the 422 comes
        back as a different message and the probe reports ``False`` rather than
        guessing.
        """
        try:
            workflows = self.get_repo_workflows(token, repo_full_name)
        except GhatError:
            return None
        for workflow in workflows:
            path = str(workflow.get("path") or "")
            if path.startswith(".github/workflows/") and workflow.get("state") == "active":
                return path.rsplit("/", 1)[-1]
        return None


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------


class AsyncGithubClient:
    """Asynchronous GitHub REST client.

    The running service uses this twin; behavior and method names are
    identical to :class:`GithubClient`.
    """

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._client: httpx2.AsyncClient | None = None
        self._conditional = _ConditionalCache()

    async def __aenter__(self) -> AsyncGithubClient:
        await self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def _ensure_client(self) -> None:
        if self._client is None or self._client.is_closed:
            self._client = httpx2.AsyncClient(
                base_url=BASE_URL,
                timeout=self._timeout,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _request(
        self,
        method: str,
        url: str,
        token: str | None,
        **kwargs: Any,
    ) -> httpx2.Response:
        """Execute an HTTP request with retry logic."""
        async with _observed_async(method, url):
            return await self._request_inner(method, url, token, **kwargs)

    async def _request_inner(
        self,
        method: str,
        url: str,
        token: str | None,
        **kwargs: Any,
    ) -> httpx2.Response:
        await self._ensure_client()
        headers = auth_headers(token)
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))
        cache_key = (
            self._conditional.key(method, url, kwargs.get("params"), token)
            if method.upper() == "GET"
            else None
        )
        if cache_key is not None:
            etag = self._conditional.etag(cache_key)
            if etag:
                headers["If-None-Match"] = etag
        for attempt in range(self._max_retries):
            try:
                response = await self._client.request(method, url, headers=headers, **kwargs)
                if cache_key is not None:
                    response = await self._resolve_conditional(cache_key, headers, response, kwargs)
                check_rate_limit(response)
                if response.status_code >= 400:
                    _note_status(response)
                    handle_error(response)
                _note_status(response)
                return response
            except httpx2.TransportError as exc:
                # See :meth:`GithubClient._request`: the wider catch is what keeps
                # ``RemoteProtocolError`` from surfacing as an untyped error.
                retryable = method.upper() in RETRYABLE_METHODS
                if retryable and attempt < self._max_retries - 1:
                    await asyncio.sleep(self._retry_delay * (2**attempt))
                    continue
                raise UpstreamUnavailableError(
                    f"GitHub did not answer ({exc.__class__.__name__}: {exc})"
                    + (
                        f" after {self._max_retries} attempts"
                        if retryable
                        else "; the request was not retried because it is not idempotent"
                    )
                ) from exc
        raise GithubAPIError("Unexpected error in request")

    async def _resolve_conditional(
        self,
        cache_key: tuple[str, str, str, str],
        headers: dict[str, str],
        response: httpx2.Response,
        kwargs: dict[str, Any],
    ) -> httpx2.Response:
        """See :meth:`GithubClient._resolve_conditional`; the async twin."""
        if response.status_code == 304:
            replayed = self._conditional.replay(cache_key)
            if replayed is not None:
                return replayed
            self._conditional.forget(cache_key)
            headers.pop("If-None-Match", None)
            return await self._client.request("GET", cache_key[1], headers=headers, **kwargs)
        if response.status_code == 200:
            self._conditional.remember(cache_key, response)
        return response

    async def _paginate(
        self,
        method: str,
        url: str,
        token: str | None,
        params: dict[str, Any] | None = None,
        all_pages: bool = True,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch pages of a paginated endpoint (see the sync twin)."""
        results: list[dict[str, Any]] = []
        current_url: str | None = url
        current_params = params.copy() if params else {}
        while current_url:
            response = await self._request(
                method,
                current_url,
                token,
                params=current_params if current_url == url else None,
                **kwargs,
            )
            data = response.json()
            if isinstance(data, list):
                results.extend(data)
            elif isinstance(data, dict):
                results.extend(unwrap_envelope(data))
            if not all_pages:
                break
            links = parse_link_header(response.headers.get("Link", ""))
            current_url = links.get("next")
            current_params = {}
        return results

    # -- workflow dispatch ---------------------------------------------------

    async def trigger_workflow(
        self,
        token: str,
        repo_full_name: str,
        workflow_id: str,
        ref: str,
        inputs: dict[str, str] | None = None,
        return_run_details: bool = True,
    ) -> dict[str, Any]:
        """Trigger a workflow run and, when GitHub allows, return its id.

        See :meth:`GithubClient.trigger_workflow`: asking for run details turns a
        racing read-back into a direct answer, but a 204 is still handled because
        not every server honours the parameter.
        """
        url = f"/repos/{repo_full_name}/actions/workflows/{workflow_id}/dispatches"
        payload: dict[str, Any] = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs
        if return_run_details:
            payload["return_run_details"] = True
        response = await self._request("POST", url, token, json=payload)
        if response.status_code in (202, 204) or not response.content:
            return {"run_id": 0, "status": "queued"}
        body = response.json()
        run_id = body.get("workflow_run_id") or body.get("id") or 0
        return {
            "id": run_id,
            "run_id": run_id,
            "html_url": body.get("html_url", ""),
            "run_url": body.get("run_url", ""),
        }

    # -- run queries ----------------------------------------------------------

    async def rerun_workflow(self, token: str, repo_full_name: str, run_id: int) -> None:
        """Re-run a finished workflow run.

        ``POST /actions/runs/{id}/rerun`` answers 201 with no body; the new run
        is discovered by listing the workflow's runs afterwards, because the
        endpoint does not return its id.
        """
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/rerun"
        await self._request("POST", url, token)

    async def rerun_failed_jobs(self, token: str, repo_full_name: str, run_id: int) -> None:
        """Re-run only the failed jobs of a finished run.

        ``POST /actions/runs/{id}/rerun-failed-jobs`` -- a different endpoint
        from the whole-run one, with the same answer shape (201, no body) and the
        same consequence: the run id is unchanged and the attempt number rises.

        GitHub's semantics are "the failed jobs *and the jobs they depend on*":
        the dependency set is GitHub's to compute, so this service reports the
        failed jobs it knows about and does not pretend the list is exhaustive.
        """
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/rerun-failed-jobs"
        await self._request("POST", url, token)

    async def cancel_workflow_run(self, token: str, repo_full_name: str, run_id: int) -> None:
        """Cancel a queued or in-progress workflow run.

        Cancelling is asynchronous on GitHub's side: the call answers 202 and the
        run reaches ``completed / cancelled`` a moment later, so the caller reads
        the run again rather than trusting the response.
        """
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/cancel"
        await self._request("POST", url, token)

    async def get_run(self, token: str | None, repo_full_name: str, run_id: int) -> dict[str, Any]:
        """Get a workflow run by id."""
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}"
        return (await self._request("GET", url, token)).json()

    async def get_run_attempt(
        self, token: str | None, repo_full_name: str, run_id: int, attempt: int
    ) -> dict[str, Any]:
        """Get one attempt of a workflow run; see the sync twin."""
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/attempts/{attempt}"
        return (await self._request("GET", url, token)).json()

    async def list_runs(
        self,
        token: str | None,
        repo_full_name: str,
        workflow_id: str | None = None,
        status: str | None = None,
        branch: str | None = None,
        head_sha: str | None = None,
        event: str | None = None,
        per_page: int = DEFAULT_PER_PAGE,
        page: int = 1,
        all_pages: bool = False,
    ) -> list[dict[str, Any]]:
        """List workflow runs (see the sync twin for the filters)."""
        # ``?workflow_id=`` on the repository-wide endpoint is **ignored** by
        # GitHub (verified: filtered and unfiltered calls return the same page).
        # The workflow-scoped path is the one that actually filters, and it
        # accepts the same query parameters.
        if workflow_id:
            url = f"/repos/{repo_full_name}/actions/workflows/{workflow_id}/runs"
        else:
            url = f"/repos/{repo_full_name}/actions/runs"
        params: dict[str, Any] = {
            "per_page": min(per_page, MAX_PER_PAGE),
            "page": page,
        }
        if status:
            params["status"] = status
        if branch:
            params["branch"] = branch
        if head_sha:
            params["head_sha"] = head_sha
        if event:
            params["event"] = event
        return await self._paginate("GET", url, token, params=params, all_pages=all_pages)

    async def get_run_jobs(
        self, token: str | None, repo_full_name: str, run_id: int
    ) -> list[dict[str, Any]]:
        """List all jobs of a run."""
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/jobs"
        return await self._paginate("GET", url, token)

    async def get_run_logs(self, token: str | None, repo_full_name: str, run_id: int) -> bytes:
        """Download a run's log archive (see the sync twin)."""
        url = f"/repos/{repo_full_name}/actions/runs/{run_id}/logs"
        return (await self._request("GET", url, token, follow_redirects=True)).content

    async def get_job(self, token: str | None, repo_full_name: str, job_id: int) -> dict[str, Any]:
        """One job, steps included; see the sync twin."""
        url = f"/repos/{repo_full_name}/actions/jobs/{job_id}"
        return (await self._request("GET", url, token)).json()

    async def get_job_logs(self, token: str | None, repo_full_name: str, job_id: int) -> str:
        """Download a job's logs (public repos work anonymously).

        See :meth:`GithubClient.get_job_logs`: the endpoint redirects to a
        signed blob URL, so the redirect is followed for this call only.
        """
        url = f"/repos/{repo_full_name}/actions/jobs/{job_id}/logs"
        return (await self._request("GET", url, token, follow_redirects=True)).text

    # -- repository / workflow discovery --------------------------------------

    async def get_repo_info(self, token: str | None, repo_full_name: str) -> dict[str, Any]:
        """Repository metadata including the default branch."""
        url = f"/repos/{repo_full_name}"
        return (await self._request("GET", url, token)).json()

    async def get_ref_sha(self, token: str | None, repo_full_name: str, ref: str) -> str:
        """The commit a branch or tag currently points at; see the sync twin."""
        url = f"/repos/{repo_full_name}/commits/{ref}"
        payload = (await self._request("GET", url, token)).json()
        return str(payload.get("sha") or "")

    async def get_pull_request(
        self, token: str | None, repo_full_name: str, number: int
    ) -> dict[str, Any]:
        """Get a pull request by number (see the sync twin)."""
        url = f"/repos/{repo_full_name}/pulls/{number}"
        return (await self._request("GET", url, token)).json()

    async def get_repo_workflows(
        self, token: str | None, repo_full_name: str
    ) -> list[dict[str, Any]]:
        """List the workflow definitions of a repository."""
        url = f"/repos/{repo_full_name}/actions/workflows"
        return await self._paginate("GET", url, token)

    async def list_accessible_repos(self, token: str) -> list[dict[str, Any]]:
        """List the repositories a token can see (``GET /user/repos``)."""
        return await self._paginate(
            "GET", "/user/repos", token, params={"per_page": MAX_PER_PAGE}, all_pages=True
        )

    # -- webhook management -----------------------------------------------------

    async def ping_webhook(self, token: str, repo_full_name: str, hook_id: int) -> None:
        """Send a test ping to a webhook."""
        url = f"/repos/{repo_full_name}/hooks/{hook_id}/pings"
        await self._request("POST", url, token)

    async def create_webhook(
        self,
        token: str,
        repo_full_name: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a webhook (see the sync twin for the events caveat)."""
        url = f"/repos/{repo_full_name}/hooks"
        hook_config = {k: v for k, v in config.items() if k != "events"}
        payload = {
            "name": "web",
            "active": True,
            "events": config.get("events", list(DEFAULT_WEBHOOK_EVENTS)),
            "config": hook_config,
        }
        return (await self._request("POST", url, token, json=payload)).json()

    async def delete_webhook(self, token: str, repo_full_name: str, hook_id: int) -> None:
        """Delete a webhook; a miss raises :class:`WebhookNotFoundError`."""
        url = f"/repos/{repo_full_name}/hooks/{hook_id}"
        try:
            await self._request("DELETE", url, token)
        except GithubAPIError as exc:
            raise _dispatch_error(exc, repo_full_name, hook_id) from exc

    async def list_webhooks(self, token: str, repo_full_name: str) -> list[dict[str, Any]]:
        """List the webhooks of a repository."""
        url = f"/repos/{repo_full_name}/hooks"
        return await self._paginate("GET", url, token)

    # -- token verification (heuristic) ------------------------------------------

    async def verify_token(self, token: str, probe_repo: str | None = None) -> dict[str, Any]:
        """Probe what a token can do (see the sync twin).

        The Actions write probe is the part that matters: it is what lets
        ``ci_status`` report ``can_trigger`` from measurement instead of
        assuming a credential implies permission.
        """
        result: dict[str, Any] = {
            "is_valid": False,
            "username": "",
            "can_read_actions": False,
            "can_write_actions": None,
            "can_manage_webhooks": False,
            "accessible_repos": 0,
        }
        try:
            response = await self._request("GET", "/user", token)
            user_data = response.json()
            result["is_valid"] = True
            result["username"] = user_data.get("login", "")
            # See the sync twin: the expiry arrives as a response header, and a
            # classic PAT never sends it.
            result["expires_at"] = response.headers.get(
                "github-authentication-token-expiration", ""
            )
        except (TokenInvalidError, GithubAPIError):
            return result

        try:
            response = await self._request("GET", "/user/repos", token, params={"per_page": 1})
            if isinstance(response.json(), list):
                result["can_read_actions"] = True
                result["accessible_repos"] = _total_from_link_header(response)
        except GithubAPIError:
            pass

        # See the sync twin: measured, and unknown is not a denial.
        result["can_manage_webhooks"] = await self.probe_manage_webhooks(token, probe_repo)

        if probe_repo:
            result["can_write_actions"] = await self.probe_actions_write(token, probe_repo)
        return result

    async def probe_manage_webhooks(self, token: str, repo_full_name: str | None) -> bool | None:
        """Whether ``token`` may read this repository's webhooks (see the sync twin).

        ``None`` is "we could not tell" -- no repository to ask about, or the
        request never got an answer -- and must not be recorded as a denial.
        ``True`` proves the read half of the permission only; creating a hook
        needs the write half, which no harmless probe can measure.
        """
        if not repo_full_name:
            return None
        try:
            await self._request("GET", f"/repos/{repo_full_name}/hooks", token)
        except GhatError as exc:
            status = getattr(exc, "status_code", None)
            if status in (403, 404):
                return False
            return None
        except Exception:  # noqa: BLE001 - a transport failure is not a denial
            return None
        return True

    async def probe_actions_write(self, token: str, repo_full_name: str) -> bool:
        """Report whether ``token`` may dispatch workflows (see the sync twin).

        Same reasoning and same safe signal: a dispatch to a nonexistent ref is
        rejected with 422 only after the permission check has passed, so nothing
        is ever started.
        """
        workflow = await self._probe_workflow(token, repo_full_name)
        if workflow is None:
            return False
        url = f"/repos/{repo_full_name}/actions/workflows/{workflow}/dispatches"
        try:
            await self._request("POST", url, token, json={"ref": PROBE_REF})
        except GhatError as exc:
            return getattr(exc, "status_code", 0) == 422
        return True

    async def _probe_workflow(self, token: str, repo_full_name: str) -> str | None:
        """Pick a workflow file name to probe with (see the sync twin)."""
        try:
            workflows = await self.get_repo_workflows(token, repo_full_name)
        except GhatError:
            return None
        for workflow in workflows:
            path = str(workflow.get("path") or "")
            if path.startswith(".github/workflows/") and workflow.get("state") == "active":
                return path.rsplit("/", 1)[-1]
        return None
