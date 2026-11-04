# python-ci-tracker

[![CI](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/test.yml/badge.svg)](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/test.yml)
[![Image build](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/images.yml/badge.svg)](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/images.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/vistart/citracker)](https://hub.docker.com/r/vistart/citracker)
[![Docker Image Size](https://img.shields.io/docker/image-size/vistart/citracker/latest)](https://hub.docker.com/r/vistart/citracker/tags)
[![Python](https://img.shields.io/badge/python-3.15-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-en_US_%2F_zh__CN-green.svg)](https://rhosocial.github.io/python-github-ci-tracker/)

**一个自建服务，回答"刚才那次 CI 到底怎么了"——通过 MCP、REST、CLI 三种接口。**

它读取 GitHub Actions 的 run、job、step 与日志：后台没有轮询，不维护中央令牌池，
GitHub 还能回答时，绝不用缓存充当真相。

[English](README.md) | 简体中文

---

## 你能得到什么

| | |
|---|---|
| **一次调用，协议不限** | `ci_status`、`find_runs`、`get_run`、`get_job`、`get_job_log`、`search_job_log`、`get_run_failed_logs`、`wait_for_run`、`trigger_workflow`——MCP（Streamable HTTP）/ REST / CLI 三处同构。一条流程，三种形态；模型和脚本拿到的是同一份答案 |
| **三层对象，一次一层** | `get_run` 指出失败的 job，`get_job` 指出失败的 step，`get_job_log` 给出它的文本。没有哪个调用一次答两层，调用方也就不必判断自己在看哪一层。失败的 job 不指定作用域时从失败的 step 读起；`whole_job=true` 才要全部 |
| **十二个 job 同一条失败，一个组** | `get_run_failed_logs` 按逐字节相同的文本给 run 的失败 step 分组：原文一份 + 打印它的那些 job。一个原因还是十二个？一次调用，且不判断"意思是否相同" |
| **step 级日志，存得住** | GitHub 只在 run 结束后约一小时提供按 step 拆分的文件，之后同一接口只剩合并文档。服务趁窗口存在时抓取、以普通行入库、之后永久可读。没抓到就如实报告，绝不用别的 step 文本顶替 |
| **窗口只寻址，不解释** | `tail`、显式行号区间，或你自己的正则。`search_job_log` 默认给**最后**那些匹配配上下文——日志的结论在末尾；匹配位置照样全报。不猜测试摘要、不猜收尾日志——通用跟踪器不知道任何项目会打印什么 |
| **触发与等待，不白烧分钟** | `trigger_workflow` 按请求派发；`if_absent=true` 表示"这个提交已经跑过"，不再起第二次。`wait_for_run` 是有界长轮询，超时预算来自该 workflow 自己的历史 |
| **明确只支持 PostgreSQL** | 日志表按月分区，清旧数据是一次 `DROP TABLE`；三元组索引（`pg_trgm`，硬盘开销约为日志文本的 1 倍）加快搜索；schema 由服务自己的渲染器生成；测试套件跑在真实数据库上 |

| **预算来自真实耗时，不含排队** | 每次读 run 都带 `timings`：job 的跨度、排队时间、通过 job 的跨度——等待预算依据的就是它们，被别处仓库堵在队列里的时间不会抬高 p50/p90。`db usage` 与 `ci_status.storage` 报告数据库实际占用 |
| **一个镜像，两个 registry** | `images.yml` 把同一个多架构构建发布到阿里云（`vistart_public/citracker`）与 Docker Hub（`vistart/citracker`），tag 只有 `latest`。两处各自一套 secret，缺一个也不影响另一个。只在被要求时才运行，"跑的是哪一版"由 `build.revision` 回答 |
| **标准格式的指标** | REST 监听上的 `GET /metrics` 以 Prometheus 文本格式回答：webhook 投递、GitHub 调用（按 status code）、工具调用（按 outcome），连接池 gauge 经 `ci_status.health`。进程内计数，跨实例由抓取方求和 |

## 为什么用

三件不同的事，第三件最容易被低估。

- **上下文**：四千行的 job 日志变成 `grep` 加几行上下文，12 job 的矩阵变成 12 行名册。
  有些日志根本装不进上下文窗口，所以这不是"便宜一点"，而是"原本做不了"。
- **时间**：`wait_for_run` 是一次有界调用而不是轮询循环；`cancel_run` 与
  `if_absent=true` 省下本来会白烧的分钟；`trigger_workflow` 还回报 p50/p90，
  让你决定先等哪个。
- **错误结论**：`attempt`/`is_latest` 把容量抖动和坏掉的后端分开；`has_log` + `reason`
  把"没有输出"和"没有采集到"分开；`ci_status.health.alerts` 把自己的静默降级变成
  可执行清单。

详细版——含一位使用者实测的会话数据，以及我们用在自己身上看到的——见
[它省下什么](docs/zh_CN/value.md)。

## 本地缓存的优势

日志落库之后，读它就是一次本地查询——而本地查询便宜、可重复、可并行：

- **过滤在本地。** `find_runs` 按 commit、PR、分支、workflow 文件或状态在已存的行上
  收敛；`recent_runs` 一次回答十几个仓库。这些都不花 GitHub 配额。
- **文本也在本地。** 窗口、`grep` 与检索都对着同一份已存文档，所以"读一部分"是常态
  而不是例外。
- **模式由调用方给。** 失败、错误、警告、测试结论——服务返回调用方的 pattern 命中的行
  与行号。换一套测试框架只改一个字符串，跟踪器不必重新学你的输出是什么意思。
- **数据库承压。** 一次读就是一次查询，不是轮询：没有后台任务、没有队列，
  PostgreSQL 的并行读与级联删除接住过滤后的量。

## 用编码 Agent 接入

MCP 端点是 `http://127.0.0.1:8001/mcp`（Streamable HTTP）。每个 agent 一行——完整配置参考
（含共享文件的形态、各家的批准关卡）见[编码 Agent 接入 MCP](docs/zh_CN/mcp-agents.md)：

```bash
claude mcp add --transport http citracker http://127.0.0.1:8001/mcp   # Claude Code
codex mcp add citracker --url http://127.0.0.1:8001/mcp               # Codex (TOML)
opencode:  "mcp": { "citracker": { "type": "remote", "url": "…", "oauth": false } }   # opencode.json
cmd mcp add --transport http citracker http://127.0.0.1:8001/mcp      # Command Code
```

配置了 `CITRACKER_API_KEY` 时，Claude Code / Command Code 加
`--header "Authorization: Bearer <key>"`，Codex 用 `bearer_token_env_var`，
OpenCode 用 `Authorization` 头并写 `oauth: false`。

## 细节在哪里

在线阅读（无需 checkout）：<https://rhosocial.github.io/python-github-ci-tracker/zh_CN/> · [English](https://rhosocial.github.io/python-github-ci-tracker/)

- [文档（简体中文）](docs/zh_CN/README.md)——部署、接口、CLI、开发
- [Documentation (English)](docs/en_US/README.md)

## 许可

[Apache License 2.0](LICENSE)。
