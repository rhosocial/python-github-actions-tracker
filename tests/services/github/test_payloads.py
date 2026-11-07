# tests/services/github/test_payloads.py
"""GitHub payload translation: values out, GitHub key names left behind.

No database and no client. These tests pin the one class that knows what
``head_sha``, ``run_attempt`` or ``pull_requests`` mean, so a rename in GitHub's
interface is caught here rather than in a controller or a model.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rhosocial.github_actions_tracker.services.github.payloads import GithubPayloads


class TestStatusVocabulary:
    def test_aliases_mean_queued(self) -> None:
        payloads = GithubPayloads()
        for alias in ("requested", "pending", "waiting", "QUEUED"):
            assert payloads.attempt({"status": alias})["status"] == "queued"

    def test_blank_and_unknown_values(self) -> None:
        payloads = GithubPayloads()
        assert payloads.attempt({})["status"] == "queued"
        assert payloads.attempt({"status": "frozen"})["status"] == "frozen"

    def test_conclusion_blanks_mean_unfinished(self) -> None:
        payloads = GithubPayloads()
        for blank in (None, "", "   "):
            assert payloads.attempt({"conclusion": blank})["conclusion"] is None
        assert payloads.job({"conclusion": " Failure "})["conclusion"] == "failure"


class TestTimestamps:
    def test_github_spellings(self) -> None:
        payloads = GithubPayloads()
        created = payloads.attempt({"created_at": "2026-10-10T12:34:56Z"})["github_created_at"]
        assert created == datetime(2026, 10, 10, 12, 34, 56, tzinfo=timezone.utc)
        offset = payloads.attempt({"created_at": "2026-10-10T12:34:56+02:00"})["github_created_at"]
        assert offset == datetime(2026, 10, 10, 10, 34, 56, tzinfo=timezone.utc)

    def test_naive_datetime_is_utc(self) -> None:
        payloads = GithubPayloads()
        parsed = payloads.attempt({"created_at": datetime(2026, 10, 10, 12, 34, 56)})[
            "github_created_at"
        ]
        assert parsed.tzinfo == timezone.utc

    def test_epoch_and_bad_values(self) -> None:
        payloads = GithubPayloads()
        epoch = payloads.attempt({"created_at": 1_760_000_000})["github_created_at"]
        assert epoch == datetime.fromtimestamp(1_760_000_000, tz=timezone.utc)
        assert payloads.attempt({"created_at": "not a timestamp"})["github_created_at"] is None


class TestCompletedAttempt:
    def test_missing_completed_at_falls_back_to_updated_at(self) -> None:
        payloads = GithubPayloads()
        values = payloads.attempt(
            {
                "status": "completed",
                "updated_at": "2026-10-10T12:40:00Z",
            }
        )
        assert values["completed_at"] == datetime(2026, 10, 10, 12, 40, tzinfo=timezone.utc)

    def test_in_progress_keeps_completed_at_empty(self) -> None:
        payloads = GithubPayloads()
        values = payloads.attempt({"status": "in_progress", "updated_at": "2026-10-10T12:40:00Z"})
        assert values["completed_at"] is None

    def test_start_accepts_both_github_spellings(self) -> None:
        payloads = GithubPayloads()
        rest = payloads.attempt({"run_started_at": "2026-10-10T12:00:00Z"})["started_at"]
        webhook = payloads.attempt({"started_at": "2026-10-10T12:00:00Z"})["started_at"]
        assert rest == webhook


class TestRunValues:
    def test_identity_and_commit_fields(self) -> None:
        values = GithubPayloads().run(
            {
                "id": 501,
                "run_number": 7,
                "run_attempt": 2,
                "event": "pull_request",
                "head_branch": "feature",
                "head_sha": "a" * 40,
                "display_title": "Fix it",
                "head_commit": {
                    "message": "Fix it",
                    "timestamp": "2026-10-10T12:00:00Z",
                    "author": {"name": "Ada"},
                },
                "head_repository": {"full_name": "fork/demo"},
                "pull_requests": [
                    {"number": 126, "base": {"ref": "main"}},
                ],
                "actor": {"login": "octocat"},
            }
        )
        assert values["github_run_id"] == 501
        assert values["run_number"] == 7
        assert values["pr_number"] == 126
        assert values["branch"] == "feature"
        assert values["commit_sha"] == "a" * 40
        assert values["commit_message"] == "Fix it"
        assert values["head_repo"] == "fork/demo"
        assert values["commit_author"] == "Ada"
        assert values["base_ref"] == "main"
        assert values["actor"] == "octocat"

    def test_attempt_number_is_optional(self) -> None:
        payloads = GithubPayloads()
        assert payloads.attempt_number({"run_attempt": "3"}) == 3
        assert payloads.attempt_number({}) is None

    def test_workflow_run_envelope_is_unwrapped(self) -> None:
        envelope = {"workflow_run": {"id": 501, "head_branch": "main"}}
        assert GithubPayloads().run(envelope)["github_run_id"] == 501


class TestWorkflowValues:
    def test_a_run_payload_has_no_state(self) -> None:
        values = GithubPayloads().workflow({"workflow_id": 7, "name": "CI", "path": "ci.yml"})
        assert values["workflow_id"] == 7
        assert values["state"] == ""

    def test_the_sibling_workflow_object_supplies_state(self) -> None:
        values = GithubPayloads().workflow(
            {
                "workflow_run": {"workflow_id": 7},
                "workflow": {"id": 7, "name": "CI", "path": "ci.yml", "state": "deleted"},
            }
        )
        assert values["state"] == "deleted"

    def test_a_workflow_list_entry_names_itself_by_id(self) -> None:
        values = GithubPayloads().workflow({"id": 11, "name": "CI", "path": "ci.yml"})
        assert values["workflow_id"] == 11


class TestJobAndStepValues:
    def test_job_values(self) -> None:
        values = GithubPayloads().job(
            {
                "id": 9001,
                "name": "test",
                "status": "completed",
                "conclusion": "failure",
                "runner_name": "ubuntu-latest",
                "started_at": "2026-10-10T12:00:00Z",
                "completed_at": "2026-10-10T12:05:00Z",
            }
        )
        assert values["github_job_id"] == 9001
        assert values["status"] == "completed"
        assert values["conclusion"] == "failure"
        assert values["runner_name"] == "ubuntu-latest"

    def test_steps_are_a_list_of_values(self) -> None:
        values = GithubPayloads().steps(
            {"steps": [{"number": 1, "name": "checkout", "status": "completed"}]}
        )
        assert values == [
            {
                "number": 1,
                "name": "checkout",
                "status": "completed",
                "conclusion": None,
                "started_at": None,
                "completed_at": None,
            }
        ]

    def test_a_job_without_steps_returns_an_empty_list(self) -> None:
        assert GithubPayloads().steps({}) == []


class TestRepositoryValues:
    def test_caller_spelling_wins(self) -> None:
        values = GithubPayloads().repository(
            {
                "id": 42,
                "full_name": "github/old-name",
                "owner": {"login": "github"},
                "name": "old-name",
                "is_private": True,
                "default_branch": "develop",
            },
            "acme/demo",
        )
        assert values["full_name"] == "acme/demo"
        assert values["owner"] == "github"
        assert values["name"] == "old-name"
        assert values["github_repo_id"] == 42
        assert values["is_private"] is True
        assert values["default_branch"] == "develop"

    def test_owner_and_name_fall_back_to_the_full_name(self) -> None:
        values = GithubPayloads().repository({}, "acme/demo")
        assert values["owner"] == "acme"
        assert values["name"] == "demo"
        assert values["default_branch"] == "main"


class TestPullRequestAttribution:
    def test_a_number_miss_is_exclusive_when_the_array_is_present(self) -> None:
        payloads = GithubPayloads()
        payload = {"pull_requests": [{"number": 126}], "event": "pull_request"}
        assert payloads.belongs_to_pull_request(payload, 126, "abc") is True
        assert payloads.belongs_to_pull_request(payload, 127, "abc") is False

    def test_a_closed_pull_request_falls_back_to_the_head_sha(self) -> None:
        payloads = GithubPayloads()
        payload = {"pull_requests": [], "event": "pull_request", "head_sha": "a" * 40}
        assert payloads.belongs_to_pull_request(payload, 126, "a" * 40) is True
        assert payloads.belongs_to_pull_request(payload, 126, "b" * 40) is False
