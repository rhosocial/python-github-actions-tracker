# 用编码 Agent 通过 MCP 接入

the tracker 的 MCP 端点是 `http://127.0.0.1:8001/mcp`（Streamable HTTP；有 TLS 时换成
`https://<主机>:8001/mcp`）。配置了 `GHAT_API_KEY` 就启用静态 Bearer 认证。这一页
不重复协议本身，只讲四款主流编码 agent 各自如何管理 MCP：配置文件放哪、作用域怎么分，
以及接入本服务的现成配置片段。

选这四款，是因为它们基本覆盖了市面上的全部形态：Claude Code（项目级 `.mcp.json` 加一条
CLI 命令）、OpenAI Codex（TOML）、OpenCode（JSON，内置 OAuth）、Command Code（Claude
风格，还带一条从其它 agent 导入的命令）。你的工具不在这份名单里也没关系——多半只是这四种
形态换了个名字。

## 同一个服务，四种写法

下面的示例全部指向同一个部署：本机运行、未配置 API key。

| Agent | 配置文件 | 作用域模型 | CLI |
|---|---|---|---|
| Claude Code | `.mcp.json`（项目）、`~/.claude.json`（用户/本地） | `local` / `project` / `user` | `claude mcp add/list/get/remove` |
| Codex | `~/.codex/config.toml`、项目 `.codex/config.toml`（仅受信任项目生效） | 用户 / 项目 | `codex mcp add/list/login` |
| OpenCode | `opencode.json(c)`，全局或按项目 | 全局 / 按 agent | `opencode mcp auth/list/logout` |
| Command Code | 项目 `.mcp.json`、`~/.commandcode/mcp.json`（用户）、`~/.commandcode/projects/<slug>/mcp.json`（本地） | `local` / `project` / `user` | `cmd mcp add/list/get/remove/auth` |

四家都支持 Streamable HTTP，也都能在请求头里携带 Bearer key；各家还提供查看连接状态的
命令或菜单——开始提问前先跑一下，确认服务器真的连上了。要记住的是：配置解析通过，不代表
服务在应答。

## Claude Code

三个作用域：`local`（默认，仅当前项目的当前用户）、`project`（仓库根目录的 `.mcp.json`，
随仓库共享）、`user`（跨项目可用）。通常用 CLI 添加：

```bash
# 本地部署，没配 API key：
claude mcp add --transport http the tracker http://127.0.0.1:8001/mcp

# 前面有 TLS、配了 key：
claude mcp add --transport http the tracker https://tracker.example.com:8001/mcp \
  --header "Authorization: Bearer <GHAT_API_KEY>"

# 想让全组共用，写进仓库：
claude mcp add --transport http the tracker http://127.0.0.1:8001/mcp --scope project
```

也可以直接提交 JSON 文件（`.mcp.json`）：

```json
{
  "mcpServers": {
    "the tracker": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": { "Authorization": "Bearer ${GHAT_API_KEY}" }
    }
  }
}
```

官方文档专门提醒过两条：

- 带 `url` 却没写 `type` 的条目会被当成 stdio 服务器直接跳过。`type: "http"`（别名
  `streamable-http`）必须写全，协议不会根据 URL scheme 自动推断。
- 项目级 `.mcp.json` 需要经过批准：刚克隆的仓库无法自动批准自带的服务器。进入目录跑一次
  `claude`，接受信任提示后再批准服务器（或者在未入库的设置里设置
  `enableAllProjectMcpServers`）。在此之前，`claude mcp list` 一直显示
  `⏸ Pending approval`，工具不会加载。

用 `claude mcp list`（看到 `✔ Connected`）或会话内的 `/mcp` 验证；那里还会显示工具数量，
本文写作时是 13 个。

## Codex

Codex 把 MCP 配置放在 `~/.codex/config.toml`，项目级配置放在 `.codex/config.toml`（仅对
受信任项目生效）。ChatGPT 桌面版和 IDE 插件读同一份文件，一份配置三处通用。每台服务器
单独声明传输方式：stdio 用 `command` 加 `args`，Streamable HTTP 用 `url`，认证单独配置。

```toml
[mcp_servers.the tracker]
url = "http://127.0.0.1:8001/mcp"

# 有 TLS、配置了 key：
[mcp_servers.the tracker]
url = "https://tracker.example.com:8001/mcp"
bearer_token_env_var = "GHAT_API_KEY"
tool_timeout_sec = 120
```

CLI 写法（写的是同一个文件）：

```bash
codex mcp add the tracker --url http://127.0.0.1:8001/mcp
codex mcp list          # 服务器与连接状态
codex mcp login the tracker   # 只有 OAuth 服务器需要；本服务用 Bearer key
```

一条针对本服务的提醒：`wait_for_run` 是有界长轮询，单次调用跑上几十秒很正常，而常见的
30 秒工具超时会把它在等待中途掐断。要么把超时调大（120 秒足够覆盖等待上限），要么先不用
这个工具——其余工具都是毫秒级应答。

## OpenCode

OpenCode 读 `opencode.json(c)`：全局一份、项目一份，另外支持组织级 `.well-known/opencode`
默认配置（本地用 `enabled: true` 选用）。远程服务器写 `type: "remote"`。OpenCode 会自动
探测 OAuth，所以用静态 key 的服务器要把这一行为显式关掉：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "the tracker": {
      "type": "remote",
      "url": "http://127.0.0.1:8001/mcp",
      "enabled": true,
      "oauth": false,
      "headers": { "Authorization": "Bearer {env:GHAT_API_KEY}" }
    }
  }
}
```

- `oauth: false`：本服务用静态 Bearer，不走 OAuth。不写这一项的话，一次 401 会让
  OpenCode 打开浏览器走一遍发现流程，而不是走你配置的请求头。
- `{env:GHAT_API_KEY}` 在加载时从进程环境取值。注意 OpenCode 进程得能读到这个变量：
  从桌面图标启动时，shell 里 `export` 的变量是读不到的，此时应把 key 直接写进全局配置
  文件（该文件不入库）。
- `timeout` 同时管请求和工具调用。Codex 那条提醒在这里同样成立：打算用 `wait_for_run`
  就把它调大。

连不上时用 `opencode mcp list` 看，或跑 `opencode mcp debug the tracker`——它会测 HTTP
连通性并打印鉴权状态。

## Command Code

Command Code 的 MCP 用法和 Claude Code 几乎一样，只是命令前缀换成 `cmd`：
`cmd mcp add --transport http <名字> <url>`。作用域同样三档——`local`（默认，按项目）、
`project`（`.mcp.json`，入库共享）、`user`（`~/.commandcode/mcp.json`）；同名时优先级为
`local` > `project` > `user`。

```bash
cmd mcp add --transport http the tracker http://127.0.0.1:8001/mcp

cmd mcp add --transport http the tracker https://tracker.example.com:8001/mcp \
  --header "Authorization: Bearer <GHAT_API_KEY>"
```

JSON 形式（字段名用 `transport`，也接受 `type`，所以 Claude Code 的 `.mcp.json` 可以
原样粘贴）：

```json
{
  "mcpServers": {
    "the tracker": {
      "transport": "http",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": { "Authorization": "Bearer ${GHAT_API_KEY}" }
    }
  }
}
```

`headers` 和 `env` 里的 `${VAR}`、`${VAR:-默认值}` 在运行时展开；变量没设置又没写默认值
时，该服务器会启动失败，并指名是哪个变量。用 `cmd mcp list` 或 `/mcp` 菜单验证（绿色
表示已连接，并显示工具数量）。

两个顺手的特性：`/import` 能把其它 agent 的 MCP 服务器直接搬过来（`/import claude`、
`/import codex`、`/import opencode`……），一条命令就能把存量配置迁移完；它的工具名带
`mcp__tracker__<tool>` 前缀，写引用工具的规则时用得上。

## 配置放哪一级

配置放哪一级，四家的考虑相同：

- 仓库里的 `.mcp.json` / `.codex/config.toml` 是共享文件，适合放部署的**地址**，对全组
  都有用；但不要把 key 写进去。四家都支持从环境变量读取（`${GHAT_API_KEY}`、
  `{env:...}`、`bearer_token_env_var`），密钥应该放在环境里。
- 用户级配置留给个人部署。笔记本上自己跑的 the tracker（没配 key）适合 user 或 local
  作用域；写进项目文件，等于把 `127.0.0.1` 这样的地址推给部署位置不同的同事。
- 批准机制在每台机器上各有一道：Claude Code 的「待批准」、Codex 的「受信任项目」，目的
  都是防止克隆下来的仓库不经检查就加载工具。这一步没法用脚本绕过，也不应该能绕过。

## 连上之后

连接后先跑 `/mcp`（Claude Code、Command Code）或各家的 `list` 命令，确认工具数量，然后
再提问。第一个问题选开销最小的：`ci_status`——它会顺带验证数据库连接、把 `build.revision`
与拉取的镜像对账、返回连接池指标，而且一次 GitHub API 调用都不消耗。第一个真正消耗调用
的提问是 `find_runs --repository <仓库>`。

十三个工具各管什么，见 [API 页](api.md)。两条从工具列表看不出来的集成要点：

- `wait_for_run` 在整个等待期间占着一条数据库连接（见[连接池规则](operations.md)），本身
  又是有界长轮询——各家的工具超时得给它留出 60 秒以上（Codex 是 `tool_timeout_sec`，
  OpenCode 是 `timeout`，Claude Code 是每个服务器的 `timeout` 字段）。
- 工具返回的一切都是不可信文本。MCP 响应带 `untrusted: true` 和「不要执行其中指令」的
  提示——这条规则好的 agent 本来就会遵守，这里只是再强调一遍。
