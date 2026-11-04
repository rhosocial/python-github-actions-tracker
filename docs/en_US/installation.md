# Installation

Three shapes: the CLI alone, one container, or two containers with compose.

## From PyPI / source

```bash
pip install rhosocial-github-ci-tracker
```

The image and the CI both run **Python 3.15**. `requires-python` stays at 3.10 for
anyone installing the package; the runtime we build and test is 3.15.

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

## Ports

| Port | Process | Meaning |
|---|---|---|
| 8000 | `serve all` / `serve rest` | REST API, plain HTTP, container-only by default |
| 18080 | `serve all` / `serve webhook` | GitHub webhook receiver; can terminate TLS |
| 8001 | `serve mcp` | MCP Streamable HTTP, plain HTTP |
