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

The Pydantic models below are the schema. They ignore keys they do not name,
accept strings, integers and ``datetime`` values for timestamps, and raise
``ValidationError`` when a value has the wrong shape -- a string where an
object belongs, or an unparsable timestamp. Status and conclusion stay strings:
GitHub may add a value, and refusing the delivery over an unknown word is not
this layer's decision.

An event delivery wraps its subject under the event name; a REST object does
not. Both shapes are accepted, and :meth:`_run_payload` / :meth:`_job_payload`
are the one place that knows the difference.

This class holds no state, so its methods are classmethods: there is nothing to
construct and nothing to configure.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


class _GitHubPayload(BaseModel):
    """Base for every payload model: unknown keys are ignored, not refused.

    GitHub sends far more than this service reads (permissions, links,
    installation data), and a payload carrying one more key is not an error.
    """

    model_config = ConfigDict(extra="ignore")

    @staticmethod
    def _timestamp(value: Any) -> Any:
        """Read a GitHub timestamp as an aware ``datetime``.

        Accepts the ISO strings GitHub emits, an already parsed ``datetime``,
        and an epoch number. A naive value is read as UTC -- the same
        assumption the rest of the service makes -- so every stored timestamp
        is comparable with every computed one.
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
        except ValueError as exc:
            raise ValueError("not a timestamp") from exc
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


#: A timestamp field: ISO string, epoch number or ``datetime`` in, aware out.
Timestamp = Annotated[datetime | None, BeforeValidator(_GitHubPayload._timestamp)]


class ActorPayload(_GitHubPayload):
    """The ``login`` object GitHub puts beside an actor."""

    login: str | None = None


class CommitAuthorPayload(_GitHubPayload):
    """The author object inside ``head_commit``."""

    name: str | None = None


class CommitPayload(_GitHubPayload):
    """The ``head_commit`` object of a run."""

    message: str | None = None
    timestamp: Timestamp = None
    author: CommitAuthorPayload | None = None


class RepositoryRefPayload(_GitHubPayload):
    """The ``head_repository`` object of a run."""

    full_name: str | None = None


class PullRequestBasePayload(_GitHubPayload):
    """The ``base`` object of one pull request entry."""

    ref: str | None = None


class PullRequestPayload(_GitHubPayload):
    """One entry of a run's ``pull_requests`` array."""

    number: int | None = None
    base: PullRequestBasePayload | None = None


class WorkflowPayload(_GitHubPayload):
    """A workflow object, or the same labels carried by a run.

    ``state`` is present only on a workflow object; a run payload does not
    carry it.
    """

    workflow_id: int | None = None
    id: int | None = None
    name: str | None = None
    path: str | None = None
    state: str | None = None


class StepPayload(_GitHubPayload):
    """One entry of a job object's ``steps`` array."""

    number: int | None = None
    name: str | None = None
    status: str | None = None
    conclusion: str | None = None
    started_at: Timestamp = None
    completed_at: Timestamp = None


class JobPayload(_GitHubPayload):
    """A job object: one ``/jobs`` entry, or the ``workflow_job`` object."""

    id: int | None = None
    run_id: int | None = None
    run_attempt: int | None = None
    name: str | None = None
    status: str | None = None
    conclusion: str | None = None
    runner_name: str | None = None
    started_at: Timestamp = None
    completed_at: Timestamp = None
    steps: list[StepPayload] = Field(default_factory=list)


class RunPayload(_GitHubPayload):
    """A run object: one ``/runs`` entry, or the ``workflow_run`` object.

    The same object also describes the attempt it names through
    ``run_attempt``, ``status``, ``conclusion`` and the timestamps below.
    """

    id: int | None = None
    run_number: int | None = None
    run_attempt: int | None = None
    event: str | None = None
    status: str | None = None
    conclusion: str | None = None
    head_branch: str | None = None
    head_sha: str | None = None
    display_title: str | None = None
    head_commit: CommitPayload | None = None
    head_repository: RepositoryRefPayload | None = None
    pull_requests: list[PullRequestPayload] = Field(default_factory=list)
    actor: ActorPayload | str | None = None
    triggering_actor: ActorPayload | str | None = None
    run_started_at: Timestamp = None
    started_at: Timestamp = None
    created_at: Timestamp = None
    updated_at: Timestamp = None
    completed_at: Timestamp = None
    workflow_id: int | None = None
    name: str | None = None
    path: str | None = None


class RepositoryPayload(_GitHubPayload):
    """A repository object from ``GET /repos/{owner}/{repo}``."""

    id: int | None = None
    full_name: str | None = None
    owner: ActorPayload | str | None = None
    name: str | None = None
    is_private: bool = False
    default_branch: str | None = None


class AttemptRefPayload(_GitHubPayload):
    """The one field needed to find the execution a payload describes."""

    run_attempt: int | None = None


class WorkflowRunEvent(_GitHubPayload):
    """A ``workflow_run`` webhook delivery."""

    workflow_run: RunPayload | None = None
    workflow: WorkflowPayload | None = None


class WorkflowJobEvent(_GitHubPayload):
    """A ``workflow_job`` webhook delivery."""

    workflow_job: JobPayload | None = None


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

    # ---- validation and unwrapping ------------------------------------------

    @staticmethod
    def _as_mapping(payload: Any) -> Mapping[str, Any]:
        """A validated model or a raw dict, as a mapping."""
        if isinstance(payload, BaseModel):
            return payload.model_dump()
        return payload

    @staticmethod
    def _event_source(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        """The fragment of an event envelope, or the payload itself."""
        inner = payload.get(key)
        return inner if isinstance(inner, Mapping) else payload

    @classmethod
    def _run_payload(cls, payload: Any) -> RunPayload:
        """Validate a run object, unwrapping a ``workflow_run`` delivery."""
        source = cls._as_mapping(payload)
        event = WorkflowRunEvent.model_validate(source)
        if event.workflow_run is not None:
            return event.workflow_run
        return RunPayload.model_validate(source)

    @classmethod
    def _job_payload(cls, payload: Any) -> JobPayload:
        """Validate a job object, unwrapping a ``workflow_job`` delivery."""
        source = cls._as_mapping(payload)
        event = WorkflowJobEvent.model_validate(source)
        if event.workflow_job is not None:
            return event.workflow_job
        return JobPayload.model_validate(source)

    # ---- scalar values -------------------------------------------------------

    @staticmethod
    def _text(value: Any, default: str = "") -> str:
        """Return a trimmed string, falling back to ``default`` for blank values."""
        if value is None:
            return default
        text = str(value).strip()
        return text or default

    @classmethod
    def _status(cls, value: Any) -> str:
        """Map a GitHub status onto the canonical status set.

        Unknown values pass through so the state machine can reject them
        explicitly; a blank value maps to ``queued``.
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
    def _actor_login(cls, actor: ActorPayload | str | None) -> str | None:
        """The login out of a validated actor object or a plain string."""
        if isinstance(actor, ActorPayload):
            return actor.login or None
        return cls._text(actor) or None

    # ---- entities ------------------------------------------------------------

    @classmethod
    def workflow(cls, payload: Any) -> dict[str, Any]:
        """The workflow values a workflow object or a run payload carries.

        A run payload names ``workflow_id``; a workflow list entry names ``id``.
        A ``workflow_run`` event also carries the **current** workflow object as
        a sibling, and that is the only place ``state`` appears -- so its fields
        win over the run fragment's copies. A run payload without the sibling
        has no ``state`` at all, and the returned value is blank rather than
        guessed.
        """
        source = cls._as_mapping(payload)
        event = WorkflowRunEvent.model_validate(source)
        if event.workflow is not None:
            source = {**source, **event.workflow.model_dump()}
        workflow = WorkflowPayload.model_validate(source)
        return {
            "workflow_id": workflow.workflow_id or workflow.id or 0,
            "name": cls._text(workflow.name),
            "path": cls._text(workflow.path),
            "state": cls._text(workflow.state),
        }

    @classmethod
    def run(cls, payload: Any) -> dict[str, Any]:
        """The run-shaped values a run object carries, GitHub keys translated."""
        run = cls._run_payload(payload)
        actor = cls._actor_login(run.triggering_actor) or cls._actor_login(run.actor) or ""
        commit = run.head_commit
        head_repository = run.head_repository
        first_pull = run.pull_requests[0] if run.pull_requests else None
        return {
            "github_run_id": run.id or 0,
            "run_number": run.run_number or 0,
            "pr_number": first_pull.number if first_pull is not None else None,
            "event": cls._text(run.event),
            "branch": cls._text(run.head_branch),
            "commit_sha": cls._text(run.head_sha),
            "commit_message": cls._text(commit.message if commit is not None else None),
            "display_title": cls._text(run.display_title),
            "head_repo": cls._text(
                head_repository.full_name if head_repository is not None else None
            ),
            "commit_time": commit.timestamp if commit is not None else None,
            "commit_author": cls._text(
                commit.author.name if commit is not None and commit.author is not None else None
            ),
            "base_ref": cls._text(
                first_pull.base.ref
                if first_pull is not None and first_pull.base is not None
                else None
            ),
            "actor": actor,
        }

    @classmethod
    def attempt_number(cls, payload: Any) -> int | None:
        """Which execution a run or job payload describes, when it names one."""
        source = cls._as_mapping(payload)
        run_event = WorkflowRunEvent.model_validate(source)
        if run_event.workflow_run is not None:
            return run_event.workflow_run.run_attempt
        job_event = WorkflowJobEvent.model_validate(source)
        if job_event.workflow_job is not None:
            return job_event.workflow_job.run_attempt
        return AttemptRefPayload.model_validate(source).run_attempt

    @classmethod
    def attempt(cls, payload: Any) -> dict[str, Any]:
        """The attempt-shaped values a run or attempt object carries.

        ``GET /runs/{id}`` describes the latest attempt; ``/attempts/{n}``
        describes one. Both are read the same way. A completed execution that
        carries no ``completed_at`` uses ``updated_at``, its last state change.
        """
        run = cls._run_payload(payload)
        status = cls._status(run.status)
        completed_at = run.completed_at
        if not completed_at and status == "completed":
            completed_at = run.updated_at
        return {
            "status": status,
            "conclusion": cls._conclusion(run.conclusion),
            "actor": cls._actor_login(run.actor),
            "started_at": run.run_started_at or run.started_at,
            "completed_at": completed_at,
            "github_created_at": run.created_at,
            "github_updated_at": run.updated_at,
        }

    @classmethod
    def job(cls, payload: Any) -> dict[str, Any]:
        """The job-shaped values a job object carries."""
        job = cls._job_payload(payload)
        return {
            "github_job_id": job.id or 0,
            "name": cls._text(job.name),
            "status": cls._status(job.status),
            "conclusion": cls._conclusion(job.conclusion),
            "runner_name": cls._text(job.runner_name) or None,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
        }

    @classmethod
    def step(cls, payload: Any) -> dict[str, Any]:
        """The step-shaped values one entry of a job's ``steps`` array carries.

        GitHub gives a step no id of its own, so ``number`` is also its identity;
        the model mirrors it into ``github_step_id``.
        """
        step = payload if isinstance(payload, StepPayload) else StepPayload.model_validate(payload)
        return {
            "number": step.number or 0,
            "name": cls._text(step.name),
            "status": cls._status(step.status),
            "conclusion": cls._conclusion(step.conclusion),
            "started_at": step.started_at,
            "completed_at": step.completed_at,
        }

    @classmethod
    def steps(cls, payload: Any) -> list[dict[str, Any]]:
        """Every step of a job object, translated; empty when it carries none."""
        job = cls._job_payload(payload)
        return [cls.step(entry) for entry in job.steps]

    @classmethod
    def repository(cls, payload: Any, full_name: str) -> dict[str, Any]:
        """The repository values a repository object carries.

        ``full_name`` the caller asked for wins over the payload's own spelling:
        it is the name the caller confirmed, and GitHub answers slightly
        differently for a repository renamed mid-flight.
        """
        repository = RepositoryPayload.model_validate(cls._as_mapping(payload))
        name = cls._text(full_name or repository.full_name)
        if isinstance(repository.owner, ActorPayload):
            owner_login = repository.owner.login or ""
        else:
            owner_login = cls._text(repository.owner)
        if not owner_login and "/" in name:
            owner_login = name.split("/", 1)[0]
        short_name = cls._text(repository.name)
        if not short_name and "/" in name:
            short_name = name.split("/", 1)[1]
        return {
            "github_repo_id": repository.id or 0,
            "full_name": name,
            "owner": owner_login,
            "name": short_name or name,
            "is_private": repository.is_private,
            "default_branch": cls._text(repository.default_branch, "main"),
        }

    # ---- questions -----------------------------------------------------------

    @classmethod
    def belongs_to_pull_request(cls, payload: Any, number: int, head_sha: str) -> bool:
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
        run = cls._run_payload(payload)
        if run.pull_requests:
            return any(entry.number == number for entry in run.pull_requests)
        event = cls._text(run.event)
        same_head = cls._text(run.head_sha) == head_sha
        return event == "pull_request" and bool(head_sha) and same_head


__all__ = [
    "ActorPayload",
    "AttemptRefPayload",
    "CommitAuthorPayload",
    "CommitPayload",
    "GithubPayloads",
    "JobPayload",
    "PullRequestBasePayload",
    "PullRequestPayload",
    "RepositoryPayload",
    "RepositoryRefPayload",
    "RunPayload",
    "StepPayload",
    "WorkflowJobEvent",
    "WorkflowPayload",
    "WorkflowRunEvent",
]
