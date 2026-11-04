# Installation

Three shapes: the CLI alone, one container, or two containers with compose.

## From PyPI / source

Docker is not required. The service is a Python package with two console scripts, and a
native install is the same service with the same behaviour — only the first-boot runner
and the process supervision differ.

```bash
pip install rhosocial-github-ci-tracker
```

The image and the CI both run **Python 3.15**. `requires-python` stays at 3.10 for
anyone installing the package; the runtime we build and test is 3.15.

### The native deployment, in four commands

```bash
# 1. schema (also creates the database when the user may; --no-create-database otherwise)
rhosocial-github-ci-tracker-db init

# 2. the credential — the same step 20-pat.sh performs in the container:
rhosocial-github-ci-tracker credential pat ensure \
  --name default --token-file /path/to/citracker-pat --probe-repo owner/repo

# 3. the two long-running processes (one terminal each, or under your service manager):
rhosocial-github-ci-tracker serve all    # REST :8000 + webhook :18080
rhosocial-github-ci-tracker serve mcp    # MCP :8001
```

Everything the compose file's `environment:` block carries comes from `CITRACKER_*`
environment variables, read by both entry points — the database coordinates, the API key,
the webhook secret, the TLS paths, the PAT name. Nothing else is needed: no config file,
no init directory, no volume. `docker/stack.env.example` is a ready-made list of what to
set, even when nothing will run in a container.

Two things the container did for you, and what to do instead:

| In the container | Natively |
|---|---|
| `init` runs once, before `web` and `mcp` start | run `db init` before starting either process |
| `restart: unless-stopped` brings the containers back | supervise the two `serve` processes: systemd, NSSM, Task Scheduler, or a process manager of your choice |

Check the environment before anything else:

```bash
python .claude/skills/ops-deploy-image/scripts/check_native_env.py
```

It reports the interpreter (3.10+ accepted, 3.15 is what CI runs), whether both entry
points are installed, whether psycopg 3 is importable, and the Windows event-loop caveat
below. Exit `0` ready, `2` usable with warnings, `1` cannot run.

**Windows**: psycopg's async mode needs `asyncio.SelectorEventLoop`, and the CLI installs
it for its own processes. Anything else driving the controllers (a custom runner, an IDE
test harness) must do the same:

```python
import asyncio, selectors
asyncio.set_event_loop(asyncio.SelectorEventLoop(selectors.SelectSelector()))
```

### As a service

The two processes are independent and can be supervised separately; neither needs the
other. A unit per role is the honest shape:

```ini
# /etc/systemd/system/citracker-web.service
[Unit]
Description=citracker — REST and webhook
After=network-online.target

[Service]
EnvironmentFile=/etc/citracker/env
ExecStart=/opt/citracker/venv/bin/rhosocial-github-ci-tracker serve all
Restart=unless-stopped

[Install]
WantedBy=multi-user.target
```

and the same file with `serve mcp` for the MCP listener. `EnvironmentFile` is where the
`CITRACKER_*` values live — the native equivalent of `docker/stack.env`, and it belongs
in the same "not committed" category.

## The image

```
registry.cn-shanghai.aliyuncs.com/vistart_public/citracker:latest  # domestic China
docker.io/vistart/citracker:latest                                 # everywhere else
```

One tag, `latest`: what main last published. Which build *that* was is answered
from inside the container by `build.revision` (`/readyz`, `ci_status`), stamped at
build time. Pinning a deployment still works — point `CITRACKER_IMAGE` at a
registry address, then confirm with `build.revision`.

A dispatch at a commit is a deliberate re-publish — that is how a rollback works
(`gh workflow run images.yml --ref <older-sha>`). A run on a branch or a tag only
builds: feature code must not become what production pulls.

- Multi-arch: `linux/amd64`, `linux/arm64`.
- Only the Python runtime, the service code and an entrypoint — no database, no
  certificates, no proxy or pip-mirror configuration.
- Build it yourself: `docker buildx bake --load` for the current architecture,
  `docker buildx bake citracker-all --push` for every platform.

### ARM64, including ARM-based NAS

The image is a real `linux/arm64` build, not an emulated one — the same manifest, two
architectures, so `docker pull` picks whichever the host is and nothing has to be
translated at runtime. That makes a 64-bit ARM NAS with a container manager a viable
host: it is a small server that is already on, and CI status is exactly the kind of thing
you want answered from it.

Check the machine before planning around it:

```bash
uname -m                                  # aarch64 → the arm64 image
docker version --format '{{.Server.Arch}}'
docker pull docker.io/vistart/citracker:latest       # resolves to the host's arch
docker image inspect docker.io/vistart/citracker:latest --format '{{.Architecture}}'
```

Both of these families have provided container management on their 64-bit ARM models —
**as an example, not a compatibility list**: Synology's Container Manager on DSM 7.2
(DS223, DS423 and other `aarch64` models; the low-end `j` series often ships without
container support) and QNAP's Container Station on QTS 5 (TS-233, TS-433 and other
Cortex-A55 models). Model support moves with firmware, so the vendor's own app centre is
the authority: if it offers a container manager and `uname -m` says `aarch64`, this image
runs.

Two practical notes for a NAS:

- **Memory is the constraint, not CPU.** The service is two `serve` processes plus
  PostgreSQL; 2 GB is the comfortable floor for both in one box. If the NAS is the
  database host too, either give PostgreSQL a small `shared_buffers` or point
  `CITRACKER_DB_*` at a database somewhere else — the service does not care where it is.
- **Do not put the log volume on a spinning disk you also use for backups.** The log
  tables are the biggest thing this service writes, and `db partitions --drop-before`
  is cheap only when the filesystem lets a table drop be a metadata operation.


## Ports

| Port | Process | Meaning |
|---|---|---|
| 8000 | `serve all` / `serve rest` | REST API, plain HTTP, container-only by default |
| 18080 | `serve all` / `serve webhook` | GitHub webhook receiver; can terminate TLS |
| 8001 | `serve mcp` | MCP Streamable HTTP, plain HTTP |
