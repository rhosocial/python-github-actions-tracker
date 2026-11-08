# Extending the service

This page is a working guide, not a tour: it answers "I want X" for the four kinds of X this
service is built to accept. Each section ends with the files that change and the test that
must pass.

## Where an extension goes

| I want to … | It lives in | It must **not** … |
|---|---|---|
| read or write another GitHub endpoint | `services/github/` (protocol + implementation) | know a flow, or write a row |
| accept a new payload field or event | `services/github/payloads.py` (`GithubPayloads`) | appear as a GitHub field name anywhere else |
| change what a delivery does | a `Controller` | know which protocol asked |
| add REST, MCP or CLI surface | `adapters/` | import `models` |
| store a new fact | `models/` + `schema/model.py` | read a payload, or name a GitHub key |
| change what an answer looks like | `views.py` | re-decide a flow |

## The map

```text
adapters/      one App per protocol: MCP, REST, CLI
controllers/   flows, and the only place ORM queries are written
services/      abilities, wrapped by protocol; github/ holds the one payload translator
models/        the leaf: table mapping, relations, state machines, row validation
views.py       what the adapters shape
config.py      settings from GHAT_* environment variables
database.py    the connection pool and the per-request scope
container.py   the composition root
```

## Rules that a change is expected to keep

* one `save()` is one complete write; no transaction spans models;
* `models/` never imports `services/`, `controllers/`, `adapters/` or `github/`;
* `controllers/` and `services/` contain no module-level function except an entry point;
* no `Mapping` parameter on a public model method, and no `*_from_remote` name;
* no `RawSQL` and no hand-built `SELECT`.

The commands that check this:

```bash
# models stay a leaf (must print nothing)
rg -n 'from \.\.(?!models)' -P -g '*.py' src/rhosocial/github_actions_tracker/models

# no payload parsing and no module-level functions in models
rg -n '_from_remote' -g '*.py' src/rhosocial/github_actions_tracker/models
rg -n '^(async )?def ' -g '*.py' src/rhosocial/github_actions_tracker/models

# adapters do not touch models directly
rg -n '^\s*from \.+models' -g '*.py' src/rhosocial/github_actions_tracker/adapters
```

## Add a GitHub capability

1. Declare the protocol next to the others in `services/github/protocols.py`, structurally:
   a method returning what the caller needs, never how it is spelled.
2. Implement it in `services/github/<resource>.py` against the same `AsyncGithubClient`.
3. Let the controller that needs it take the protocol as a constructor argument. The
   container already builds one instance per resource group; add yours there.
4. Write a recording fake in `tests/conftest.py`; the flow test asserts on that fake, not
   on GitHub.

Files: `services/github/protocols.py`, `services/github/<resource>.py`,
`controllers/<flow>.py`, `container.py`.
Test: the controller's flow suite with the fake; no network in CI.

## Add a field or an event

1. Extend the matching Pydantic model in `services/github/payloads.py`. Unknown keys are
   ignored today; add only what you read, and give it a type.
2. Extend the `GithubPayloads` method to return the model-shaped value, keyed by the model
   column (`head_sha` → `commit_sha`), not by GitHub's spelling.
3. Add the controller entry that stores it: `store_*` accepts the payload, calls the
   service, resolves the parent, then calls the model's value entry.
4. If the model needs a new column, add it in `schema/model.py` and rebuild the schema —
   there is no in-place migration.

Files: `services/github/payloads.py`, `controllers/run.py` (or the relevant row layer),
`models/<entity>.py` when a column is involved.
Test: `tests/services/github/test_payloads.py` for the translation; the controller suite
for the cascade.

## Add an entity

A new level in the cascade touches both halves of every model:

1. `models/<entity>.py` — the sync twin and the `Async` twin, sharing one
   `__table_name__`.
2. Relations: declare both directions with `inverse_of`, and add the pair to the other
   side's class.
3. `schema/model.py` — the table, its foreign key (`RESTRICT` for anything with history),
   the unique constraint, and the partition column if it stores logs.
4. Row validation in `models/validators.py` or on the model itself — never a payload.
5. `services/github/payloads.py` — the payload shape and the value translator.
6. `controllers/<flow>.py` — the `store_*` entry that resolves the parent and writes in
   order.
7. `views.py` — the record, with `*_cached` / `latest_*` naming when the number is local.
8. Publish the model in `models/__init__.py`; the relation annotations stay strings.
9. Tests: a model test that constructs rows with values only; an AST-safe cascade test; the
   sync twin must cover a full chain (repo → … → new entity).

The tests that fail first if one of these is missed:

```bash
tests/models/test_sync_twins.py
tests/models/test_no_payload_in_models.py
tests/core/test_no_unit_of_work.py
```

## Add an adapter surface

For a REST endpoint, add a route in `adapters/rest/routes/` and its schema; for an MCP tool,
add a function in `adapters/mcp/tools/`. Both do parameter binding, error mapping and
serialization, and call a controller — nothing below that. No model import, no payload key,
no second place that decides a flow.

## Testing at the right layer

| Layer | What a test constructs | What it must not do |
|---|---|---|
| `services/` | a payload mapping | touch a database |
| `models/` | a row from values | build a payload |
| `controllers/` | a flow with fakes | talk to GitHub |
| `adapters/` | a request and its parameters | read GitHub |

Run only the affected files locally. The suite binds models to a real PostgreSQL server, and
running the whole `tests/` directory takes minutes; CI runs that directory.

```bash
pytest tests/services/github/test_payloads.py tests/models/test_<entity>.py -q
```

## Common mistakes

* a GitHub field name appearing outside `services/github/payloads.py`;
* a model method taking a `Mapping` or named `*_from_remote`;
* a module-level helper function in `models/`;
* a `RawSQL` node, or SQL text concatenated from table names;
* a transaction, a write scope, or a post-commit task;
* a field called `total` / `count` for something only this service has.
