# Testing

## A real PostgreSQL is required

The suite binds the models to a real server and gives every test a freshly rebuilt
schema. The server comes from the environment or from a shared scenarios file; CI
uses a service container.

```bash
# specify the server directly
GHAT_DB_HOST=localhost GHAT_DB_PORT=5432 \
GHAT_DB_USER=github_actions_tracker GHAT_DB_PASSWORD=github_actions_tracker \
PYTHONPATH=tests python -m pytest tests -q

# or use the shared postgres_scenarios.yaml
GHAT_TEST_PG_SCENARIO_FILE=../python-activerecord-postgres/tests/config/postgres_scenarios.yaml \
GHAT_TEST_PG_SCENARIO=postgres_exts_19 \
PYTHONPATH=tests python -m pytest tests -q
```

Without a server the tests skip and say why; in CI `GHAT_TEST_PG_REQUIRED=1` turns
that into a failure.

**File paths inside a scenarios file** (`sslrootcert` and friends) are relative to
the project that owns the file, not to the directory pytest runs from: the shared
file writes `tests/config/postgres_root.pem`, which does not exist relative to
this repository's root. `tests/pg.py` therefore resolves such a path by looking a
bounded number of levels above the scenarios file (`FILE_SEARCH_DEPTH`) and
rewriting it to an absolute path, while leaving a path that already resolves from
the working directory exactly as written. The command above works as-is; if you
write your own scenarios file, use absolute paths and save the reader the search.

## Scope

Run the files affected by your change. The full suite takes 3–5 minutes against a
remote server.

```bash
PYTHONPATH=tests python -m pytest tests/core -q
PYTHONPATH=tests python -m pytest tests/controllers -q
```

## Static checks

```bash
ruff check src tests
black --check src tests
isort --check-only src tests
```
