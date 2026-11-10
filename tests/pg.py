# tests/pg.py
"""Where a PostgreSQL test server comes from.

The rule: the **environment always wins over the file**. CI supplies a service
container through the same ``GHAT_DB_*`` variables the application reads, and
a stale file on a developer's disk must not redirect a CI run at a server that is
not there. A developer names a scenarios file -- the sibling
``python-activerecord-postgres`` repository ships one with a ``postgres_18``
entry -- or simply exports the variables.

The database is always ``ghat_test``, whatever a scenarios file says: the YAML
is shared with another project and names *its* database. Everything else (host,
port, user, password, sslmode) comes from the environment, or from the file when
the environment is silent.

The file is shared, so a *file* it names is relative to the file itself. The
sibling project's suite runs from that project's root and its entries write
``sslrootcert: tests/config/postgres_root.pem``; reading such a file from this
repository would resolve that path against the wrong directory and fail with a
certificate error that names neither the scenario nor the file it came from.
:func:`_resolve_file_fields` closes that gap without changing a path that is
already correct where it stands.

This is a contract, not a location: nothing here knows which server exists.
``resolve_scenario()`` returns ``None`` when nothing names one, and the fixture in
``conftest.py`` turns that into a skip.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

#: The database this project's tests run in, on whichever server the environment
#: names. Constant on purpose: a second database name would split test state
#: across two places for no gain.
TEST_DATABASE = "ghat_test"

#: Where a scenarios file may be named. The second name is the sibling project's,
#: so one file serves both without either hard-coding the other's path.
SCENARIO_FILE_ENV = "GHAT_TEST_PG_SCENARIO_FILE"
SCENARIO_FILE_ALIAS = "POSTGRES_SCENARIOS_CONFIG_PATH"
#: Which scenario to read from that file.
SCENARIO_NAME_ENV = "GHAT_TEST_PG_SCENARIO"
SCENARIO_NAME = "postgres_18"
#: The conventional location, relative to this repository. Deliberately absent
#: from the tree: a committed scenario would make every run reach for a server
#: that may not exist, and a committed password is worse than no default.
SCENARIO_FILE = Path("tests") / "config" / "postgres_scenarios.yaml"

#: Connection field -> the environment variables that override it, first wins.
#: ``GHAT_DB_*`` is what the application reads, so a CI job that configures
#: the database once configures the tests too; ``PG*`` is the libpq convention.
FIELD_ENV: dict[str, tuple[str, ...]] = {
    "host": ("GHAT_DB_HOST", "PGHOST"),
    "port": ("GHAT_DB_PORT", "PGPORT"),
    "username": ("GHAT_DB_USER", "PGUSER"),
    "password": ("GHAT_DB_PASSWORD", "PGPASSWORD"),
    "sslmode": ("GHAT_DB_SSLMODE", "PGSSLMODE"),
}

#: Connection fields whose value is a path on disk. They are the reason a shared
#: scenarios file cannot be read from another project's root unchanged: the path
#: is written relative to the file's project, and libpq resolves it against the
#: CWD.
FILE_FIELDS: tuple[str, ...] = ("sslcert", "sslkey", "sslrootcert", "sslcrl")

#: How far above the scenario file a relative path may be looked for. The shared
#: file lives at ``<project root>/tests/config``, so two levels cover it; the
#: third covers a file placed one directory higher. The bound is the point -- a
#: match further up is a different file that happens to share a name.
FILE_SEARCH_DEPTH = 3


def _repo_root() -> Path:
    """The repository root, from the location of this file."""
    return Path(__file__).resolve().parents[1]


def _expand(value: Any) -> Any:
    """Replace ``${VAR}`` with its environment value, the sibling's convention.

    It is what lets a scenarios file be shared without carrying a password: the
    file says which variable holds it.
    """
    if not isinstance(value, str) or "${" not in value:
        return value
    return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), value)


def _resolve_file_fields(scenario: dict[str, Any], base: Path) -> dict[str, Any]:
    """Point a relative file path at the directory the scenario was read from.

    The scenarios file is shared with the sibling ``python-activerecord-postgres``
    project, whose entries write ``sslrootcert: tests/config/postgres_root.pem``.
    That is correct where that project's own suite runs -- from its repository
    root -- and wrong everywhere else, including here, where the documented
    invocation reads the very same file and then fails with ``root certificate
    file "tests/config/postgres_root.pem" does not exist``, naming neither the
    scenario nor the file it came from. A path written that way is relative to
    the project that owns the file, and that project root is one of the
    directories above the file itself: it lives at ``<root>/tests/config``.

    The search is therefore bounded and ordered. ``base`` is the directory the
    scenario file was read from; a rewrite looks there, then at each parent, up
    to :data:`FILE_SEARCH_DEPTH` levels -- nearest first, because that is the
    one the entry was written against. A match further up the tree is not the
    same file, it is a coincidence, and a bounded walk refuses to mistake one
    for the other. Nothing is rewritten when the path already resolves from the
    working directory, so a scenario that means "relative to where you are
    standing" keeps meaning that; and a path that exists in neither place is
    left alone, so it still fails with the name the operator wrote.
    """
    resolved = dict(scenario)
    for field in FILE_FIELDS:
        value = resolved.get(field)
        if not isinstance(value, str) or not value:
            continue
        candidate = Path(value)
        if candidate.is_absolute() or candidate.is_file():
            continue
        for directory in (base, *base.parents)[:FILE_SEARCH_DEPTH]:
            beside = (directory / candidate).resolve()
            if beside.is_file():
                resolved[field] = str(beside)
                break
    return resolved


def _scenario_from_file() -> dict[str, Any] | None:
    """Read one scenario from a YAML file, or ``None`` when there is no file."""
    candidates = (
        os.getenv(SCENARIO_FILE_ENV),
        os.getenv(SCENARIO_FILE_ALIAS),
        str(_repo_root() / SCENARIO_FILE),
    )
    located = next((c for c in candidates if c and Path(c).is_file()), None)
    if located is None:
        return None

    path = Path(located)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    name = os.getenv(SCENARIO_NAME_ENV, SCENARIO_NAME)
    scenario = (data.get("scenarios") or {}).get(name)
    if not scenario:
        # A file that exists but lacks the scenario is a mistake, not a
        # configuration: it would otherwise read as "no server configured".
        raise ValueError(f"scenario '{name}' is not defined in {path}")

    return _resolve_file_fields(
        {key: _expand(value) for key, value in scenario.items() if key != "enabled"},
        path.parent,
    )


def _scenario_from_env() -> dict[str, Any]:
    """Read connection fields from the environment; empty when none are set."""
    found: dict[str, Any] = {}
    for field, names in FIELD_ENV.items():
        for name in names:
            value = os.getenv(name)
            if value:
                found[field] = value
                break
    return found


def resolve_scenario() -> dict[str, Any] | None:
    """Connection parameters: file underneath, environment on top.

    Returns:
        Keyword arguments for
        :class:`~rhosocial.activerecord.backend.impl.postgres.config.PostgresConnectionConfig`,
        minus ``database`` -- that is always :data:`TEST_DATABASE` and is added by
        the fixture. ``None`` when nothing in the environment names a server.
    """
    scenario: dict[str, Any] = _scenario_from_file() or {}
    scenario.update(_scenario_from_env())
    scenario.pop("database", None)

    if not scenario.get("host"):
        return None
    if "port" in scenario:
        scenario["port"] = int(scenario["port"])
    return scenario


__all__ = [
    "FIELD_ENV",
    "FILE_FIELDS",
    "FILE_SEARCH_DEPTH",
    "SCENARIO_FILE",
    "SCENARIO_FILE_ALIAS",
    "SCENARIO_FILE_ENV",
    "SCENARIO_NAME",
    "SCENARIO_NAME_ENV",
    "TEST_DATABASE",
    "resolve_scenario",
]
