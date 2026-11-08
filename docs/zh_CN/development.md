# 开发

## 本地虚拟环境

先克隆仓库，然后按自己的习惯创建并激活虚拟环境。项目支持 Python 3.10 及以上版本，CI 与镜像使用 3.15。

本项目依赖三个兄弟项目。请在自己的虚拟环境中以可编辑模式安装它们，再安装本项目与开发依赖：

```bash
python -m pip install -e ../python-activerecord
python -m pip install -e ../python-activerecord-postgres
python -m pip install -e ../python-mcpserver

python -m pip install -e ".[dev]"
```

如果安装依赖较慢，可以根据实际网络选择镜像源。三个兄弟项目各自有 release 线，安装前请确认目录分支与所需版本一致。

## 运行测试

测试方法见[测试](testing.md)。测试需要一个真实的 PostgreSQL（9.6 以上即可，生产环境验证过 18.6）。
测试套件会把模型绑定到真实服务上，每个测试使用重建后的 schema。服务器地址由环境变量或场景文件指定，
CI 使用 service container：

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

## 代码结构

`src/rhosocial/github_actions_tracker/` 目录包含：

- `models/`：表映射、状态机与行级校验，位于依赖最底层；
- `services/`：GitHub API 封装，并包含 `GithubPayloads`，由该服务把 GitHub JSON 翻译成模型需要的值；
- `controllers/`：流程实现，也是唯一编写 ORM 查询的位置；
- `views.py`：适配层输出的数据结构；
- `config.py`、`database.py` 与 `container.py`：配置、数据库连接与装配；
- `adapters/`：每种协议一个适配层。

架构与设计取舍分别见[架构](architecture.md)和[设计取舍](design.md)；二次开发见[自定义开发](extending.md)。
