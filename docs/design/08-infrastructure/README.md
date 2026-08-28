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

**observability 段** (iter-43): 任务停滞检测参数配置化 —
`stall_check_interval` (后台检查周期, 秒, 实际最小 10, 默认 60) /
`stall_minutes` (停滞判定阈值, 分钟, 默认 30, ≤0 禁用检测与告警)；
Secretary 激活时由 station_controller 读取驱动 `start_stall_watcher`；
缺省段回退默认值兼容旧配置。

**observability 日志修剪字段** (iter-54, 补强#2): `log_retention_days`
(日志保留天数, 默认 30, ≤0 禁用修剪) / `log_prune_interval_hours`
(修剪周期, 小时, 实际最小 1, 默认 24, ≤0 禁用) / `log_vacuum`
(修剪后执行 VACUUM, 默认 true)；由 station_controller `_prune_loop`
节流驱动 `Database.prune_logs()` (llm_call_log/chat_history/
resource_usage_log 仅删已上报/progress_reports/heartbeat_log 固定 24h)。

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

**闭环接线** (iter-44): 新增全局事件回调 `set_event_callback` (每条
capture 触发, 异常隔离) 与突发告警冷却去重 (同模块两次告警最小间隔
= 告警窗口, `_last_alert_at` 按模块独立, `clear()` 同重置)；
`station_controller.start()` 装配: 事件回调 → event_bus `error_captured`
(WS 实时刷面板), 突发告警 → event_bus `error_burst` + Bot 推送。

**自愈诊断** (iter-46, F4.2 首层): 模块级 `DIAGNOSIS_RULES` 模式规则表
(超时/连接/认证/限流/上游5xx 五类, 各带建议文案与动作标识) +
`diagnose(window_records)` 方法: 缓冲错误小写子串匹配, 首命中归属防重复
计数, findings 命中数降序 (命中数/影响模块/最近时间/样例/建议),
未命中计入 `unmatched`; window 夹取 1~500。

**落盘持久化** (iter-47): `set_persist_callback` 第三回调 — 每条 capture
触发 `callback(record_dict)` (异常隔离, 落盘失败不影响捕获); 由
`station_controller.start()` 装配 → Database `error_log` 表 (v6 迁移,
容量修剪保留最近 2000 行), 补齐 docstring 声称的「落盘 SQLite」能力,
重启不再丢失诊断历史。

**历史诊断扩展** (iter-48): 规则匹配逻辑抽为模块级纯函数
`diagnose_records(records)` (无状态, 首命中归属/降序/未命中计数规则不变),
实例方法 `diagnose()` 复用之; 端点 `source=history` 时对 `error_log`
落盘记录执行同一诊断, 重启后缓冲空仍可分析历史错误。

**自愈动作执行** (iter-49, 修复环节): 诊断建议的 action 标识接入执行器 —
`station_controller.run_heal_action()` 仅注册安全只读动作 (`check_peer`
UDP 探测已知设备 / `probe_balances` 余额探测), 未注册动作 (如 `retry_or_switch`)
返回 `manual_required` 不自动执行破坏性操作; 每次执行落盘 `heal_log` 表
(Database v7 迁移, 容量修剪 500 行) 并广播 `heal_action` 事件,
完成「检测→诊断→修复」闭环的可审计修复环节。

**自动自愈守护** (iter-50, 自动化环节): `_auto_heal_loop()` 守护线程按
`observability.auto_heal_interval` (最小 30s) 周期调用 `_auto_heal_once()` —
诊断缓冲后仅对 `_AUTO_HEAL_ACTIONS` 安全动作 (check_peer/rotate_key/switch_pool)
自动执行 (复用 run_heal_action), 同类别冷却期 (`auto_heal_cooldown`) 内跳过
防执行风暴, 需人工动作计入 `skipped_manual`; 默认关 (`auto_heal_enabled=false`),
守护/扫描/执行全链异常隔离 (no-op + warning); 状态经 `get_auto_heal_status()`
暴露 (开关/周期/冷却/累计轮次/最近动作)。

## host_rating.py — 主机评级

CPU/内存/磁盘综合得分（0~100）→ S/A/B/C/D 五级 + 可读摘要。
用于 Station Director 资源池筛选与项目规划按难度匹配主机。

## preflight.py — 启动自检

12 项前置检查（Python 版本/依赖/配置/目录/端口/网络/DB/Web UI 模板/
Web UI SPA/CLI Agent 后端），打印检查报告，失败即终止启动（CLI Agent
检测非致命）。iter-56 起新增 `_check_spa_bundle`（仅 secretary 角色）:
检查 `web/static/spa/index.html` 构建产物，缺失仅提示不阻断
（旧版仪表盘不受影响）。

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
| 2026-08-28 | iter-54 | 日志容量修剪 (补强#2): ObservabilityConfig 增 log_retention_days/log_prune_interval_hours/log_vacuum 三字段 (默认 30 天/24h/开, ≤0 禁用) |
| 2026-08-27 | iter-43 | config.py 新增 ObservabilityConfig 段 (停滞检查周期/阈值配置化, 缺省回退默认) |
| 2026-08-27 | iter-44 | error_tracker 闭环接线: 全局事件回调 (每条捕获触发) + 突发告警冷却去重 (按模块独立) |
| 2026-08-27 | iter-46 | error_tracker 自愈诊断: DIAGNOSIS_RULES 模式规则表 + diagnose() 分组建议 (F4.2 首层) |
| 2026-08-27 | iter-47 | error_tracker 落盘持久化: set_persist_callback 第三回调 (异常隔离, 每条捕获触发) → database error_log 表, 重启不丢诊断历史 |
| 2026-08-27 | iter-48 | 诊断规则抽取模块级纯函数 diagnose_records (实例 diagnose 复用), 支持历史落盘记录诊断双源 |
| 2026-08-27 | iter-49 | F4.2 修复环节: run_heal_action 自愈动作执行器 (安全只读动作注册制 + 未注册返回 manual_required), 执行记录落盘 heal_log (v7 迁移) + heal_action 事件广播 |
| 2026-08-28 | iter-50 | F4.2 自动化环节: _auto_heal_loop 守护线程 + _auto_heal_once 单轮扫描 (安全动作集自动执行 + 同类别冷却去重 + 需人工跳过计数), config.yaml observability.auto_heal_* 驱动 (默认关), get_auto_heal_status 状态暴露 |
| 2026-08-16 | iter-31 | api.py 按端点域拆分 (275 行工厂 → 装配层 + worker_routes_basic/pm/p2p 三模块; 路由集合/顺序/行为不变) + 工厂签名类型标注补齐 |
| 2026-08-16 | iter-27 后 | 初建 |
