# Architecture

This project uses a Service-MVC-App shape. The dependency direction is one-way:
App → Controller → Service → Model. Adapters belong to the App layer and do
argument binding, authentication, serialization and error mapping; `container.py`
does the wiring.

```
App (adapters / container.py)    argument binding, auth, serialization, error mapping, wiring
      ↓
Controller (the C of MVC)         one flow: trigger, query, subscribe, deliver, credentials
      ↓
Service                          GitHub capabilities: protocol + implementation
      ↓
Model (the M of MVC)              each model owns its data behaviour (ActiveRecord)
      ↑
PostgreSQL
```

The three responsibilities:

- **Service** wraps a callable capability — today, the GitHub API. It defines no
  flow and writes no row. A new capability adds a protocol and an implementation;
  it never copies an existing flow.
- **MVC** puts data behaviour and validation in Model, flow sequencing in
  Controller, and output shape in `views.py`. The rules live in Model, so the
  Controller stays thin.
- **App** does wiring and protocol adaptation. `container.py` builds settings,
  the database connection and the controllers once, and the adapters share that
  container.

The project also follows **fat models, thin controllers**:

- a Model owns the table mapping, relations, state machine and row validation.
  One `save()` is one complete write. There is no unit of work and no
  cross-model transaction;
- a Controller only decides what to read, which capability to call and which row
  to write. It does not re-implement data rules;
- a Service only answers how a capability is called. It decides no flow and
  knows no model.

`config.py`, `database.py`, `container.py` and `views.py` sit at the package root;
`adapters/` holds one App per protocol.

See [Design decisions](design.md) for why these shapes were chosen, and
[Extending the service](extending.md) for how to add to them.
