# 02 Station 核心

系统骨架：进程控制器、路由层、基础设施管理器、数据库持久化。
设计原则: **Station Director 只管「机器」，Secretary 只管「项目」**。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| database.py | SQLite 数据库存储层 - Secretary 端主机注册记录持久化 |
| runtime_trace.py | 运行时追踪与性能审计 — P0/P1 运行时分析引擎 |
| singleton.py | 主机级工作站单实例守护 (E6)。 |
| station_api.py | Station Director API 路由层 (装配入口) |
| station_controller.py | Station Director 独立控制器 — 基础设施管理入口 |
| station_director.py | Station Director (工作站主管) — 基础设施资源管理器 |
| station_routes_basic.py | Station 基础路由 — 健康/错误/角色/注册心跳/主机网络/Director (P1 #2 拆分产物) |
| station_routes_chat.py | Station 交互路由 — 秘书聊天/多对话/PM 线程/Bot 消息入口 (P1 #2 拆分产物) |
| station_routes_common.py | Station API 路由公共层 — 限流/认证中间件与共享工具 (P1 #2 拆分产物) |
| station_routes_pm.py | Station PM 路由 — PM Agent 管理/进度上报/子任务同步/团队 (P1 #2 拆分产物) |
| station_routes_projects.py | Station 项目与能力路由 — 项目管理/MCP 工具/模型路由/技能库/Bot 通道 (P1 #2 拆分产物) |
| station_routes_resources.py | Station 资源与密钥路由 — 模型资源池/配置向导/密钥同步/事件与角色卡 (P1 #2 拆分产物) |
| station_routes_tasks.py | Station 任务路由 — Agent 管理/任务生命周期/图结构/交付闭环/任务记忆 (P1 #2 拆分产物) |
| station_routes_worker.py | Station Worker 侧路由 — 内嵌 Worker 端点/P2P 通讯/云存储同步 (P1 #2 拆分产物) |
<!-- /AUTO:module-list -->
---

## station_controller.py — Station 控制器（核心枢纽）

**职责**: `python main.py station` 的进程主体，基础设施管理入口。

**核心流程**:
1. UDP 广播发现对端 station；`_on_device_seen` 汇聚全部发现事件
2. 首次发现对端 → HTTP 自动注册；已注册 → 轻量心跳落库
3. Secretary 选举: 5s 发现窗口，网络无其他 Secretary 则本机当选
4. 当选后同进程加载 Secretary 组件（chat_handler / orchestrator / PM /
   model_router / MCP 网关）

**E4 冲突仲裁**: 选举时机错开致双 Secretary 时按 `device_id` 字典序
确定性让位（较大者降级为 Station，双端对称规则保证收敛）; 发现包
携带真实角色（`packet.role`），修复对端永远无法经 UDP 感知 Secretary
身份的问题。

**E5 Secretary Failover**: 选举只在启动时进行，Secretary 宕机后无人
接管。`_prune_loop` 每轮清理后调用 `_secretary_failover_check()`：
Secretary 超时离线且网络无其他在线 Secretary 时，由 `device_id`
字典序最小的在线 Station 接任（与 E4 同一对称仲裁规则，多节点并发
接管亦自然收敛；双 Secretary 短暂并存时由 `_on_device_seen` 让位
逻辑裁决）。接管复用 `activate_secretary()`（含密钥对齐与断点恢复），
并广播 `secretary_failover` WS 事件 + Bot 通知。

**S1/S3 密钥与版本同步 + F1 角色无关自动对齐**（本模块近期核心增量）:
- `_align_config_with_peers()`: **F1 核心** — 与主从无关的对齐仲裁。
  内容指纹一致跳过；不一致时按 `config_ts` 新者胜（本机新推、
  对端新拉）；ts 缺失/相等按资源池数仲裁
- `_align_loop()`: 周期对齐线程（60s），任意节点主动与对端收敛
- `_auto_upgrade()`: 版本落后自动对齐 — git pull + 依赖安装
  （工作区脏则跳过；同 commit 仅试一次；`auto_upgrade: false` 可关）
- `push_resource_secrets()`: 加密推送（角色无关，对齐仲裁选择推时调用）
- `pull_resource_secrets()`: 加密拉取，解密 → 指纹校验 → 幂等跳过/落盘热重载
- `_startup_sync_once()`: 启动一次性同步（发现层 + DB 双通道查对端 →
  等选举 → 版本领先检测 + 密钥对齐），**替代 60s 轮询**
- `_sync_with_new_peer()`: 新主机入网即时对齐（免轮询，主从无关）
- `_converge_mesh_token()`: mesh_token 信任根收敛（token 分歧自愈，
  详见 [05-resources-secrets](../05-resources-secrets/README.md)）

**关键接口**: `start()` / `activate_secretary()` / `deactivate_secretary()`

**依赖**: discovery, station_director, database, secret_sync, version_sync,
http_retry, config, event_bus

## station_director.py — 基础设施资源管理器

**职责**: 主机生命周期管理（只管机器，不管项目）。

**核心逻辑**:
- `on_host_registered()`: 评级 + 事件记录 + 持久化（携带 code_version 落库）
- `on_heartbeat()`: 更新实时指标；心跳携带 `code_version` 时同步落库
  （版本统计三通道之一）；携带 `role` 时同步落库（E4 选举避让依赖 DB
  role，陈旧 role 会导致双 Secretary 脑裂）
- 资源池查询: 按评级筛选在线主机，供 Secretary/Planner 调度决策

**依赖**: database, host_rating, event_bus

## station_api.py — 路由装配层 (P1 #2 拆分后)

**职责**: 装配入口 — 原 2500+ 行单文件按路由域拆为公共层 + 7 个
路由模块，本文件仅负责 `include_router` 装配与 `/ws`、`/ws/worker`
WebSocket 通道。

**拆分结构** (路由函数名/端点路径/行为逐字保留, 装配后路由集合与
拆分前一致):
| 模块 | 职责 | 可用性 |
|---|---|---|
| station_routes_common.py | 限流/认证中间件与共享工具 (单一事实源) | — |
| station_routes_basic.py | 健康/错误/角色/注册心跳/主机/Director/运行时追踪 | 始终 |
| station_routes_tasks.py | Agent/任务/图/交付闭环/任务记忆 | Secretary |
| station_routes_resources.py | 模型资源/配置向导/密钥同步/事件 | Secretary |
| station_routes_pm.py | PM Agent 管理/进度/子任务/团队 | Secretary |
| station_routes_chat.py | 秘书聊天/多对话/PM 线程/Bot 入口 | Secretary |
| station_routes_projects.py | 项目/MCP 工具/模型路由/技能库/Bot 通道 | Secretary |
| station_routes_worker.py | 内嵌 Worker 端点/P2P/云同步 | 始终 |

**路由分层**:
- 基础路由（始终可用）: 主机注册/心跳/查询、角色激活、bootstrap-token、
  `/api/secrets/fetch`（S3 拉取端点，F1 起附带 `config_ts`）、
  `/api/secrets/sync-all`（F1 起角色无关对齐，任意节点可用）、
  `/api/resources/config`（F1 起放开 Secretary 限制，任意节点可保存后
  自动全网对齐）、`/api/version/*`（S2）
- Secretary 路由（`secretary_active` 为真才可用，否则 503）: 任务/Agent/
  项目/MCP 工具/模型路由/聊天；守卫统一走 common 的
  `check_secretary(controller)`
- `/ws`: WebSocket 实时推送（event_bus sink 装配于 station_api.py）
- `/ws/worker`: M5-2 Worker 事件直推通道 — 认证启用时握手前校验
  query 参数 token（mesh_token 恒定时间比较，不通过直接拒绝）；
  `usage_batch` 帧复用 HTTP 批量同一幂等路径（`apply_usage_batch`）
  并回 ack，Secretary 未激活时 ack 失败（Worker 不推游标，HTTP
  兜底链路后续补报）；其他 type 转发 event_bus → 自动广播前端 /ws；
  连接建立/断开记录 client IP 供运维观察

**设计要点**:
- 所有组件经 `controller` 可变引用访问，支持免重启激活/停用 Secretary
- `_AUTH_WHITELIST`: 免认证端点白名单（含 `/` 仪表盘 HTML 入口 —
  auth 开启时页面必须先加载才能执行 auth-token 自举; 注册引导/版本查询/secrets 拉取）
- mesh 认证态与 token 访问器 `get_mesh_auth_token()` 位于 common
  (原闭包全局迁移, 避免跨模块引用失效)
- station_api.py 兼容再导出 common 的中间件/工具，station_controller /
  worker 的既有导入路径不变
- `/api/secrets/receive` 接收端自愈: 解密失败且报 mesh_token 不匹配时,
  自动从推送方（请求来源 IP + 报文 src_port）收敛信任根后重试一次
  （`_heal_mesh_token_from`，E4）
- `/api/version/upgrade-notice` 收到领先通知时触发 `_auto_upgrade`
  自动升级（F1，工作区脏则安全跳过）

## 历史遗留清理 (P3)

- **secretary.py 已删除**: 早期独立 Secretary 进程实现，能力早已由
  station_controller + station_api 取代；`main.py secretary` 入口一并
  移除，唯一启动入口为 `python main.py station`
- **master.py 已删除**: 历史遗留空占位文件（全库零引用）
- **api.py 瘦身**: 旧 `create_secretary_router` / `broadcast_ws` 及其
  专用 payload 模型随 secretary.py 删除，仅保留 Worker 路由
  （Secretary 端路由由 station_routes_* 承担，端点全覆盖已在
  P1 #2 路由对比中验证）

## database.py — SQLite 存储层

**职责**: Secretary 端全部持久化（类名 `Database`）。

**表结构**: hosts（主机 + 版本列）、tasks/subtasks、chat_history、skills、
skill_assignments、resource_usage_log、events 等。

**迁移机制**: `SCHEMA_VERSION` + `_MIGRATIONS` 字典（当前 **v5**）：
- v5: llm_call_log 审计表 (运行时 LLM 调用性能追踪)
- v4: hosts 表新增 `code_version` / `version_ts` 列（S2/S3 版本统计）
- 迁移函数必须幂等（ALTER TABLE 包 try/except）；新增列的索引放迁移函数内

**关键接口**: `upsert_host()` / `list_hosts()` / `on_heartbeat` 相关 /
`record_usage()` / `insert_llm_call()` / `query_llm_metrics()` /
`backup()` (P2 #7) 等

## runtime_trace.py — 运行时追踪与性能审计

**职责**: P0/P1 运行时分析引擎 — 记录子任务执行轨迹与 LLM 调用性能明细。

**双写机制**:
- **JSONL 文件** (`~/.lan_mesh/trace.jsonl`): 子任务 start/end + LLM 调用记录; 50MB 自动轮转; 可用 `jq` / `pandas` 快速聚合
- **SQLite llm_call_log 表**: LLM 调用审计明细 (延迟/token/状态); 供 `/api/runtime/metrics` 聚合查询

**数据流向**:
```
agent_runtime.execute()
    → trace_subtask_start() → trace_subtask_end()
    → _call_openai_compatible() / _call_openai_with_tools()
        → trace_llm_call()  (JSONL + SQLite 双写)
```

**写入钩子**:
- `trace_subtask_start()` / `trace_subtask_end()`: 包裹 `execute()` 方法, 记录技能类型、耗时、状态
- `trace_llm_call()`: 嵌入 `_call_openai_compatible` (流式, chat)、`_call_openai_with_tools` (ReAct, tools)、`_handle_cli_agent` (CLI Agent)
- `set_db(db)`: station_api.py 装配时注入 Database 引用 (避免循环导入)

**查询端点** (station_routes_basic.py, 始终可用):
| 端点 | 数据源 | 用途 |
|---|---|---|
| `/api/runtime/metrics?hours=1` | SQLite | 聚合指标: 调用次数/延迟/P99/Token/按模型拆分 |
| `/api/runtime/trace?limit=50&type=` | JSONL | 最近追踪记录明细 (子任务+LLM) |
| `/api/runtime/calls?limit=50` | SQLite | LLM 调用明细 (调试/排查) |
| `/api/runtime/stats?hours=1` | JSONL | 子任务成功率/模型分布/错误 Top5 |

**线程安全**: JSONL 追加写入用 `threading.Lock`; SQLite 经 Database 线程局部连接。

**P3 任务流追踪** (iter-38): 追踪粒度从单次调用扩展到任务级生命周期。
- `trace_task_event(task_id, stage, detail, pm_id)`: 写 `type="task_flow"` JSONL 记录; 钩子均在 `try/except: pass` 内异常静默
- 钩子点: 任务提交 (station_routes_tasks / station_controller)、PM 生命周期 (pm_agent `report_status()` 单点覆盖全部状态)、子任务结果、交付上报 (`TASK_STAGE_LABELS` 阶段标签映射)
- `read_task_flow()` / `task_flow_waterfall()`: 按 task_id 聚合 (追溯 5000 行), 计算 gap_ms/total_ms
- `task_flow_overview()` (iter-39): 多任务聚合总览 (每任务末阶段/阶段数/总耗时/终态标记 `TASK_FLOW_TERMINAL_STAGES`), 末活动倒序; iter-40 增停滞检测 (`stall_minutes` 阈值, 未到终态且空闲超阈标 `stalled` + `idle_ms`, ≤0 禁用, 终态永不标)
- 停滞主动告警 (iter-41): `check_stall_alerts()` 单轮检查 — 档位去重防刷屏 (空闲 1/2/4 倍阈值 → Lv1/2/3, 仅新停滞/档位升级重推; 恢复活动清档位可再告警; 终态永不告警); `start_stall_watcher()` daemon 线程 60s 周期 (Secretary 激活时启动, 异常隔离); 推送链: event_bus `task_stall_alert` → WS 实时广播 + Bot 三档模板 (`task_stall_alert_low/task_stall_alert/task_stall_alert_high`); 端点 `/api/runtime/task-stall-alerts` (活跃告警+守护状态) 与 `.../check` (手动触发); iter-43 起检查周期/阈值改由 config.yaml `observability` 段驱动 (缺省 60s/30min, ≤0 禁用)
- 端点: `/api/runtime/task-flow?task_id=&limit=` → Dashboard 运行时 Tab 瀑布查询
- 任务记忆总览 (iter-42, F4.1 可视化): `/api/task-memory/overview?limit=` 在 `/stats` 基础上返回按类型分组聚合 `by_type` (次数/成功率/平均耗时/推荐模式, 多者在前) + 最近沉淀 `recent` (关键词≤5 截断, limit 夹取 1~50); 复用 `query_task_memory`/`get_task_memory_stats`, Secretary 未激活 503

**P2 #7 DB 自动备份**: `__init__` 末尾调用 `backup()` — sqlite3 在线
备份 API 一致性快照至 `~/.lan_mesh/backups/<stem>-<时间戳>.sqlite3`,
保留最近 3 代; 失败仅告警不阻断启动。

**依赖**: 无外部依赖（标准库 sqlite3）

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-26 | iter-39 | P3 任务流总览: task_flow_overview 多任务聚合 (末阶段/终态判断/末活动倒序); /api/runtime/task-flow-list 端点; Dashboard 运行时 Tab 总览表 + 一键查瀑布 (UI-037) |
| 2026-08-26 | iter-40 | P3 任务停滞检测: task_flow_overview 增 idle_ms/stalled (stall_minutes 阈值, 终态免疫, ≤0 禁用); 端点参数透传与夹取 (0~1440); Dashboard 状态列三态 + 红色告警横幅 (UI-038) |
| 2026-08-26 | iter-41 | P3 任务停滞主动告警: check_stall_alerts 档位去重 (1/2/4 倍阈值 Lv1/2/3, 仅升级重推, 恢复清档) + 60s 守护线程; event_bus task_stall_alert → WS toast + 总览表自动刷新 + Bot 三档模板; 告警查询/手动检查端点 (UI-039) |
| 2026-08-26 | iter-42 | F4.1 任务记忆面板: /api/task-memory/overview 端点 (全局统计 + 按类型分组 + 最近沉淀); Dashboard 运行时 Tab 记忆面板 (统计卡片/分组表/最近列表, 503 优雅降级) (UI-040) |
| 2026-08-27 | iter-43 | 停滞检测参数配置化: config.yaml observability 段 (stall_check_interval/stall_minutes) 驱动守护线程, 缺省回退 60s/30min, ≤0 禁用 |
| 2026-08-25 | iter-38 | P3 任务流全链路追踪: trace_task_event/read_task_flow/task_flow_waterfall; pm_agent report_status 单点钩子 + 提交/子任务结果/交付阶段点; /api/runtime/task-flow 瀑布端点; Dashboard 瀑布查询 (UI-036) |
| 2026-08-25 | iter-36 | P0/P1 运行时追踪与性能审计: runtime_trace.py (JSONL 子任务轨迹 + SQLite llm_call_log 审计表); agent_runtime execute() 计时钩子; LLM 三路径 (chat/tools/cli) trace_llm_call; /api/runtime/{metrics,trace,calls,stats} 端点; database v5 迁移 |
| 2026-08-25 | iter-35 | M5-2 多主机联验: /ws/worker 连接建立/断开记录 client IP (运维观察); 分机升级后双机 WS 直推端到端验证 7/7 全过 |
| 2026-08-18 | iter-35 | E6: 主机级单实例守护 — ~/.lan_mesh/station.lock 锁仲裁 (同版本/更新实例取消启动, 旧版实例关闭接管, 僵尸锁覆盖, dev-reload 同版接管; 无锁时按端口占用者是否为工作站进程兜底清理旧版遗留) |
| 2026-08-18 | iter-34 | auth 白名单补 `/` 仪表盘 HTML 入口 (auth_enabled 时 Web UI 可自举加载, 与 auth-token 同一信任假设; 回归测试锁定其余 API 仍 401) |
| 2026-08-17 | iter-32 | M5-2: /ws/worker Worker 事件直推端点 (mesh_token 鉴权 + usage_batch 幂等复用 + 通用事件转发 event_bus; 白名单收录) |
| 2026-08-16 | iter-30 | F1: 角色无关自动对齐 — config_ts 仲裁密钥收敛 (推/拉主从解耦) + 落后节点自动 git pull 升级 + 保存端点/周期对齐线程 |
| 2026-08-16 | iter-30 补 | E5: Secretary 离线故障转移 (prune 循环挂接管检查 + device_id 仲裁接任 + WS/Bot 通知) |
| 2026-08-16 | iter-30 补② | P1 #2: station_api 按路由分层拆分 (2594 行 → 装配层 + common + 7 路由域; 路由集合/行为不变, 兼容再导出保外部导入不破) |
| 2026-08-16 | iter-30 补③ | P2 #7: DB 启动自动备份 (sqlite3 在线快照 → ~/.lan_mesh/backups/, 留 3 代, 失败不阻断) |
| 2026-08-16 | iter-30 补④ | P3: 删除历史遗留 secretary.py/master.py 与 main.py secretary 入口; api.py 移除旧 Secretary 路由及专用模型; Orchestrator stub 随之下线 |
| 2026-08-16 | iter-28 | E4: 双 Secretary 冲突仲裁 (真实角色广播 + device_id 字典序让位) + role 落库 + 密钥接收端自愈 |
| 2026-08-16 | iter-27 后 | 初建；收录 S1/S2/S3 同步链路设计 |
