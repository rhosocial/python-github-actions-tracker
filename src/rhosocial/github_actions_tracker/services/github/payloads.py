# src/rhosocial/github_actions_tracker/services/github/payloads.py
"""GitHub payloads, translated into semantic values.

GitHub does not promise that its field names stay the same. The translation
therefore lives in exactly one class, in the service layer, and everything it
returns is keyed by the **model column** it describes -- ``status``,
``commit_sha``, ``github_run_id`` -- never by a GitHub spelling such as
``head_sha`` or ``run_attempt``.

That leaves two boundaries intact:

* a controller asks this service for values and hands them to a model; it never
  reads a payload key itself;
* a model stores values and never learns where they came from.

An API rename is one edit here, and a test can exercise the translation without
a database, a controller or a fake client.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


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

    @classmethod
    def _event_source(cls, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
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

    def workflow(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The workflow values a run or workflow payload carries.

        A run payload names ``workflow_id``; a workflow list entry names ``id``.
        A ``workflow_run`` event also carries the **current** workflow object as
        a sibling, and that is the only place ``state`` appears -- so its fields
        win over the run fragment's copies. A run payload without the sibling
        has no ``state`` at all, and the returned value is blank rather than
        guessed.
        """
        nested = payload.get("workflow")
        source = {**payload, **nested} if isinstance(nested, Mapping) else payload
        return {
            "workflow_id": self._as_int(source.get("workflow_id"))
            or self._as_int(source.get("id"))
            or 0,
            "name": self._text(source.get("name")),
            "path": self._text(source.get("path")),
            "state": self._text(source.get("state")),
        }

    def run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The run-shaped values a run payload carries, GitHub keys translated."""
        payload = self._event_source(payload, "workflow_run")
        head_commit = self._mapping(payload.get("head_commit"))
        pull_requests = payload.get("pull_requests")
        has_pull = isinstance(pull_requests, list) and bool(pull_requests)
        if has_pull and isinstance(pull_requests[0], Mapping):
            first_pull = pull_requests[0]
        else:
            first_pull = {}
        head_repository = self._mapping(payload.get("head_repository"))
        author = self._mapping(head_commit.get("author"))
        base = self._mapping(first_pull.get("base"))

        actor = ""
        for key in ("actor", "triggering_actor"):
            candidate = payload.get(key)
            if isinstance(candidate, Mapping):
                login = self._text(candidate.get("login"))
                if login:
                    actor = login
                    break
            elif candidate:
                actor = self._text(candidate)
                break

        return {
            "github_run_id": self._as_int(payload.get("id")) or 0,
            "run_number": self._as_int(payload.get("run_number")) or 0,
            "pr_number": self._as_int(first_pull.get("number")),
            "event": self._text(payload.get("event")),
            "branch": self._text(payload.get("head_branch")),
            "commit_sha": self._text(payload.get("head_sha")),
            "commit_message": self._text(head_commit.get("message")),
            "display_title": self._text(payload.get("display_title")),
            "head_repo": self._text(head_repository.get("full_name")),
            "commit_time": self._timestamp(head_commit.get("timestamp")),
            "commit_author": self._text(author.get("name")),
            "base_ref": self._text(base.get("ref")),
            "actor": actor,
        }

    def attempt_number(self, payload: Mapping[str, Any]) -> int | None:
        """Which execution a run or job payload describes, when it names one."""
        payload = self._event_source(payload, "workflow_run")
        return self._as_int(payload.get("run_attempt"))

    def attempt(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The attempt-shaped values a run or attempt payload carries.

        ``GET /runs/{id}`` describes the latest attempt; ``/attempts/{n}``
        describes one. Both are read the same way. A completed execution that
        carries no ``completed_at`` uses ``updated_at``, its last state change.
        """
        payload = self._event_source(payload, "workflow_run")
        status = self._status(payload.get("status"))
        completed_at = payload.get("completed_at")
        if not completed_at and status == "completed":
            completed_at = payload.get("updated_at")
        return {
            "status": status,
            "conclusion": self._conclusion(payload.get("conclusion")),
            "actor": self._actor_login(payload),
            "started_at": self._timestamp(
                payload.get("run_started_at") or payload.get("started_at")
            ),
            "completed_at": self._timestamp(completed_at),
            "github_created_at": self._timestamp(payload.get("created_at")),
            "github_updated_at": self._timestamp(payload.get("updated_at")),
        }

    def job(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The job-shaped values a job payload carries."""
        payload = self._event_source(payload, "workflow_job")
        return {
            "github_job_id": self._as_int(payload.get("id")) or 0,
            "name": self._text(payload.get("name")),
            "status": self._status(payload.get("status")),
            "conclusion": self._conclusion(payload.get("conclusion")),
            "runner_name": self._text(payload.get("runner_name")) or None,
            "started_at": self._timestamp(payload.get("started_at")),
            "completed_at": self._timestamp(payload.get("completed_at")),
        }

    def step(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """The step-shaped values one entry of a job's ``steps`` array carries.

        GitHub gives a step no id of its own, so ``number`` is also its identity;
        the model mirrors it into ``github_step_id``.
        """
        return {
            "number": self._as_int(payload.get("number")) or 0,
            "name": self._text(payload.get("name")),
            "status": self._status(payload.get("status")),
            "conclusion": self._conclusion(payload.get("conclusion")),
            "started_at": self._timestamp(payload.get("started_at")),
            "completed_at": self._timestamp(payload.get("completed_at")),
        }

    def steps(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Every step of a job payload, translated; empty when it carries none."""
        payload = self._event_source(payload, "workflow_job")
        entries = payload.get("steps")
        if not isinstance(entries, list):
            return []
        return [self.step(entry) for entry in entries if isinstance(entry, Mapping)]

    def repository(self, payload: Mapping[str, Any], full_name: str) -> dict[str, Any]:
        """The repository values a repository payload carries.

        ``full_name`` the caller asked for wins over the payload's own spelling:
        it is the name the caller confirmed, and GitHub answers slightly
        differently for a repository renamed mid-flight.
        """
        name = self._text(full_name or payload.get("full_name"))
        owner = self._mapping(payload.get("owner"))
        owner_login = self._text(owner.get("login")) or self._text(payload.get("owner"))
        if not owner_login and "/" in name:
            owner_login = name.split("/", 1)[0]
        short_name = self._text(payload.get("name"))
        if not short_name and "/" in name:
            short_name = name.split("/", 1)[1]
        return {
            "github_repo_id": self._as_int(payload.get("id")) or 0,
            "full_name": name,
            "owner": owner_login,
            "name": short_name or name,
            "is_private": bool(payload.get("is_private", False)),
            "default_branch": self._text(payload.get("default_branch"), "main"),
        }

    # ---- questions -----------------------------------------------------------

    def belongs_to_pull_request(
        self, payload: Mapping[str, Any], number: int, head_sha: str
    ) -> bool:
        """Whether a run payload belongs to pull request ``number``.

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
                isinstance(entry, Mapping) and self._as_int(entry.get("number")) == number
                for entry in pull_requests
            )
        event = self._text(payload.get("event"))
        same_head = self._text(payload.get("head_sha")) == head_sha
        return event == "pull_request" and bool(head_sha) and same_head


__all__ = ["GithubPayloads"]
