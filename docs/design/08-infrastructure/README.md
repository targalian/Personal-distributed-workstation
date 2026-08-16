# 08 基础设施

横切关注点：配置、日志、事件总线、错误追踪、评级、启动自检、旧版路由。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| api.py | FastAPI 路由层 - Worker API (装配入口) |
| config.py | 配置管理 - 基于 Pydantic 的强类型配置校验 |
| error_tracker.py | F1.4: 本地错误聚合追踪 |
| event_bus.py | Station 事件总线 (M5)。 |
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
