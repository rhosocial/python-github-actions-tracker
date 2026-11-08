# 架构

本项目采用 Service-MVC-App 设计模式，依赖方向只有一个：App → Controller → Service → Model。
Adapter 属于 App 层，负责参数绑定、认证、序列化与错误码映射；`container.py` 负责装配。

```
App（adapters / container.py）   参数绑定、认证、序列化、错误码映射、装配
      ↓
Controller（MVC 的 C）            一组流程：触发、查询、订阅、投递、凭据
      ↓
Service                          GitHub 能力封装：协议 + 实现
      ↓
Model（MVC 的 M）                 每个模型封装自己的数据行为（ActiveRecord）
      ↑
PostgreSQL
```

三种职责分别说明：

- **Service**：封装可调用的能力，目前是 GitHub API。这一层不定义流程，也不直接写库；
  新能力只新增协议与实现，不应复制一份已有流程。
- **MVC**：Model 封装数据行为与校验；Controller 只编排流程；`views.py` 定义输出结构。
  规则尽量放在 Model 中，Controller 保持薄。
- **App**：负责装配与协议适配。`container.py` 一次性构造 settings、数据库连接与控制器，
  各适配层共用同一个容器。

本项目同时遵循**胖模型、瘦控制器**：

- Model 保存表的映射、关系、状态机与行级校验。一次 `save()` 就是一次完整的写；本项目没有 unit of work，也没有跨模型事务。
- Controller 只决定“先读什么、再调用哪个能力、最后写哪一行”，不重复实现数据规则。
- Service 只回答“这个能力怎么调用”，不决定流程，也不感知模型。

`config.py`、`database.py`、`container.py` 与 `views.py` 位于包根目录；`adapters/` 下每种协议对应一个 App。

每一步取舍的理由见[设计取舍](design.md)；二次开发见[自定义开发](extending.md)。
