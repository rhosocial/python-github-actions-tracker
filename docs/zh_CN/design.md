# 设计取舍

本页面为二次开发而准备。每一节都先给出结论，然后再给出其他方案为什么被否决；最后，设立了相关测试，用于断言每一项结论。

## GitHub 是事实来源，本地库只是缓存

有四个问题**每次都向 GitHub 询问**：

* 这次 run 是否存在；
* 这次 run 的状态是什么；
* 这个分支或这个 workflow 下有哪些 run；
* 哪一次 run 是最新的。

这些都只在真正回答得出时才作答，缓存不作为事实来源。接口不可达就报错，不降级返回旧答案：
一个过时的“成功”比一次报错更糟，因为调用方会依据该答案做出决定。读取成功而本地有差异时，再把响应写回，
于是缓存自己收敛，不需要另外的修复任务。

这也是两条通道可以放心使用的根本原因：漏收一次 webhook 不构成数据丢失，下一次读就会补齐。
因此服务的定义里没有后台轮询、没有任务队列——读路径本身就是修复路径。

“每次都回源”靠条件请求把代价压住：`ETag` 命中的 `304` 不消耗 API 额度。

## 纯 ActiveRecord，没有 unit of work

一次 `save()` 就是一次完整的写。没有会话，没有 flush，也没有“这几个模型一起提交”的边界。

于是一次 webhook 投递是一条**写序列**，不是一个原子单元：

```text
验签 → 解析 id → 写入父对象 → 写入 attempt → 写入 job → 写入 step
```

第 4 步失败时，写入就停在那里。这个半途状态是合法状态：已写入的都是完整数据行，缺口由下一次读（以 GitHub
为准）补齐。投递台账会记下这次没有处理成功，缺口因此是可见的，不是无声的。

被否掉的替代方案是给整条级联加事务。该方案承诺的是“回滚一次本可以重读的事件”，并带来与之配套的锁和
待处理状态——而这两样都回答不了“GitHub 现在怎么说”。

守则测试 `tests/core/test_no_unit_of_work.py` 的判据是：一旦出现事务、write scope、投递锁或提交后的
延迟任务，该测试就会失败。

## 每一级只认直接父对象

```text
Repository → Workflow → WorkflowRun → RunAttempt → Job → Step
```

每一级只接收自己的**直接父对象**。job 不认识 run：job 属于某个 attempt，而一个 attempt 只属于一个
run。若在 job 上再放一个 `run_id`，两个外键就可以互相矛盾，而且会出现“某个非键属性由键的一部分
决定”——这是 BCNF 违例，也是 `ghat_jobs` 不设 `run_id` 的原因。

“payload 指向哪一个父对象”由 Controller 判断，不由模型判断：payload 知道 attempt 编号，父对象
知道自己有哪些 attempt。

## 模型只收语义值，GitHub JSON 由一个服务翻译

`services/github/payloads.py` 是唯一读取 GitHub 字段名的类。`GithubPayloads` 返回的键就是
**模型列名**：

| GitHub 的写法 | 模型存的列 |
|---|---|
| `head_sha` | `commit_sha` |
| `run_attempt` | `attempt_number` |
| `run_started_at` | `started_at` |
| run 对象上的 `id` | `github_run_id` |
| job 对象上的 `id` | `github_job_id` |

这不是风格问题。GitHub 从未承诺接口形状不变，把解析写死在 Controller 里，等于让业务逻辑绑定某个
版本的接口；集中到一个类，接口改名就只改一处，也能脱离数据库测试。

模型只保留值入口：

```python
Workflow.refresh(*, workflow_id=None, name=None, path=None, state=None)
WorkflowRun.refresh_metadata(*, run_number=None, ...
RunAttempt.record(run, attempt_number, *, status=None, ...
Job.record(attempt, *, github_job_id, ...
Step.record(job, *, number, ...
```

“行”的不变量在实例化时由 Pydantic 校验：

* `IdentityMixin`——编号不为负，`attempt_number >= 1`；
* `UtcTimestampMixin`——naive 时间按 UTC 读，让存进去的时间和算出来的时间可比；
* `Repository.full_name` 必须是 `owner/name`；`RepoWebhookSecret.secret` 不能为空、`state`
  只能是本项目那三种；`PatCredential.name` / `token` 不能为空。

`status` 和 `conclusion` **不做词汇表白名单**。GitHub 可能新增取值；若因为一个没见过的结论就拒绝落库，
就会丢掉这次执行的唯一记录。显式拒绝未知状态是状态机的职责，位置在状态转移处。

守则测试 `tests/models/test_no_payload_in_models.py` 用 AST 扫描 `models/`：一旦出现 `*_from_remote`
命名、公开方法带有 `Mapping` 参数，或模型模块里出现模块级函数，该测试都会失败。

## run 是身份，attempt 是历史

`ghat_workflow_runs` 存的是 run **是什么**：workflow、编号、分支、提交、PR。run 的状态和时间不是列，
因为那些是某一次执行的事实。

`ghat_run_attempts` 存的是每次执行**做了什么**。run 的“当前状态”是查询时由数据库从最新 attempt
算出来的（`latest_attempt`、`latest_status`、`latest_conclusion` 等）。同一个事实只有一份，不存在
第二份可以与之矛盾。

状态机只向前：`queued → in_progress → completed`。已结束的 attempt 又迟到一条 `in_progress`，那是
过去乱序到达，记录下来并跳过，不改写历史。

attempt 编号刻意允许稀疏。“没有 attempt 1”可能真的意味着 GitHub 先报上来的是 attempt 3；表里存的是
被告知的事实，所以字段叫 `latest_attempt_number`，不叫 `attempts_total`。

## 一份合并日志，一份限时能采到的分片

| 来源 | 是什么 | 可得性 |
|---|---|---|
| `GET /actions/jobs/{job_id}/logs` | job 的整篇文本 | 90 天内随时可读 |
| `GET /actions/runs/{run_id}/logs` | run 级归档，含 step 分片与合并文件 | step 分片只在 run 结束后约一小时内提供 |

合并文件与 job 端点逐字节相同，所以 `ghat_job_logs` 的行为没有变。step 分片是**采集截止时间**，不是
保存期限：一旦写进 `ghat_step_logs`，这条日志和其他数据行一样长期存在。窗口外归档不再提供分片，答案是
`capture_closed`——不能说“日志过期”，因为没有任何已存的数据行过期。

两张日志表的分区键都是执行**被触发**的时刻（`ghat_run_attempts.github_created_at` 拷贝到日志行）。
一条日志归在它所属的 run 名下，而不是归在它恰好被下载的那个小时。

## 字段名要说清这是本地数据

阅读这些字段的人不能把“本服务有什么”误当成“GitHub 有什么”。

| 前后缀 | 含义 |
|---|---|
| `*_cached` | 本地数据行给出的计数或存在性答案 |
| `latest_*` | GitHub 最近一次告诉我们的值 |
| `*_synced_at` | 这一组上一次完整列自 GitHub 的时间 |

本地数字不能用裸的 `total` / `count` / `all`；MCP 工具共用一句 `CACHE_NOTE`，说明只写一处，而不是
十三个工具各写一遍。

## 这个项目刻意不做的事

* 没有后台轮询、没有任务队列、没有定时器；
* 没有跨模型事务，也没有暗示存在事务的锁；
* 不在 run 上做 workflow 快照，也不判断 workflow 文件是否还存在；
* 不用 `RawSQL`，不手拼 `SELECT`；该用表达式的地方由 ORM 表达式节点承担。
