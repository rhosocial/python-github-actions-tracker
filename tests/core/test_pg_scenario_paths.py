# tests/core/test_pg_scenario_paths.py
"""How a shared scenarios file resolves the file paths inside it.

The suite reads ``python-activerecord-postgres``'s scenarios file, and that file
writes ``sslrootcert: tests/config/postgres_root.pem`` -- correct where that
project's suite runs, from its repository root, and wrong everywhere else.
Following the documented invocation from this repository used to produce::

    psycopg.OperationalError: root certificate file
    "tests/config/postgres_root.pem" does not exist

-- a message naming neither the scenario nor the file it came from, which is why
the workaround had been a private copy of the file with an absolute path. The
file itself sits at ``<project root>/tests/config``, so the root its entries are
relative to is two levels above it.

The fix belongs in :mod:`tests.pg`, not in every developer's head. These tests
pin what it must and must not do: rewrite a path only when the file exists at a
bounded number of levels above the scenarios file, and not relative to the
working directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pg import FILE_FIELDS, FILE_SEARCH_DEPTH, _resolve_file_fields


def _touch(root: Path, relative: str) -> Path:
    """Create ``root/relative`` and return it, making parents as needed."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    return path


@pytest.fixture
def shared_project(tmp_path: Path) -> Path:
    """A directory laid out like the sibling that owns the scenarios file.

    Padding keeps the decoy in ``test_a_match_further_up_than_the_bound_is_refused``
    *inside* tmp_path. On a CI runner tmp_path sits just a few levels under ``/``,
    so walking ``FILE_SEARCH_DEPTH + 1`` parents of a shallow fixture would land
    on the filesystem root, where the test cannot create the decoy. The number of
    pad directories is an implementation detail of the test, not of the resolver.
    """
    root = tmp_path
    for _level in range(FILE_SEARCH_DEPTH + 2):
        root = root / "pad"
    root.mkdir(parents=True)
    project = root / "python-activerecord-postgres"
    project.mkdir()
    (project / "tests" / "config").mkdir(parents=True)
    return project


class TestWhichFieldsArePaths:
    def test_the_list_covers_every_path_parameter_the_backend_forwards(self) -> None:
        """A path parameter that is missed keeps failing from another root.

        The list is checked against the library's own ``SSL_CONNECTION_PARAMS``
        rather than retyped, so a parameter the backend learns to forward shows
        up here as a failing test instead of as a surprise on someone's disk.
        """
        from rhosocial.activerecord.backend.impl.postgres.config import SSL_CONNECTION_PARAMS

        forwarded = set(SSL_CONNECTION_PARAMS)
        # ``sslmode`` and ``sslcompression`` are keywords, not files on disk.
        assert forwarded - set(FILE_FIELDS) == {"sslmode", "sslcompression"}


class TestTheRewrite:
    def test_a_path_relative_to_the_owning_project_is_found(
        self, shared_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The actual shape of the shared file: ``<root>/tests/config``."""
        certificate = _touch(shared_project, "tests/config/postgres_root.pem")
        elsewhere = shared_project.parent / "somewhere-else"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        resolved = _resolve_file_fields(
            {"sslrootcert": "tests/config/postgres_root.pem"},
            shared_project / "tests" / "config",
        )
        assert Path(resolved["sslrootcert"]) == certificate.resolve()

    def test_a_path_beside_the_file_itself_is_found(
        self, shared_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file that names its own directory rather than the project root."""
        certificate = _touch(shared_project, "tests/config/root.pem")
        elsewhere = shared_project.parent / "somewhere-else"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        resolved = _resolve_file_fields(
            {"sslrootcert": "root.pem"},
            shared_project / "tests" / "config",
        )
        assert Path(resolved["sslrootcert"]) == certificate.resolve()

    def test_a_path_that_resolves_from_the_cwd_is_left_alone(
        self, shared_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scenario meaning "relative to where you are standing" keeps it."""
        _touch(shared_project, "tests/config/only-here.pem")
        monkeypatch.chdir(shared_project)

        resolved = _resolve_file_fields(
            {"sslrootcert": "tests/config/only-here.pem"},
            shared_project / "tests" / "config",
        )
        assert resolved["sslrootcert"] == "tests/config/only-here.pem"

    def test_a_match_further_up_than_the_bound_is_refused(self, shared_project: Path) -> None:
        """A same-named file above the bound is a different file, not this one."""
        grandparent = shared_project.parent
        for _level in range(FILE_SEARCH_DEPTH + 1):
            grandparent = grandparent.parent
        _touch(grandparent, "tests/config/postgres_root.pem")

        resolved = _resolve_file_fields(
            {"sslrootcert": "tests/config/postgres_root.pem"},
            shared_project / "tests" / "config",
        )
        assert resolved["sslrootcert"] == "tests/config/postgres_root.pem"

    def test_a_path_that_exists_nowhere_is_not_invented(self, shared_project: Path) -> None:
        """A missing file must still fail with the name the operator wrote."""
        resolved = _resolve_file_fields(
            {"sslrootcert": "tests/config/postgres_root.pem"},
            shared_project / "tests" / "config",
        )
        assert resolved["sslrootcert"] == "tests/config/postgres_root.pem"

    def test_an_absolute_path_is_never_rewritten(self, shared_project: Path) -> None:
        _touch(shared_project, "tests/config/postgres_root.pem")
        resolved = _resolve_file_fields(
            {"sslcert": "/etc/ssl/cert.pem"}, shared_project / "tests" / "config"
        )
        assert resolved["sslcert"] == "/etc/ssl/cert.pem"

    def test_fields_that_are_not_paths_are_ignored(self, shared_project: Path) -> None:
        _touch(shared_project, "tests/config/postgres_root.pem")
        resolved = _resolve_file_fields(
            {"host": "db.example", "sslmode": "verify-full"},
            shared_project / "tests" / "config",
        )
        assert resolved == {"host": "db.example", "sslmode": "verify-full"}
