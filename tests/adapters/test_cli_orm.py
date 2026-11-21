# tests/adapters/test_cli_orm.py
"""``ghat orm``: the delegation to the library's model CLI.

The command forwards an argument list this project does not own, so almost
everything it must get right is *argv construction*: complete the model the
operator typed, infer the backend the model's sync/async half needs, and supply
the connection without the password ever reaching the command line. Those are
pure functions and are tested as such -- no library, no database, no network.

Only two tests need more: the gate (what an operator sees when the installed
core package has no model CLI) and one smoke run through a real PostgreSQL.
Both skip with a reason rather than fail, the same contract the scenarios file
uses: the suite runs on machines whose core package predates the CLI (it was
added in ``v1.0.0.dev30`` / #138, and before that it lived on the
``feature/activerecord-cli`` branch), and a missing optional dependency is not a
broken build.
"""

from __future__ import annotations

import sys

import pytest

from rhosocial.github_actions_tracker.adapters.cli import orm

CLI_MODULE = "rhosocial.activerecord.cli"


def _cli_available() -> bool:
    try:
        __import__(CLI_MODULE, fromlist=["main"])
    except ImportError:
        return False
    return True


needs_cli = pytest.mark.skipif(
    not _cli_available(),
    reason=(
        "the installed core package is older than "
        f"{orm.CLI_AVAILABLE_SINCE}, which is where the model CLI was added"
    ),
)


class TestModelCompletion:
    def test_a_bare_class_name_is_completed_with_the_models_module(self) -> None:
        """The operator names the class; the module is this project's."""
        assert orm.complete_model("AsyncWorkflowRun") == (
            "rhosocial.github_actions_tracker.models:AsyncWorkflowRun"
        )

    def test_a_full_reference_is_left_alone(self) -> None:
        assert orm.complete_model("app.models:User") == "app.models:User"

    def test_a_dotted_reference_is_left_alone(self) -> None:
        """The library accepts ``module.Class`` too, so completion must not eat it."""
        assert orm.complete_model("app.models.User") == "app.models.User"

    def test_completion_happens_in_both_spellings_of_the_flag(self) -> None:
        assert orm.build_forward_argv(["dql", "count", "--model", "AsyncJob"]) == [
            "dql",
            "count",
            "--model",
            "rhosocial.github_actions_tracker.models:AsyncJob",
            "--backend-class",
            orm.ASYNC_BACKEND,
            "--connection",
            orm.NAMED_CONNECTION,
        ]
        assert orm.build_forward_argv(["dql", "count", "--model=AsyncJob"]) == [
            "dql",
            "count",
            "--model=rhosocial.github_actions_tracker.models:AsyncJob",
            "--backend-class",
            orm.ASYNC_BACKEND,
            "--connection",
            orm.NAMED_CONNECTION,
        ]


class TestBackendInference:
    def test_an_async_model_gets_the_async_backend(self) -> None:
        assert orm.backend_for("AsyncWorkflowRun").endswith(":AsyncPostgresBackend")

    def test_a_sync_model_gets_the_sync_backend(self) -> None:
        assert orm.backend_for("WorkflowRun").endswith(":PostgresBackend")

    def test_the_pair_matters_and_the_library_checks_it(self) -> None:
        """Why inference is not optional: the library refuses a mismatched pair.

        An async model driven by a sync backend fails validation in
        ``resolve_connection`` before any connection is opened, so naming the
        wrong one here would surface as a backend error far from the cause.
        """
        assert orm.backend_for("AsyncRepository") != orm.backend_for("Repository")


def _flag_values(argv: list[str], flag: str) -> list[str]:
    """Every value of ``flag`` in ``argv``, in either spelling.

    Written here rather than imported from the module under test: a test that
    reused the wrapper's own parser would agree with a bug in it.
    """
    values: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == flag and index + 1 < len(argv):
            values.append(argv[index + 1])
            index += 2
            continue
        if token.startswith(flag + "="):
            values.append(token.split("=", 1)[1])
        index += 1
    return values


class TestWhatTheOperatorTypedWins:
    def test_an_explicit_backend_class_is_not_overridden(self) -> None:
        forwarded = orm.build_forward_argv(
            ["dql", "count", "--model", "AsyncJob", "--backend-class", "app:MyBackend"]
        )
        assert _flag_values(forwarded, "--backend-class") == ["app:MyBackend"]
        # The connection is still supplied: only the backend was the caller's.
        assert _flag_values(forwarded, "--connection") == [orm.NAMED_CONNECTION]

    def test_an_explicit_connection_is_not_overridden(self) -> None:
        forwarded = orm.build_forward_argv(
            ["dql", "count", "--model", "AsyncJob", "--connection", "app:main_db"]
        )
        assert _flag_values(forwarded, "--connection") == ["app:main_db"]
        assert orm.NAMED_CONNECTION not in forwarded

    def test_an_explicit_config_class_suppresses_the_named_connection(self) -> None:
        """The library refuses to take both, so injecting ours would break it."""
        forwarded = orm.build_forward_argv(
            [
                "dql",
                "count",
                "--model",
                "AsyncJob",
                "--config-class",
                "app:MyConfig",
                "--config",
                "database=other",
            ]
        )
        assert orm.NAMED_CONNECTION not in forwarded

    def test_a_config_override_keeps_the_named_connection(self) -> None:
        """``--config`` values merge on top of the connection, per the library."""
        forwarded = orm.build_forward_argv(
            ["dql", "list", "--model", "AsyncJob", "--config", "sslmode=require"]
        )
        assert _flag_values(forwarded, "--config") == ["sslmode=require"]
        assert _flag_values(forwarded, "--connection") == [orm.NAMED_CONNECTION]

    def test_a_flag_given_as_one_token_is_recognised(self) -> None:
        forwarded = orm.build_forward_argv(
            ["dql", "count", "--model=AsyncJob", "--backend-class=app:MyBackend"]
        )
        assert _flag_values(forwarded, "--model") == [
            "rhosocial.github_actions_tracker.models:AsyncJob"
        ]
        assert _flag_values(forwarded, "--backend-class") == ["app:MyBackend"]


class TestNothingIsInvented:
    def test_no_model_means_nothing_is_injected(self) -> None:
        """The library reports the missing argument; a half-configured
        invocation would answer a different question first."""
        forwarded = orm.build_forward_argv(["dql", "list"])
        assert forwarded == ["dql", "list"]

    def test_no_arguments_asks_for_the_command_tree(self) -> None:
        assert orm.build_forward_argv([]) == ["--help"]

    def test_a_model_with_no_other_flags_still_gets_its_connection(self) -> None:
        forwarded = orm.build_forward_argv(["dql", "exists", "--model", "Workflow"])
        assert "--connection" in forwarded
        assert "--backend-class" in forwarded


class TestTheGate:
    def test_a_missing_cli_names_the_version_it_arrived_in(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The one failure an operator will actually hit, stated usefully.

        The module entry is set to ``None`` -- which makes any ``import`` of it
        raise -- rather than deleting it, because in an environment that *has*
        the CLI a deletion would not survive the next import and the test would
        silently stop covering the gate.
        """
        monkeypatch.setitem(sys.modules, CLI_MODULE, None)

        assert orm.run(["dql", "count", "--model", "AsyncJob"]) == orm.EXIT_CLI_UNAVAILABLE
        message = capsys.readouterr().err
        assert orm.CLI_AVAILABLE_SINCE in message
        assert "older than that" in message
        assert "Upgrade" in message

    @needs_cli
    def test_the_unavailable_code_matches_the_librarys_own(self) -> None:
        """A script branching on the code must see one vocabulary.

        4 is the library's ``EXIT_BACKEND_UNAVAILABLE``: "the thing that was
        asked for is not installed". Asserting the number against the library
        that defines it keeps the two from drifting apart silently.
        """
        from rhosocial.activerecord.cli import errors

        assert orm.EXIT_CLI_UNAVAILABLE == errors.EXIT_BACKEND_UNAVAILABLE


@needs_cli
class TestAgainstALiveDatabase:
    def test_a_count_travels_the_whole_chain(self, pg_database, bound_models) -> None:
        """Model completion, backend inference, connection, query, output.

        Run as a subprocess, not in-process: the library's CLI calls
        ``model.configure(...)``, which writes the model classes' own backend
        attributes. Doing that inside the test process would leak a binding
        into every later test in it, which is precisely the order dependence
        ``tests/conftest.py`` exists to prevent. As a subprocess it is also the
        real operator path -- a separate process, reading ``GHAT_DB_*``.

        Depends on ``pg_database`` for the coordinates (it creates ``ghat_test``
        on a fresh runner) and on ``bound_models`` so the schema actually exists
        in it — a bare database has no ``ghat_repositories`` to count. Taking the
        name from the fixture rather than hardcoding it keeps the subprocess
        pointed at the database the suite prepared.
        """
        import os
        import subprocess

        env = dict(os.environ)
        env.update(
            {
                "GHAT_DB_HOST": str(pg_database["host"]),
                "GHAT_DB_PORT": str(pg_database["port"]),
                "GHAT_DB_USER": str(pg_database["username"]),
                "GHAT_DB_PASSWORD": str(pg_database["password"]),
                "GHAT_DB_NAME": str(pg_database["database"]),
                "GHAT_DB_SSLMODE": str(pg_database.get("sslmode", "prefer")),
            }
        )
        # The tracker's settings carry no certificate path, so a verify-full
        # scenario supplies one as the documented --config override.
        if pg_database.get("sslrootcert"):
            env["GHAT_DB_SSLMODE"] = str(pg_database["sslmode"])
            extra = ["--config", f"sslrootcert={pg_database['sslrootcert']}"]
        else:
            extra = []

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "rhosocial.github_actions_tracker.adapters.cli.main",
                "orm",
                "dql",
                "count",
                "--model",
                "AsyncRepository",
                *extra,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr[-2000:]
        assert result.stdout.strip().isdigit(), result.stdout[-500:]
