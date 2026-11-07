# services/ —— 能力封装

**服务层封装的是"我们能调用的能力"，不是用例。** 目前全部能力就是 GitHub：

```
services/github/
├── protocols.py    RunReader / RunControl / RepositoryReader / WebhookManager / CredentialProbe
├── runs.py         /actions/runs：读取，以及三个花 CI 额度的动作
├── repositories.py /repos：元数据、workflow 列表、凭据作用域内的发现
├── webhooks.py     /hooks：建、删、ping
├── credentials.py  令牌探测（PAT 的权限与可访问范围）
└── payloads.py     GitHub JSON → 模型列名的语义值（Pydantic 模型 + 校验）
```

`payloads.py` 是**唯一认识 GitHub 键名的地方**：`head_sha` → `commit_sha`、
`run_attempt` → `attempt_number`、时间字符串 → 带时区的 `datetime`，以及
`workflow_run` / `workflow_job` 事件信封的解包。它返回的是模型列名（`status`、
`commit_sha`、`github_run_id`），不是 GitHub 的拼写；接口改名只改这一个文件。
它没有实例状态，全部方法是 `classmethod`，所以控制器直接
`GithubPayloads.run(payload)`，不构造对象。

控制器声明自己需要哪个协议，构造时拿到任何满足它的对象：生产环境是上面这些实现，测试里是
记录调用的假对象。协议是结构化的，所以假对象不必继承用不到的行为，typed 成 `RunReader` 的对象
也不可能顺手 dispatch 一个 workflow。

## 这里没有什么

没有问题流程，没有跨模型编排，没有存储：

- "触发一次 workflow 并等待"、"订阅一个仓库"、"应用一条投递"是流程，住在
  [`controllers/`](../controllers/README.md)；
- 任何落库都是控制器经模型做的。

一个流程搬到这里，就会变成已有流程的第二份实现——这是这一层唯一的禁区。

`payloads.py` 是能力而不是流程：它只回答"这段 GitHub JSON 表示什么值"，不决定
哪一行该写、父对象是谁、写序如何。这些问题属于控制器。

## 加一种能力

加一个模块即可：第二个 provider、一个 S3 镜像、一个挡在 API 前面的缓存。
`controllers/` 只会因为想用新能力而改构造参数，业务判断一行不动。
