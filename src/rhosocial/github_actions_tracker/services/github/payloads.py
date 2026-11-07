# src/rhosocial/github_actions_tracker/services/github/payloads.py
"""GitHub payloads, translated into semantic values.

GitHub does not promise that its field names stay the same. The translation
therefore lives in exactly one class, in the service layer, and everything it
returns is keyed by the **model column** it describes -- ``status``,
``commit_sha``, ``github_run_id`` -- never by a GitHub spelling such as
``head_sha`` or ``run_attempt``.

Sources
-------

The payloads this class accepts come from exactly these places:

* ``run`` -- a run object: ``GET /repos/{owner}/{repo}/actions/runs/{run_id}``,
  or the ``workflow_run`` object of a ``workflow_run`` delivery.
* ``attempt_number`` / ``attempt`` -- the same run object, or
  ``GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{n}``.
* ``job`` / ``steps`` -- ``GET .../runs/{run_id}/jobs`` entries,
  ``GET .../jobs/{job_id}``, or the ``workflow_job`` object of a
  ``workflow_job`` delivery.
* ``workflow`` -- ``GET .../actions/workflows`` entries, any run object, or the
  sibling ``workflow`` object beside a ``workflow_run``.
* ``repository`` -- the repository object from ``GET /repos/{owner}/{repo}``.
* ``belongs_to_pull_request`` -- ``GET .../actions/runs`` entries, or the
  ``workflow_run`` object.
* ``step`` -- one entry of a job object's ``steps`` array.

An event delivery wraps its subject under the event name; a REST object does
not. Both shapes are accepted, and :meth:`_event_source` is the one place that
knows the difference. The ``TypedDict`` declarations below name the keys this
class actually reads; anything else in the object is ignored.

This class holds no state, so its methods are classmethods: there is nothing to
construct and nothing to configure.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TypedDict


class WorkflowPayload(TypedDict, total=False):
    """A workflow object, or the same labels carried by a run.

    ``state`` is present only on a workflow object; a run payload does not
    carry it.
    """

    workflow_id: int
    id: int
    name: str
    path: str
    state: str


class StepPayload(TypedDict, total=False):
    """One entry of a job object's ``steps`` array."""

    number: int
    name: str
    status: str
    conclusion: str | None
    started_at: str | None
    completed_at: str | None


class JobPayload(TypedDict, total=False):
    """A job object: one ``/jobs`` entry, or the ``workflow_job`` object."""

    id: int
    run_id: int
    run_attempt: int
    name: str
    status: str
    conclusion: str | None
    runner_name: str | None
    started_at: str | None
    completed_at: str | None
    steps: list[StepPayload]


class RunPayload(TypedDict, total=False):
    """A run object: one ``/runs`` entry, or the ``workflow_run`` object.

    The same object also describes the attempt it names through
    ``run_attempt``, ``status``, ``conclusion`` and the timestamps below.
    """

    id: int
    run_number: int
    run_attempt: int
    event: str
    status: str
    conclusion: str | None
    head_branch: str
    head_sha: str
    display_title: str
    head_commit: Mapping[str, Any]
    head_repository: Mapping[str, Any]
    pull_requests: list[Mapping[str, Any]]
    actor: Mapping[str, Any] | str
    triggering_actor: Mapping[str, Any] | str
    run_started_at: str | None
    started_at: str | None
    created_at: str | None
    updated_at: str | None
    completed_at: str | None
    workflow_id: int
    name: str
    path: str


class RepositoryPayload(TypedDict, total=False):
    """A repository object from ``GET /repos/{owner}/{repo}``."""

    id: int
    full_name: str
    owner: Mapping[str, Any] | str
    name: str
    is_private: bool
    default_branch: str


class WorkflowRunEvent(TypedDict, total=False):
    """A ``workflow_run`` webhook delivery."""

    workflow_run: RunPayload
    workflow: WorkflowPayload


class WorkflowJobEvent(TypedDict, total=False):
    """A ``workflow_job`` webhook delivery."""

    workflow_job: JobPayload


class GithubPayloads:
    """Translate GitHub API objects into model-shaped value dictionaries.

    Every public method returns plain values: ``int``, ``str``, ``datetime``,
    ``bool`` or a list/dict of those. No key of a GitHub payload appears in a
    returned dictionary.
    """

    #: Status synonyms GitHub reports for the canonical "not started yet" state.
    _STATUS_ALIASES: dict[str, str] = {
        "queued": "queued",
        "in_progress": "in_progress",
        "completed": "completed",
        "requested": "queued",
        "pending": "queued",
        "waiting": "queued",
    }

    # ---- scalar values -------------------------------------------------------

    @staticmethod
    def _as_int(value: Any) -> int | None:
        """Coerce a payload scalar to ``int``, returning ``None`` when impossible."""
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _text(value: Any, default: str = "") -> str:
        """Return a trimmed string, falling back to ``default`` for blank values."""
        if value is None:
            return default
        text = str(value).strip()
        return text or default

    @staticmethod
    def _mapping(value: Any) -> Mapping[str, Any]:
        """``value`` when it is a mapping, otherwise an empty one."""
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _event_source(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        """The fragment of an event envelope, or the payload itself.

        A delivery wraps its subject under the event name (``workflow_run``,
        ``workflow_job``); a REST object does not. Accepting both shapes here
        keeps the event envelope out of the controllers.
        """
        inner = payload.get(key)
        return inner if isinstance(inner, Mapping) else payload

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        """Parse a GitHub timestamp into an aware ``datetime``.

        Accepts ISO 8601 strings (``2026-10-10T12:34:56Z``), epoch numbers and
        ``datetime`` values; a naive one is taken as UTC, matching GitHub.
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, tz=timezone.utc)

        text = str(value).strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    @classmethod
    def _status(cls, value: Any) -> str:
        """Map a GitHub status onto the canonical status set.

        Unknown values pass through so the state machine can reject them
        explicitly; a blank value means "not started" and maps to ``queued``.
        """
        if not value:
            return "queued"
        normalized = str(value).strip().lower()
        if not normalized:
            return "queued"
        return cls._STATUS_ALIASES.get(normalized, normalized)

    @staticmethod
    def _conclusion(value: Any) -> str | None:
        """Normalize a GitHub conclusion, treating blanks as "not finished yet"."""
        if value is None:
            return None
        cleaned = str(value).strip().lower()
        return cleaned or None

    @classmethod
    def _actor_login(cls, payload: Mapping[str, Any]) -> str | None:
        """The actor login out of a payload, tolerant of the field being absent."""
        actor = payload.get("actor")
        if isinstance(actor, Mapping):
            return cls._text(actor.get("login")) or None
        return cls._text(actor) or None

    # ---- entities ------------------------------------------------------------

    @classmethod
    def workflow(cls, payload: WorkflowPayload | RunPayload | WorkflowRunEvent) -> dict[str, Any]:
        """The workflow values a workflow object or a run payload carries.

        A run payload names ``workflow_id``; a workflow list entry names ``id``.
        A ``workflow_run`` event also carries the **current** workflow object as
        a sibling, and that is the only place ``state`` appears -- so its fields
        win over the run fragment's copies. A run payload without the sibling
        has no ``state`` at all, and the returned value is blank rather than
        guessed.
        """
        nested = payload.get("workflow")
        source: Mapping[str, Any] = (
            {**payload, **nested} if isinstance(nested, Mapping) else payload
        )
        return {
            "workflow_id": cls._as_int(source.get("workflow_id"))
            or cls._as_int(source.get("id"))
            or 0,
            "name": cls._text(source.get("name")),
            "path": cls._text(source.get("path")),
            "state": cls._text(source.get("state")),
        }

    @classmethod
    def run(cls, payload: RunPayload | WorkflowRunEvent) -> dict[str, Any]:
        """The run-shaped values a run object carries, GitHub keys translated."""
        payload = cls._event_source(payload, "workflow_run")
        head_commit = cls._mapping(payload.get("head_commit"))
        pull_requests = payload.get("pull_requests")
        has_pull = isinstance(pull_requests, list) and bool(pull_requests)
        if has_pull and isinstance(pull_requests[0], Mapping):
            first_pull = pull_requests[0]
        else:
            first_pull = {}
        head_repository = cls._mapping(payload.get("head_repository"))
        author = cls._mapping(head_commit.get("author"))
        base = cls._mapping(first_pull.get("base"))

        actor = ""
        for key in ("actor", "triggering_actor"):
            candidate = payload.get(key)
            if isinstance(candidate, Mapping):
                login = cls._text(candidate.get("login"))
                if login:
                    actor = login
                    break
            elif candidate:
                actor = cls._text(candidate)
                break

        return {
            "github_run_id": cls._as_int(payload.get("id")) or 0,
            "run_number": cls._as_int(payload.get("run_number")) or 0,
            "pr_number": cls._as_int(first_pull.get("number")),
            "event": cls._text(payload.get("event")),
            "branch": cls._text(payload.get("head_branch")),
            "commit_sha": cls._text(payload.get("head_sha")),
            "commit_message": cls._text(head_commit.get("message")),
            "display_title": cls._text(payload.get("display_title")),
            "head_repo": cls._text(head_repository.get("full_name")),
            "commit_time": cls._timestamp(head_commit.get("timestamp")),
            "commit_author": cls._text(author.get("name")),
            "base_ref": cls._text(base.get("ref")),
            "actor": actor,
        }

    @classmethod
    def attempt_number(
        cls, payload: RunPayload | JobPayload | WorkflowRunEvent | WorkflowJobEvent
    ) -> int | None:
        """Which execution a run or job payload describes, when it names one."""
        payload = cls._event_source(payload, "workflow_run")
        if "run_attempt" not in payload:
            payload = cls._event_source(payload, "workflow_job")
        return cls._as_int(payload.get("run_attempt"))

    @classmethod
    def attempt(cls, payload: RunPayload | WorkflowRunEvent) -> dict[str, Any]:
        """The attempt-shaped values a run or attempt object carries.

        ``GET /runs/{id}`` describes the latest attempt; ``/attempts/{n}``
        describes one. Both are read the same way. A completed execution that
        carries no ``completed_at`` uses ``updated_at``, its last state change.
        """
        payload = cls._event_source(payload, "workflow_run")
        status = cls._status(payload.get("status"))
        completed_at = payload.get("completed_at")
        if not completed_at and status == "completed":
            completed_at = payload.get("updated_at")
        return {
            "status": status,
            "conclusion": cls._conclusion(payload.get("conclusion")),
            "actor": cls._actor_login(payload),
            "started_at": cls._timestamp(
                payload.get("run_started_at") or payload.get("started_at")
            ),
            "completed_at": cls._timestamp(completed_at),
            "github_created_at": cls._timestamp(payload.get("created_at")),
            "github_updated_at": cls._timestamp(payload.get("updated_at")),
        }

    @classmethod
    def job(cls, payload: JobPayload | WorkflowJobEvent) -> dict[str, Any]:
        """The job-shaped values a job object carries."""
        payload = cls._event_source(payload, "workflow_job")
        return {
            "github_job_id": cls._as_int(payload.get("id")) or 0,
            "name": cls._text(payload.get("name")),
            "status": cls._status(payload.get("status")),
            "conclusion": cls._conclusion(payload.get("conclusion")),
            "runner_name": cls._text(payload.get("runner_name")) or None,
            "started_at": cls._timestamp(payload.get("started_at")),
            "completed_at": cls._timestamp(payload.get("completed_at")),
        }

    @classmethod
    def step(cls, payload: StepPayload) -> dict[str, Any]:
        """The step-shaped values one entry of a job's ``steps`` array carries.

        GitHub gives a step no id of its own, so ``number`` is also its identity;
        the model mirrors it into ``github_step_id``.
        """
        return {
            "number": cls._as_int(payload.get("number")) or 0,
            "name": cls._text(payload.get("name")),
            "status": cls._status(payload.get("status")),
            "conclusion": cls._conclusion(payload.get("conclusion")),
            "started_at": cls._timestamp(payload.get("started_at")),
            "completed_at": cls._timestamp(payload.get("completed_at")),
        }

    @classmethod
    def steps(cls, payload: JobPayload | WorkflowJobEvent) -> list[dict[str, Any]]:
        """Every step of a job object, translated; empty when it carries none."""
        payload = cls._event_source(payload, "workflow_job")
        entries = payload.get("steps")
        if not isinstance(entries, list):
            return []
        return [cls.step(entry) for entry in entries if isinstance(entry, Mapping)]

    @classmethod
    def repository(cls, payload: RepositoryPayload, full_name: str) -> dict[str, Any]:
        """The repository values a repository object carries.

        ``full_name`` the caller asked for wins over the payload's own spelling:
        it is the name the caller confirmed, and GitHub answers slightly
        differently for a repository renamed mid-flight.
        """
        name = cls._text(full_name or payload.get("full_name"))
        owner = cls._mapping(payload.get("owner"))
        owner_login = cls._text(owner.get("login")) or cls._text(payload.get("owner"))
        if not owner_login and "/" in name:
            owner_login = name.split("/", 1)[0]
        short_name = cls._text(payload.get("name"))
        if not short_name and "/" in name:
            short_name = name.split("/", 1)[1]
        return {
            "github_repo_id": cls._as_int(payload.get("id")) or 0,
            "full_name": name,
            "owner": owner_login,
            "name": short_name or name,
            "is_private": bool(payload.get("is_private", False)),
            "default_branch": cls._text(payload.get("default_branch"), "main"),
        }

    # ---- questions -----------------------------------------------------------

    @classmethod
    def belongs_to_pull_request(cls, payload: RunPayload, number: int, head_sha: str) -> bool:
        """Whether a run object belongs to pull request ``number``.

        GitHub offers no PR-number filter on the runs endpoint, so attribution
        is decided in two tiers:

        * a non-empty ``pull_requests`` array is authoritative and exclusive --
          GitHub populates it while the pull request is open, so a number miss
          means the run belongs to another pull request;
        * an empty or absent array (closed/merged pull requests, where GitHub
          drops the attribution) falls back to ``event == "pull_request"`` plus a
          matching ``head_sha``. Best effort: two pull requests sharing a head
          commit become indistinguishable, which is why open pull requests are
          the case this supports.
        """
        pull_requests = payload.get("pull_requests")
        if pull_requests:
            return any(
                isinstance(entry, Mapping) and cls._as_int(entry.get("number")) == number
                for entry in pull_requests
            )
        event = cls._text(payload.get("event"))
        same_head = cls._text(payload.get("head_sha")) == head_sha
        return event == "pull_request" and bool(head_sha) and same_head


__all__ = [
    "GithubPayloads",
    "JobPayload",
    "RepositoryPayload",
    "RunPayload",
    "StepPayload",
    "WorkflowJobEvent",
    "WorkflowPayload",
    "WorkflowRunEvent",
]
