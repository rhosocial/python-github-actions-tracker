# 功能

## 三层对象，一层一个工具

从一次失败的 run 定位到失败的 step，本项目只提供一条路径：

```
get_run(run_id)              → run 本体与 job 名册（job_id / name / 状态 / 结论 / 耗时 / failed）
get_job(job_id)              → 一个 job 的全部 step（状态、结论、起止、耗时、failed、has_log）
get_job_log(job_id, step=N)  → 该 step 的原文
get_job_log(job_id)          → 失败 job 的失败 step；否则整个 job 的合并文本
get_job_log(job_id, whole_job=true)   → 明确要求合并文本
get_run_failed_logs(run_id)  → 整个 run 中失败 job 的失败 step，按文本分组
```

- step 只属于 `get_job`：`get_run` 不接受 `include_steps`，也不返回 failure 块；日志摘录同样只在 `get_job_log` 中提供。
- `get_job_log(failed_steps_only=true)` 是批量便利：一次读取全部失败 step 的文本，省去多次调用。该参数不是第二套“哪个 step 失败”的判定方式。
- 失败 job 在不指定作用域时，从失败的 step 读起，并带 `scoped_to_failed: true`。作用域由 step 的结论决定，不由日志文本决定。
- 全部通过的 job 不会被催促读取日志：`has_log` 仍然如实为 `true`，数据已经存在，只是提示不会主动推荐读取。

## 两个日志来源

| 来源 | 内容 | 可得性 |
|---|---|---|
| `GET /actions/jobs/{id}/logs` | 一个 job 的合并文本 | 90 天内随时可读，并存入 `ghat_job_logs` |
| `GET /actions/runs/{id}/logs` | zip：合并文件 + `<job>/<step 号>_<step 名>.txt` | 按 step 拆分的文件只由 GitHub 在 run 结束后约一小时内提供 |

**这一小时是取回截止时间，不是保存期限。** 取回并写入 `ghat_step_logs` 的文本就是普通数据，可以永久读取。截止时间唯一决定的是是否还能继续补采。

## 引用一个 workflow：path、文件名、数字 id

显示名不是引用方式。`name:` 写在 workflow 文件里，因此同一个文件在不同分支可以有不同的名字。请使用 path 或数字 id。

例如，`vistart/Dockerfiles` 的 `build_and_deploy.yml` 就同时使用过 "php - Build and Deploy"、
"vlmcsd - Build and Deploy" 和 "ubuntu - Build and Deploy" 这几个名字。

## 失败判定

只有 `FAILED_CONCLUSIONS` 判定为失败，包括 failure、cancelled、timed_out、startup_failure、
action_required 与 stale。`conclusion = null` **不算失败**，那表示 GitHub 仍在生成元数据。

## webhook 降低延迟，但不是权威来源

webhook 投递可能丢失，run 也常在本系统之外创建。因此 run 的存在性、状态、列表与 latest
每次都回源 GitHub；读不到就报错，服务绝不降级返回缓存答案。
