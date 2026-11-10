# src/rhosocial/github_actions_tracker/adapters/cli/orm_connection.py
"""The named connection the library's model CLI reads for this deployment.

``ghat orm`` forwards to ``rhosocial.activerecord.cli`` with
``--connection`` pointing here. A named connection is a zero-argument callable
returning a ``ConnectionConfig``, which is what keeps the password off the
command line: the value is read from the environment inside this function,
from the same ``GHAT_DB_*`` variables the service itself reads.

One vocabulary of variables, parsed in one place. ``adapters/cli/db.py``
already says why: a maintenance tool that re-parsed them could drift from the
service whose database it is preparing, so it delegates to the same
``Database.connection_config(get_settings())`` this does.

What the connection does *not* carry is anything the tracker's settings do not
name -- ``sslrootcert`` for a ``verify-full`` deployment, for instance.
``DatabaseSettings`` has no such field, and adding one for a CLI's benefit
would put a second SSL vocabulary next to the first. The library merges
``--config key=value`` overrides on top of what this returns, so the operator
supplies the extra values per invocation:

    ghat orm dql list --model AsyncRepository --config sslrootcert=/path/root.pem
"""

from __future__ import annotations

from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig


def tracker() -> PostgresConnectionConfig:
    """Connection coordinates for this deployment, from ``GHAT_DB_*``.

    Called with no arguments by the library's ``NamedConnectionResolver`` --
    a required parameter would make the resolution fail with
    ``NamedConnectionMissingParameterError`` before the operator ever saw a
    useful message, so this takes none.
    """
    from ...config import get_settings
    from ...database import Database

    return Database.connection_config(get_settings())


__all__ = ["tracker"]
