# src/rhosocial/github_actions_tracker/adapters/cli/orm.py
"""``ghat orm``: the library's model CLI, pointed at this deployment.

The core package ships a model CLI (``rhosocial-activerecord``, on the
``feature/activerecord-cli`` line): ``dml`` / ``dql`` / ``set`` / ``cte`` over
any ActiveRecord model, resolving the backend and connection from explicit
class references. It is generic by design -- the core package knows nothing
about the packages that happen to ship a backend -- so using it here means
naming this deployment's models, backend and connection every time.

This module is the delegation that removes that typing, not a reimplementation:

* ``--model`` accepts a bare class name and completes it to this project's
  models module, where both the sync and the async twins are published;
* ``--backend-class`` is inferred from the ``Async`` prefix of that class name,
  which is this project's sync/async naming rule (the same one
  ``tests/models/test_sync_twins.py`` enforces) -- an async model needs the
  async backend or the library refuses the pair;
* ``--connection`` points at :mod:`orm_connection`, so the coordinates come
  from ``GHAT_DB_*`` and the password never appears in ``ps`` output.

Whatever the operator did type wins: an explicit ``--backend-class``,
``--connection`` or ``--config-class`` is forwarded untouched, and
``--config key=value`` overrides are merged by the library on top of the
connection above.

The library's CLI is not on a release line yet, so the import is guarded: the
failure names the line to install rather than surfacing a bare ImportError
from argparse's argument list.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

#: Where the library's model CLI lives. Imported lazily, because its absence is
#: an expected state until the branch reaches a release line.
CLI_MODULE = "rhosocial.activerecord.cli"

#: This project's models, where ``models/__init__.py`` publishes every sync and
#: async class, so a bare class name resolves without the operator typing it.
MODEL_MODULE = "rhosocial.github_actions_tracker.models"

#: The two postgres backends. Chosen by name, because the project names its
#: twins with an ``Async`` prefix and nothing else does.
SYNC_BACKEND = "rhosocial.activerecord.backend.impl.postgres.backend:PostgresBackend"
ASYNC_BACKEND = "rhosocial.activerecord.backend.impl.postgres.backend:AsyncPostgresBackend"

#: The named connection this deployment reads. See :mod:`orm_connection`.
NAMED_CONNECTION = "rhosocial.github_actions_tracker.adapters.cli.orm_connection:tracker"

#: The flags this wrapper understands; passing one overrides its injection.
BACKEND_FLAG = "--backend-class"
CONNECTION_FLAG = "--connection"
CONFIG_CLASS_FLAG = "--config-class"
MODEL_FLAG = "--model"

#: Exit code for "the model CLI line is not installed". Matches the library's
#: ``EXIT_BACKEND_UNAVAILABLE``, which is the same answer for the same reason.
EXIT_CLI_UNAVAILABLE = 4

_MISSING_CLI = (
    "error: the model CLI lives on python-activerecord's 'feature/activerecord-cli' "
    "line, which is not a release line yet, so it is not installed here. Install the "
    "core package from that branch (or put its src on PYTHONPATH) and retry."
)


def _value_of(flag: str, argv: Sequence[str]) -> str | None:
    """The value of ``flag`` in ``argv``, or ``None`` when it is absent.

    Both spellings are read: ``--flag value`` and ``--flag=value``. A repeated
    flag is the caller's problem -- argparse takes the last one, and so does
    this, by returning the last match.
    """
    found: str | None = None
    for index, token in enumerate(argv):
        if token == flag:
            if index + 1 < len(argv):
                found = argv[index + 1]
        elif token.startswith(flag + "="):
            found = token.split("=", 1)[1]
    return found


def _has_flag(flag: str, argv: Sequence[str]) -> bool:
    """Whether ``flag`` appears in either spelling."""
    return _value_of(flag, argv) is not None or any(
        token == flag or token.startswith(flag + "=") for token in argv
    )


def _with_value(flag: str, value: str, argv: list[str]) -> list[str]:
    """``argv`` with ``flag``'s value replaced, in whichever spelling it used."""
    rewritten = list(argv)
    for index, token in enumerate(rewritten):
        if token == flag and index + 1 < len(rewritten):
            rewritten[index + 1] = value
            return rewritten
        if token.startswith(flag + "="):
            rewritten[index] = f"{flag}={value}"
            return rewritten
    return rewritten


def complete_model(value: str) -> str:
    """Complete a model reference to ``module:Class``.

    ``module:Class`` and the dotted ``module.Class`` are passed through -- the
    library accepts both. A bare ``Class`` is completed with this project's
    models module, so the operator names the class and nothing else.
    """
    if ":" in value or "." in value:
        return value
    return f"{MODEL_MODULE}:{value}"


def backend_for(model_reference: str) -> str:
    """The backend class reference a model of this name needs.

    Read off the ``Async`` prefix of the class name, which is the project's
    naming rule rather than a guess: the sync and async twins are separate
    classes, an async model requires the async backend, and the library
    refuses a mismatched pair before touching the database.
    """
    class_name = complete_model(model_reference).rsplit(":", 1)[-1].rsplit(".", 1)[-1]
    return ASYNC_BACKEND if class_name.startswith("Async") else SYNC_BACKEND


def build_forward_argv(rest: Sequence[str]) -> list[str]:
    """The library CLI's ``argv`` for the operator's arguments.

    Injects only what is missing, and nothing at all when there is no
    ``--model``: the library reports the missing argument itself, and a
    half-configured invocation would answer a different question first.
    """
    forwarded = list(rest)
    if not forwarded:
        # No arguments at all is a request for the command tree, not an error.
        return ["--help"]

    model = _value_of(MODEL_FLAG, forwarded)
    if model is None:
        return forwarded

    forwarded = _with_value(MODEL_FLAG, complete_model(model), forwarded)
    if not _has_flag(BACKEND_FLAG, forwarded):
        forwarded = [*forwarded, BACKEND_FLAG, backend_for(model)]
    if not _has_flag(CONNECTION_FLAG, forwarded) and not _has_flag(CONFIG_CLASS_FLAG, forwarded):
        forwarded = [*forwarded, CONNECTION_FLAG, NAMED_CONNECTION]
    return forwarded


def _library_cli() -> Any:
    """Import the model CLI, or explain where it lives and return ``None``.

    ``None`` rather than an exception: an unavailable dependency is a state an
    operator can act on (install the line), not a crash, and returning keeps
    :func:`run` a function that always answers with an exit code -- which is
    what a script branching on one needs, and what the tests assert.
    """
    try:
        return __import__(CLI_MODULE, fromlist=["main"])
    except ImportError:
        print(_MISSING_CLI, file=sys.stderr)
        return None


def run(rest: Sequence[str]) -> int:
    """Forward to the model CLI and return its exit code.

    The exit codes are the library's own (0 success, 1 error, 2 usage, 3 not
    found, 4 unavailable, 5 confirmation required, 6 read-only), which is why
    this returns rather than exiting: the caller propagates them unchanged so a
    script branching on one code sees one vocabulary.
    """
    module = _library_cli()
    if module is None:
        return EXIT_CLI_UNAVAILABLE

    argv = build_forward_argv(rest)
    try:
        return int(module.main(argv) or 0)
    except SystemExit as exc:  # --help, and argparse's own usage errors
        return int(exc.code) if exc.code is not None else 0


__all__ = ["EXIT_CLI_UNAVAILABLE", "backend_for", "build_forward_argv", "complete_model", "run"]
