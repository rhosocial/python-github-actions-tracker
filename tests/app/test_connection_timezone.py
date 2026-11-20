# tests/app/test_connection_timezone.py
"""The session zone is pinned at connect time, not on every borrowed connection.

`timestamptz` values come back in the connection session's zone. On a database
whose server zone is not UTC, a connection without this option hands callers
local-time offsets, which is why the service pins UTC. It used to do so with a
`SET TIME ZONE` on every acquire — a round trip per database operation. The
libpq startup option does it once per physical connection instead.
"""

from __future__ import annotations

from rhosocial.github_actions_tracker.config import Settings
from rhosocial.github_actions_tracker.database import Database


def test_the_connection_config_pins_utc_at_startup() -> None:
    cfg = Database.connection_config(Settings())

    assert cfg.options is not None
    assert cfg.options.get("timezone") == "UTC"


def test_the_utc_option_survives_a_config_clone() -> None:
    # `ghat orm` applies `--config sslrootcert=...` by cloning the config. The
    # base to_dict() flattens `options` into top-level kwargs, which the postgres
    # config rejects; TrackerConnectionConfig keeps `options` nested so the
    # round trip is lossless.
    cfg = Database.connection_config(Settings())
    cloned = cfg.clone(sslrootcert="/path/to/root.pem")
    assert cloned.options == {"timezone": "UTC"}
    assert cloned.sslrootcert == "/path/to/root.pem"
