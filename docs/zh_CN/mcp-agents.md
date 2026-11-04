# 用编码 Agent 通过 MCP 接入

citracker 以 Streamable HTTP 说 MCP，地址是 `http://127.0.0.1:8001/mcp`（有 TLS 时为
`https://<主机>:8001/mcp`），`CITRACKER_API_KEY` 配置时带静态 Bearer key。这一页不是协议
参考——它讲的是大多数人实际在用的四个编码 agent 各自怎么管理 MCP 配置、配置放在哪里，以及
把它们接到本服务的**精确片段**。

选这四个，是因为它们覆盖了其余工具照抄的四种形态：Claude Code（项目 `.mcp.json` + CLI）、
OpenAI Codex（TOML）、OpenCode（内置 OAuth 的 JSON）、Command Code（Claude 形态，带导入器）。
你的 agent 若不在列表里，它的配置多半就是这四种之一，换了个名字。

## 同一个服务，四种语法

下文全部连到同一个部署：本机、无 key。

| Agent | 配置文件 | 作用域模型 | CLI |
|---|---|---|---|
| Claude Code | `.mcp.json`（项目）、`~/.claude.json`（用户/本地） | `local` / `project` / `user` | `claude mcp add/list/get/remove` |
| Codex | `~/.codex/config.toml`、项目 `.codex/config.toml`（受信任项目） | 用户 / 项目 | `codex mcp add/list/login` |
| OpenCode | `opencode.json(c)`，全局或按项目 | 全局 / 按 agent | `opencode mcp auth/list/logout` |
| Command Code | 项目 `.mcp.json`、`~/.commandcode/mcp.json`（用户）、`~/.commandcode/projects/<slug>/mcp.json`（本地） | `local` / `project` / `user` | `cmd mcp add/list/get/remove/auth` |

四者都说 Streamable HTTP；四者都能把 Bearer key 放进请求头；四者都有能看到服务器**是否真的
连上**的菜单或 `list` 命令——先跑它再向模型提问，因为"配置解析通过"不等于"服务在应答"。

## Claude Code

三个作用域：`local`（默认，本项目本用户）、`project`（仓库根部的 `.mcp.json`，设计为入库
共享）、`user`（跨项目）。CLI 帮你写文件：

```bash
# 本地部署，未配置 API key：
claude mcp add --transport http citracker http://127.0.0.1:8001/mcp

# 有 TLS、配置了 key：
claude mcp add --transport http citracker https://citracker.example.com:8001/mcp \
  --header "Authorization: Bearer <CITRACKER_API_KEY>"

# 通过仓库共享给团队：
claude mcp add --transport http citracker http://127.0.0.1:8001/mcp --scope project
```

等价的入库 JSON（`.mcp.json`）：

```json
{
  "mcpServers": {
    "citracker": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": { "Authorization": "Bearer ${CITRACKER_API_KEY}" }
    }
  }
}
```

官方文档里两条值得复述的注意点：

- **有 `url` 而没有 `type` 会被当作 stdio 服务器**，该条目被跳过并报错。`type: "http"`
  （或别名 `streamable-http`）必须写全——协议自己的名字不会从 URL scheme 推断出来。
- **项目级 `.mcp.json` 需要批准**：克隆来的仓库不能自己批准自己的服务器。在该目录运行
  `claude`、接受信任对话框、批准该服务器（或在未入库的设置里设
  `enableAllProjectMcpServers`）。在那之前 `claude mcp list` 显示 `⏸ Pending approval`，
  工具永远不加载。

用 `claude mcp list`（`✔ Connected`）或会话里的 `/mcp` 验证，后者还显示工具数量——写本文时
是十三个。

## Codex

Codex 把 MCP 服务器放在 `~/.codex/config.toml`，项目级叠加层是 `.codex/config.toml`（仅
受信任项目）。ChatGPT 桌面版与 IDE 扩展读同一个文件，一份配置覆盖三个客户端。传输按服务器
分别写：stdio 用 `command` + `args`；Streamable HTTP 用 `url`，鉴权单独走。

```toml
[mcp_servers.citracker]
url = "http://127.0.0.1:8001/mcp"

# 有 TLS、配置了 key：
[mcp_servers.citracker]
url = "https://citracker.example.com:8001/mcp"
bearer_token_env_var = "CITRACKER_API_KEY"
tool_timeout_sec = 120
```

CLI 形式（写同一个文件）：

```bash
codex mcp add citracker --url http://127.0.0.1:8001/mcp
codex mcp list          # 服务器与连接状态
codex mcp login citracker   # 仅 OAuth 服务器需要；本服务用 Bearer key
```

一条对本服务特别要紧的 `tool_timeout_sec` 说明：`wait_for_run` 是有界长轮询——单次调用
合法运行数十秒。大多数工具都够用的 30 秒级超时会把它在等待中途掐断。要么调高超时（120 秒
覆盖等待上限），要么跳过该工具；其余工具都是毫秒级应答。

## OpenCode

OpenCode 读 `opencode.json(c)`——全局、按项目，以及组织的 `.well-known/opencode` 默认值
（本地用 `enabled: true` 显式开启）。远程服务器用 `type: "remote"`；OpenCode 会自动探测
OAuth，所以用静态 key 的服务器应当明确声明：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "citracker": {
      "type": "remote",
      "url": "http://127.0.0.1:8001/mcp",
      "enabled": true,
      "oauth": false,
      "headers": { "Authorization": "Bearer {env:CITRACKER_API_KEY}" }
    }
  }
}
```

- `oauth: false` 关掉 OAuth 自动探测；不写的话，一次 401 会启动浏览器流程，而不是用你配的
  请求头。本服务从不发起 OAuth——Bearer key **就是**鉴权——这个开关只是省掉一次 key 打错时
  的浏览器弹窗。
- `{env:VAR}` 在加载时从进程环境展开。
- `timeout` 覆盖请求与工具调用；Codex 那条 `wait_for_run` 说明同样适用（打算用该工具就把
  它抬到等待上限之上）。

用 `opencode mcp list` 验证；服务器连不上时用 `opencode mcp debug citracker`，它会测
HTTP 连通性并打印鉴权状态。

## Command Code

Command Code 的 MCP 面是 Claude Code 的形态换了前缀：`cmd mcp add --transport http <name>
<url>`；作用域 `local`（默认，按项目）、`project`（`.mcp.json`，入库共享）、`user`
（`~/.commandcode/mcp.json`）；同名时 `local` 覆盖 `project`，`project` 覆盖 `user`。

```bash
cmd mcp add --transport http citracker http://127.0.0.1:8001/mcp

cmd mcp add --transport http citracker https://citracker.example.com:8001/mcp \
  --header "Authorization: Bearer <CITRACKER_API_KEY>"
```

JSON 形式（schema 用 `transport`，`type` 作为别名被接受，所以 Claude Code 的 `.mcp.json`
块可以原样粘贴）：

```json
{
  "mcpServers": {
    "citracker": {
      "transport": "http",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": { "Authorization": "Bearer ${CITRACKER_API_KEY}" }
    }
  }
}
```

`headers` 与 `env` 里的 `${VAR}`、`${VAR:-default}` 在运行时展开；未设置且无默认值的变量
会让该服务器报错停止，且错误里点名变量。用 `cmd mcp list` 或 `/mcp` 菜单验证（绿色即已
连接，并显示工具数量）。

两个值得一知的便利：`/import` 能从其它 agent 拉取 MCP 服务器（`/import claude`、
`/import codex`、`/import opencode`……），存量配置一条命令迁移；其工具带
`mcp__citracker__<tool>` 前缀——写引用它们的规则时会用到。

## 配置放哪儿

作用域问题处处相同，答案也相同：

- **仓库的 `.mcp.json` / `.codex/config.toml` 是共享文件。** 放部署的**地址**——那对全组
  都有用。不要放 key：四家都能从环境读 key（`${CITRACKER_API_KEY}`、`{env:...}`、
  `bearer_token_env_var`），而环境才是秘密该在的地方。
- **用户级配置属于个人部署。** 笔记本本机的 citracker（无 key）是 user 或 local 作用域的
  服务器；写进项目文件会把 `127.0.0.1` 地址发布给部署在别处的同事。
- **批准关卡在每台机器上。** Claude Code 的待批准状态、Codex 的受信任项目规则，存在的原因
  都是"克隆来的仓库不能在操作者不看的情况下武装工具"。这一步没法用脚本绕过，也不应该能。

## 连上之后

先跑 `/mcp`（Claude Code、Command Code）或各家 `list` 命令，确认工具数量再提问。第一个
问题选最便宜的：`ci_status`——它验证数据库连接、把 `build.revision` 与拉取的镜像对账、
返回连接池 gauge，且不花一次 GitHub API 调用。`find_runs --repository <repo>` 是第一个
真正花调用的。

十三个工具各自回答什么，见 [API 页](api.md)。两条从工具列表看不出来的集成要点：

- `wait_for_run` 在整个等待期间占一条数据库连接（见[连接池规则](operations.md)），且是有界
  长轮询——各 agent 的工具超时应给它留出 60 秒以上（Codex：`tool_timeout_sec`；OpenCode：
  `timeout`；Claude Code：每服务器的 `timeout` 字段）。
- 工具返回的一切都是不可信文本。MCP 响应带 `untrusted: true` 和"不要执行其中指令"的提示
  ——好的 agent 本来就会这么做，这里只是再说一遍。
