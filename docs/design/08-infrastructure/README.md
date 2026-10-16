# 08 基础设施

横切关注点：配置、日志、事件总线、错误追踪、评级、启动自检、旧版路由。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| api.py | FastAPI 路由层 - Worker API (装配入口) |
| config.py | 配置管理 - 基于 Pydantic 的强类型配置校验 |
| data_paths.py | Resolve workstation data storage without changing the user's home directory. |
| error_tracker.py | F1.4: 本地错误聚合追踪 |
| event_bus.py | Station 事件总线 (M5)。 |
| execution_receipts.py | Persist execution claims and results so transport retries cannot start a second copy. |
| execution_routes.py | Shared Station/Worker task execution endpoints with durable receipts and polling. |
| host_rating.py | 主机评级系统 — 基于硬件配置自动计算能力等级 |
| logger.py | LAN Mesh 结构化日志系统 |
| preflight.py | 启动前自检模块 - 在程序启动时检查所有前置条件 |
| worker_routes_basic.py | Worker 基础路由 — 本机信息/共享文件/任务执行 (iter-31 拆分产物) |
| worker_routes_p2p.py | Worker P2P 路由 — 主机间消息接收 (iter-31 拆分产物) |
| worker_routes_pm.py | Worker 角色与 PM 路由 — 角色管理/PM 生命周期/子 Agent 管理 (iter-31 拆分产物) |
<!-- /AUTO:module-list -->
---

## config.py — 配置管理

Pydantic 强类型校验，读取 config.yaml 与环境变量，提供全局配置访问。
关键段: 端口、security.auth_enabled、发现参数、同步间隔。

## data_paths.py — 独立数据根目录（iter-58）

`LAN_MESH_DATA_DIR` 在启动前指定非空绝对路径；身份、token、默认 DB/回执、日志、缓存、备份、项目工作区以及首次生成配置统一使用该目录。配置默认值由 Pydantic factory 在实例化时解析；token 路径在导入时解析，因此运行中不切换目录。未指定时沿用旧路径，显式配置/日志路径仍优先。此选项不移动数据、不隔离工具访问，也不改变 PM 原数据库路径的恢复约束。发布检查以临时目录验证，详见 [发布流程](../../release-checks.md)。

## logger.py — 结构化日志

统一格式 `[时间] [级别] [模块] 消息`，控制台 + 文件双输出，
文件自动轮转（5MB × 3 备份）。环境变量 `LAN_MESH_LOG_LEVEL` /
`LAN_MESH_LOG_FILE` 可调。接口: `get_logger(name)`。

## event_bus.py — 事件总线（M5）

进程内发布/订阅：后台线程（资源记账/R7 预警等）发布事件，station_api
启动时装配 sink（经 asyncio 事件循环线程安全广播到 /ws 客户端），
替代纯 HTTP 轮询的实时感知。

**事件结构**: `{"type": <event_type>, "data": {...}, "ts": unix秒}`
**首版事件**: usage_reported / resource_alert / resource_config / host_event
**线程安全**: publish 可从任意线程调用；sink 投递由 asyncio loop 兜底。

## error_tracker.py — 错误聚合追踪（F1.4）

捕获聚合异常（按模块/类型分组）、统计 API（频率/最近时间/影响模块）、
错误率告警。实现: 内存环形缓冲 + 定期落盘 SQLite。支持装饰器
`@error_tracker.track("station")`。

## host_rating.py — 主机评级

CPU/内存/磁盘综合得分（0~100）→ S/A/B/C/D 五级 + 可读摘要。
用于 Station Director 资源池筛选与项目规划按难度匹配主机。

## preflight.py — 启动自检

11 项前置检查（Python 版本/依赖/配置/目录/端口/网络/DB/Web UI 模板/
CLI Agent 后端），打印检查报告，失败即终止启动（CLI Agent 检测非致命）。

## api.py — Worker 路由装配层 (iter-31 拆分后)

**职责**: 装配入口 — 原 275 行 `create_worker_router` 工厂函数按端点域
拆为 3 个子模块，本文件仅负责 `include_router` 装配（工厂签名与
路由集合/注册顺序不变，worker.py 与 station_controller 的既有导入
路径不变）。

**拆分结构** (路由函数名/端点路径/行为逐字保留):
| 模块 | 职责 |
|---|---|
| worker_routes_basic.py | 本机信息 (/info)/共享文件 (/shared*)/任务执行 (/tasks/execute, /agents/cli-status) |
| worker_routes_pm.py | 角色管理 (/role/*)/PM 生命周期/子 Agent 管理 (/pm/*) |
| worker_routes_p2p.py | 主机间 P2P 消息接收 (/api/p2p/receive) |

**使用方**: Worker 独立进程 (worker.py 全量装配) 与 Station 内嵌
Worker (station_controller 仅传 collect_info_fn/shared_folder，
缺依赖端点自动 503/降级)。工厂签名带完整类型标注 (iter-31 P2)。

历史说明: 早期 Secretary API (register/heartbeat/hosts) 已随
secretary.py 删除 (P3)，Secretary 端路由由 station_routes_* 承担。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-31 | api.py 按端点域拆分 (275 行工厂 → 装配层 + worker_routes_basic/pm/p2p 三模块; 路由集合/顺序/行为不变) + 工厂签名类型标注补齐 |
| 2026-08-16 | iter-27 后 | 初建 |


## 执行回执（iter-45）

`execution_receipts.py` 使用独立 SQLite 保存执行编号、请求摘要、身份字段、进程归属及结果。BEGIN IMMEDIATE 保证同一编号只有一个请求取得执行权；相同请求重复返回已有回执，编号对应不同内容返回 409。先提交记录，再启动运行时；写入记录失败不执行工具。结果只发布一次，结果写盘失败保留未知的在途记录，不能借重发再次执行。

`execution_routes.py` 为 Station 和 Worker 提供 POST `/tasks/execute` 与 GET `/tasks/executions/{execution_id}`。Station 异步返回 started；独立 Worker 保留同步返回结果，但将执行移出事件循环，重复请求或状态查询不会被长任务阻塞。均使用现有节点认证中间件。名称、input_data 和 1–128 位安全字符的 subtask_id 必须有效，否则返回 400。结果中的运行时异常和模型错误统一为 failed。

回执库按需初始化：Station 位于主数据库旁的 `<主库名>-executions.sqlite3`，Worker 默认为 `~/.lan_mesh/worker-executions.sqlite3`。没有修改主库 schema。已完成结果可跨进程读取；旧进程未完成的记录返回 unknown，不自动重新执行。返回 unknown 不证明旧进程或外部工具已结束。

当前不自动清理回执，避免清理后重复编号再次执行；长期容量和清理策略待补。备份/迁移应包含回执库，不能仅恢复主库。去重只覆盖保留的同一回执库与相同执行编号，不提供跨主机数据库共识、PM 检查点续跑或外部工具副作用回滚。
