# 命令行

`rhosocial-github-actions-tracker` 与 `rhosocial-github-actions-tracker-db` 两个入口，全局参数
`--format`（`text` / `json`）与 `--verbose` 放在子命令**之前**。

```bash
rhosocial-github-actions-tracker --format json status            # 服务状态（ci_status 的 CLI 版）
rhosocial-github-actions-tracker runs --name owner/repo         # 列出最近的 run
rhosocial-github-actions-tracker latest --name owner/repo       # 最新一次 run
rhosocial-github-actions-tracker jobs --run-id <id> --step 3    # 某个 job 的 step
rhosocial-github-actions-tracker logs --job-id <id> --step 3    # step 日志
rhosocial-github-actions-tracker failures --run-id <id> --repo owner/repo   # 同一条失败只给一份
rhosocial-github-actions-tracker trigger --workflow ci.yml --ref main --name owner/repo
rhosocial-github-actions-tracker repo subscribe --name owner/repo --credential pat/work
```

## 数据库维护：`rhosocial-github-actions-tracker-db`

```bash
rhosocial-github-actions-tracker-db is-initialized        # 0=已初始化
rhosocial-github-actions-tracker-db init                  # 建表（幂等）
rhosocial-github-actions-tracker-db stats                 # 各表行数
rhosocial-github-actions-tracker-db usage                 # 各表体积与增长（待分区落地后完善）
rhosocial-github-actions-tracker-db cleanup --runs-before 90d --logs-before 365d
rhosocial-github-actions-tracker-db deliveries --unhandled --limit 20
```
