# 自定义开发

本页是操作指南，不是导览：本页回答四类“我想加 X”的问题，这个服务准备好接纳这四类扩展。每节末尾列出
要修改的文件和必须通过的测试。

## 改动应该落在哪

| 我想…… | 落在 | 改动**不能**…… |
|---|---|---|
| 读写另一个 GitHub 端点 | `services/github/`（协议 + 实现） | 知道流程，或写数据库 |
| 接受新字段、新事件 | `services/github/payloads.py`（`GithubPayloads`） | 在别处出现 GitHub 字段名 |
| 改变一次投递做什么 | 某个 `Controller` | 知道是哪种协议发起的 |
| 加 REST、MCP、CLI 接口 | `adapters/` | import `models` |
| 存一个新事实 | `models/` + `schema/model.py` | 读 payload，或写 GitHub 的键名 |
| 改变答案长什么样 | `views.py` | 重新决定流程 |

## 目录地图

```text
adapters/      每协议一个 App：MCP、REST、CLI
controllers/   流程，也是唯一写 ORM 查询的地方
services/      能力，按协议封装；github/ 里有唯一的 payload 翻译器
models/        叶子：表映射、关系、状态机、行级校验
views.py       适配层最终输出的数据结构
config.py      从 GHAT_* 环境变量读设置
database.py    连接池与每请求的连接作用域
container.py   组合根
```

## 改动被期望守住的不变式

* 一次 `save()` 是一次完整的写；没有跨模型事务；
* `models/` 不 import `services/`、`controllers/`、`adapters/`、`github/`；
* `controllers/` 与 `services/` 里除入口外没有模块级函数；
* 公开模型方法没有 `Mapping` 参数，也不存在 `*_from_remote`；
* 没有 `RawSQL`，没有手拼的 `SELECT`。

自查命令：

```bash
# models 保持叶子层（应无输出）
rg -n 'from \.\.(?!models)' -P -g '*.py' src/rhosocial/github_actions_tracker/models

# 模型里没有 payload 解析，也没有模块级函数
rg -n '_from_remote' -g '*.py' src/rhosocial/github_actions_tracker/models
rg -n '^(async )?def ' -g '*.py' src/rhosocial/github_actions_tracker/models

# 适配层不直接触碰 models
rg -n '^\s*from \.+models' -g '*.py' src/rhosocial/github_actions_tracker/adapters
```

## 增加一种 GitHub 能力

1. 在 `services/github/protocols.py` 的协议区增加一个结构化协议：方法返回调用方需要的内容，
   不暴露该能力在 GitHub 上的写法。
2. 在 `services/github/<resource>.py` 中基于同一个 `AsyncGithubClient` 实现该协议。
3. 需要该协议的控制器把协议作为构造参数。container 已经为每个资源组创建一个实例，请把新增的资源组加进去。
4. 在 `tests/conftest.py` 中编写一个会记录调用的假对象；流程测试对该假对象做断言，而不是访问 GitHub。

文件：`services/github/protocols.py`、`services/github/<resource>.py`、
`controllers/<flow>.py`、`container.py`。
测试：用假对象的控制器流程套件；CI 中不访问 GitHub。

## 增加一个字段或一个事件

1. 在 `services/github/payloads.py` 中扩展对应的 Pydantic 模型。目前未知键会被忽略；只添加需要读取的
   字段，并为该字段指定类型。
2. 扩展 `GithubPayloads` 的方法，使其返回按**模型列名**组织的值（`head_sha` → `commit_sha`），
   不按 GitHub 的写法。
3. 增加控制器入口来保存该值：`store_*` 接收 payload，先交给服务翻译，再解析父对象，最后调用模型的值入口。
4. 如果模型需要新增列，就在 `schema/model.py` 中添加并重建 schema——这个项目没有原地迁移。

文件：`services/github/payloads.py`、`controllers/run.py`（或相应的行层），需要新列时还有
`models/<entity>.py`。
测试：翻译本身进 `tests/services/github/test_payloads.py`；级联行为由控制器套件覆盖。

## 增加一个实体

在级联里新增一级时，需要同时维护同步与异步两半：

1. `models/<entity>.py`——同步类与 `Async` 类，共用同一个 `__table_name__`。
2. 关系：双向声明，`inverse_of` 写对端属性名，并到对端类补上另一条。
3. `schema/model.py`——表、外键（有历史的用 `RESTRICT`）、唯一约束，存日志还要分区列。
4. 行级校验写在 `models/validators.py` 或模型自己身上，而不是 payload。
5. `services/github/payloads.py`——payload 形状与值翻译。
6. `controllers/<flow>.py`——解析父对象、按序写入的 `store_*` 入口。
7. `views.py`——对外记录；凡是本地数字，命名用 `*_cached` / `latest_*`。
8. 在 `models/__init__.py` 中发布这个模型；关系注解保持字符串。
9. 测试：模型测试只用具值构造数据行；级联测试保持 AST 干净；同步孪生要跑通一条完整链
   （repo → … → 新实体）。

漏掉其中任何一步，以下测试通常会先失败：

```bash
tests/models/test_sync_twins.py
tests/models/test_no_payload_in_models.py
tests/core/test_no_unit_of_work.py
```

## 增加适配层接口

REST 接口在 `adapters/rest/routes/` 中添加路由与 schema；MCP 工具在 `adapters/mcp/tools/` 中添加工具函数。
两者只做参数绑定、错误映射和序列化，然后调用控制器，不得访问更下层。适配层不 import 模型，不出现
payload 键名，也不出现第二处决定流程的地方。

## 在正确的层上测试

| 层 | 测试构造什么 | 该层不能做 |
|---|---|---|
| `services/` | 一段 payload 映射 | 访问数据库 |
| `models/` | 用值构造一行 | 构造 payload |
| `controllers/` | 一次配有假对象的流程 | 访问 GitHub |
| `adapters/` | 一个请求及其参数 | 访问 GitHub |

本地只运行受影响的文件。测试需要把模型绑定到真实 PostgreSQL，运行整个 `tests/` 目录通常要几分钟；
整个目录由 CI 运行。

```bash
pytest tests/services/github/test_payloads.py tests/models/test_<entity>.py -q
```

## 常见错误

* GitHub 字段名出现在 `services/github/payloads.py` 之外；
* 模型方法带有 `Mapping` 参数，或命名以 `*_from_remote` 结尾；
* `models/` 中存在模块级工具函数；
* 使用 `RawSQL`，或用表名字符串拼接 SQL 文本；
* 出现事务、write scope，或提交后的延迟任务；
* 用 `total` / `count` 命名仅本服务持有的计数。
