# 测试

## 需要一个真实的 PostgreSQL

测试套件会把模型绑定到真实服务上，每个测试使用重建后的 schema。服务器地址由环境变量或共享场景文件指定，
CI 使用 service container。

```bash
# 方式一：直接指定地址
GHAT_DB_HOST=localhost GHAT_DB_PORT=5432 \
GHAT_DB_USER=github_actions_tracker GHAT_DB_PASSWORD=github_actions_tracker \
PYTHONPATH=tests python -m pytest tests -q

# 方式二：使用共享的 postgres_scenarios.yaml
GHAT_TEST_PG_SCENARIO_FILE=../python-activerecord-postgres/tests/config/postgres_scenarios.yaml \
GHAT_TEST_PG_SCENARIO=postgres_18 \
PYTHONPATH=tests python -m pytest tests -q
```

没有可用服务器时，测试会跳过并说明原因；CI 设置 `GHAT_TEST_PG_REQUIRED=1`，此时跳过会被视为失败。

## 本地测试范围

只运行与本次改动相关的文件。完整套件在远端数据库上约需 3–5 分钟，在本地数据库上更快。

```bash
PYTHONPATH=tests python -m pytest tests/core -q       # 模型与核心
PYTHONPATH=tests python -m pytest tests/controllers -q # 流程
```

## 静态检查

```bash
ruff check src tests
black --check src tests
isort --check-only src tests
```
