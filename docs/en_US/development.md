# Development

## Local environment

Clone the repository, then create and activate a virtual environment in whatever
way you normally do. The project supports Python 3.10 and newer; CI and the image
use 3.15.

The project depends on three sibling projects. Install them in editable mode in
your virtual environment, then install this project with its dev dependencies:

```bash
python -m pip install -e ../python-activerecord
python -m pip install -e ../python-activerecord-postgres
python -m pip install -e ../python-mcpserver

python -m pip install -e ".[dev]"
```

Use a package mirror if it is faster on your network. Each sibling has its own
release line, so check that the directory is on the branch you need.

## Running the tests

See [Testing](testing.md). A real PostgreSQL server is required. The suite binds
the models to that server and gives every test a freshly rebuilt schema. The
server comes from the environment or from a shared scenarios file; CI uses a
service container.

```bash
# specify the server directly
GHAT_DB_HOST=localhost GHAT_DB_PORT=5432 \
GHAT_DB_USER=github_actions_tracker GHAT_DB_PASSWORD=github_actions_tracker \
PYTHONPATH=tests python -m pytest tests -q

# or use the shared postgres_scenarios.yaml
GHAT_TEST_PG_SCENARIO_FILE=../python-activerecord-postgres/tests/config/postgres_scenarios.yaml \
GHAT_TEST_PG_SCENARIO=postgres_18 \
PYTHONPATH=tests python -m pytest tests -q
```

Without a server the tests skip and say why; in CI `GHAT_TEST_PG_REQUIRED=1` turns
that into a failure.

## The codebase in one paragraph

`src/rhosocial/github_actions_tracker/` holds `models/` (table mappings, state
machines, the row validators — the leaves), `services/` (the GitHub API surface
and `GithubPayloads`, the one translator of GitHub JSON into model values),
`controllers/` (the flows, and the only place ORM queries are written),
`views.py` (what the adapters shape), `config.py` / `database.py` /
`container.py` (wiring), and `adapters/` (one per protocol).

See [Architecture](architecture.md) and [Design decisions](design.md) for the
reasoning, and [Extending the service](extending.md) for how to add to it.
