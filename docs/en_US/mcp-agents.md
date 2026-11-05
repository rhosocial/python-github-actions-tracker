# Connecting coding agents over MCP

the tracker speaks MCP over Streamable HTTP at `http://127.0.0.1:8001/mcp` (or
`https://<host>:8001/mcp` when TLS is in front), with a static Bearer key when
`GHAT_API_KEY` is set. This page is not the protocol reference — it is how
the four coding agents most people actually run manage MCP configuration, what
each one stores where, and the exact snippets that connect them to this service.

The four were chosen because they cover the shapes everything else copies:
Claude Code (project `.mcp.json` + CLI), OpenAI Codex (TOML), OpenCode (JSON
with OAuth built in), and Command Code (Claude-shaped, with an importer). If
your agent is not here, its configuration is very likely one of these four
shapes under a different name.

## The same server, four grammars

Everything below connects to one deployment: local, no key.

| Agent | Config file | Scope model | CLI |
|---|---|---|---|
| Claude Code | `.mcp.json` (project), `~/.claude.json` (user/local) | `local` / `project` / `user` | `claude mcp add/list/get/remove` |
| Codex | `~/.codex/config.toml`, project `.codex/config.toml` (trusted projects) | user / project | `codex mcp add/list/login` |
| OpenCode | `opencode.json(c)`, global or per project | global / per agent | `opencode mcp auth/list/logout` |
| Command Code | `.mcp.json` (project), `~/.commandcode/mcp.json` (user), `~/.commandcode/projects/<slug>/mcp.json` (local) | `local` / `project` / `user` | `cmd mcp add/list/get/remove/auth` |

All four speak Streamable HTTP; all four can carry the Bearer key in a header;
and all four have a menu or `list` command that shows whether the server
actually connected — run it before you prompt the model, because a config that
parses is not a server that answers.

## Claude Code

Claude Code stores servers in three scopes: `local` (default, this project,
this user), `project` (`.mcp.json` at the repository root, meant to be
committed), and `user` (all projects). The CLI writes the file for you:

```bash
# Local deployment, no API key configured:
claude mcp add --transport http github-actions-tracker http://127.0.0.1:8001/mcp

# TLS in front, API key set:
claude mcp add --transport http github-actions-tracker https://tracker.example.com:8001/mcp \
  --header "Authorization: Bearer <GHAT_API_KEY>"

# Share with the team through the repository:
claude mcp add --transport http github-actions-tracker http://127.0.0.1:8001/mcp --scope project
```

The same thing as committed JSON (`.mcp.json`):

```json
{
  "mcpServers": {
    "github-actions-tracker": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": { "Authorization": "Bearer ${GHAT_API_KEY}" }
    }
  }
}
```

Two things the reference documentation considers important enough to repeat:

- **A `url` without a `type` is read as a stdio server** and the entry is
  skipped with an error. `type: "http"` (or the alias `streamable-http`) must be
  spelled out — the protocol's own name is not inferred from the URL scheme.
- **A project `.mcp.json` needs approval**: a cloned repository cannot approve
  its own servers. Run `claude` in the folder, accept the trust dialog, and
  approve the server (or set `enableAllProjectMcpServers` in a settings file
  that is not committed). Until then `claude mcp list` shows `⏸ Pending
  approval` and the tools never load.

Verify with `claude mcp list` (`✔ Connected`) or `/mcp` inside a session, which
also shows the tool count — thirteen, at the time of writing.

## Codex

Codex keeps MCP servers in `~/.codex/config.toml`, with a project overlay at
`.codex/config.toml` (trusted projects only). The ChatGPT desktop app and the
IDE extension read the same file, so one configuration covers all three. The
transport is spelled per server: stdio takes `command` + `args`; Streamable HTTP
takes `url` and its authentication travels separately.

```toml
[mcp_servers.github-actions-tracker]
url = "http://127.0.0.1:8001/mcp"

# TLS in front, API key set:
[mcp_servers.github-actions-tracker]
url = "https://tracker.example.com:8001/mcp"
bearer_token_env_var = "GHAT_API_KEY"
tool_timeout_sec = 120
```

The CLI form (writes the same file):

```bash
codex mcp add github-actions-tracker --url http://127.0.0.1:8001/mcp
codex mcp list          # servers and their connection state
codex mcp login github-actions-tracker   # only for OAuth servers; this one uses a Bearer key
```

One `tool_timeout_sec` note that matters for this service specifically:
`wait_for_run` is a bounded long poll — a single call that legitimately runs for
tens of seconds. A per-server timeout in the 30-second range that is fine for
most tools will cut it off mid-wait. Either raise the timeout (120s covers the
wait ceiling) or skip that tool; the other tools answer in milliseconds.

## OpenCode

OpenCode reads `opencode.json(c)` — global, per project, and through an
organisation's `.well-known/opencode` defaults, which a local `enabled: true`
can opt into. Remote servers take `type: "remote"`; OpenCode detects OAuth
automatically, so a server that authenticates with a static key should say so
explicitly:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "github-actions-tracker": {
      "type": "remote",
      "url": "http://127.0.0.1:8001/mcp",
      "enabled": true,
      "oauth": false,
      "headers": { "Authorization": "Bearer {env:GHAT_API_KEY}" }
    }
  }
}
```

- `oauth: false` turns off OAuth auto-detection; without it, a 401 starts a
  browser flow instead of using the header. This service never starts one — the
  Bearer key *is* the authentication — so the flag only saves you a confusing
  browser window on a mistyped key.
- `{env:VAR}` is expanded at load time from the process environment.
- `timeout` covers requests and tool calls; the same `wait_for_run` note as
  Codex applies (raise it above the wait ceiling if you plan to use that tool).

Verify with `opencode mcp list`, or debug a failing server with
`opencode mcp debug github-actions-tracker`, which tests HTTP connectivity and prints the
auth state.

## Command Code

Command Code's MCP surface is the Claude Code shape with a different prefix:
`cmd mcp add --transport http <name> <url>`, scopes `local` (default, per
project), `project` (`.mcp.json`, version controlled) and `user`
(`~/.commandcode/mcp.json`), and `local` overriding `project` overriding `user`
when the same name appears twice.

```bash
cmd mcp add --transport http github-actions-tracker http://127.0.0.1:8001/mcp

cmd mcp add --transport http github-actions-tracker https://tracker.example.com:8001/mcp \
  --header "Authorization: Bearer <GHAT_API_KEY>"
```

As JSON (the schema uses `transport`, with `type` accepted as an alias, so a
Claude Code `.mcp.json` block pastes in unchanged):

```json
{
  "mcpServers": {
    "github-actions-tracker": {
      "transport": "http",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": { "Authorization": "Bearer ${GHAT_API_KEY}" }
    }
  }
}
```

`${VAR}` and `${VAR:-default}` in `headers` and `env` are resolved at runtime; a
variable that is unset and has no default stops that server with an error naming
the variable. Verify with `cmd mcp list` or the `/mcp` menu (green means
connected, and the tool count is shown).

Two conveniences worth knowing: `/import` pulls MCP servers from other agents
(`/import claude`, `/import codex`, `/import opencode`, ...), so an existing
setup migrates in one command; and its tools are prefixed
`mcp__tracker__<tool>`, which matters if you write rules that reference them.

## Choosing where the configuration lives

The scope question is the same everywhere, and the answer is the same too:

- **A repository's `.mcp.json` / `.codex/config.toml` is a shared file.** Put
  the deployment's *address* in it — that is useful to the whole team. Do not
  put a key in it; every one of these agents can read the key from the
  environment (`${GHAT_API_KEY}`, `{env:...}`,
  `bearer_token_env_var`), and the environment is where a secret belongs.
- **A user-scope entry is for a personal deployment.** A laptop-local
  the tracker with no API key is a user-scope or local-scope server; the project
  file would publish `127.0.0.1` addresses to teammates whose deployment is
  elsewhere.
- **Approval gates are per machine.** Claude Code's pending-approval state and
  Codex's trusted-projects rule both exist so a cloned repository cannot arm
  tools without the operator looking at them. That is the reason the approval
  step cannot be scripted past, and it should stay that way.

## After connecting

Run `/mcp` (Claude Code, Command Code) or the agent's `list` command and check
the tool count before prompting. Then the first question to ask is the cheap
one: `ci_status` — it exercises the database connection, reports `build.revision`
against the image that was pulled, and returns the pool gauges, without spending
a GitHub API call. `find_runs --repository <repo>` is the first question that
does spend one.

The thirteen tools and what each answers are documented in
[the API page](api.md). Two integration notes that are not obvious from the
tool list:

- `wait_for_run` holds one database connection for its whole wait (see
  [the pool rules](operations.md)) and is a bounded long poll — per-agent tool
  timeouts should clear 60 seconds for it (Codex: `tool_timeout_sec`; OpenCode:
  `timeout`; Claude Code: the per-server `timeout` field).
- Everything the tools return is untrusted text. The MCP responses carry
  `untrusted: true` and a hint saying not to follow instructions found inside —
  the same rule a good agent applies on its own.
