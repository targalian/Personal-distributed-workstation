# 02 Station 核心

系统骨架：进程控制器、路由层、基础设施管理器、数据库持久化。
设计原则: **Station Director 只管「机器」，Secretary 只管「项目」**。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| backup.py | Offline, verified backups of task data, conversations, configuration and execution receipts. |
| database.py | SQLite 数据库存储层 - Secretary 端主机注册记录持久化 |
| singleton.py | 主机级工作站单实例守护 (E6)。 |
| station_api.py | Station Director API 路由层 (装配入口) |
| station_controller.py | Station Director 独立控制器 — 基础设施管理入口 |
| station_director.py | Station Director (工作站主管) — 基础设施资源管理器 |
| station_routes_basic.py | Station 基础路由 — 健康/错误/角色/注册心跳/主机网络/Director (P1 #2 拆分产物) |
| station_routes_briefs.py | Project description editing, generation and portable Markdown export. |
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
| 2026-10-15 | iter-47 | 离线备份通过 SQLite backup API 同时收集主库与执行/启动回执；文件摘要和历史引用校验；恢复到新目录并保留中断标记 |
| 2026-10-15 | iter-46 | 主库事务同时登记 PM 与任务归属；重复任务创建不覆盖；启动诊断按当前状态更新；未绑定交付拒绝 |
| 2026-10-15 | iter-45 | Station 子任务执行与查询共用持久化回执路由；结果由原 PM 查询，不再误发给本机无关 PM |
| station_routes_common.py | 限流/认证中间件与共享工具 (单一事实源) | — |
| station_routes_basic.py | 健康/错误/角色/注册心跳/主机/Director | 始终 |
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
  兜底链路后续补报）；其他 type 转发 event_bus → 自动广播前端 /ws

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

**迁移机制**: `SCHEMA_VERSION` + `_MIGRATIONS` 字典（当前 **v4**）：
- v4: hosts 表新增 `code_version` / `version_ts` 列（S2/S3 版本统计）
- 迁移函数必须幂等（ALTER TABLE 包 try/except）；新增列的索引放迁移函数内

**关键接口**: `upsert_host()` / `list_hosts()` / `on_heartbeat` 相关 /
`record_usage()` / `backup()` (P2 #7) 等

**P2 #7 DB 自动备份**: `__init__` 末尾调用 `backup()` — sqlite3 在线
备份 API 一致性快照至 `~/.lan_mesh/backups/<stem>-<时间戳>.sqlite3`,
保留最近 3 代; 失败仅告警不阻断启动。

**依赖**: 无外部依赖（标准库 sqlite3）

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-10-15 | iter-44 | 子任务同步保留 error 诊断字段；终止失败及后续阻塞原因随状态落库 |
| 2026-10-15 | iter-43 | SQLite事务串行交付/验收；相同交付去重并保留结论、不同内容冲突；普通任务保存保护已有交付；PM完成状态条件更新 |
| 2026-10-15 | iter-42 | 已取消/暂停任务拒绝晚到交付；PM 状态更新不覆盖已取消/暂停状态 |
| 2026-10-15 | iter-41 | 交付接口拒绝不存在任务、冲突 PM 和空内容；落库成功后才返回确认 |
| 2026-10-14 | iter-40 | 下发接口传预分配任务 ID 和 input_data._brief；既有任务 ID 返回已有任务，任务库持久化需求依据 |
| 2026-10-10 | 项目描述工作区 | 新增对话产物、三层需求 DAG、版本保存、Markdown 导出与集成验证 |
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

## station_routes_briefs.py — 项目描述接口

由 station_api 装配，会话级读取、版本比较保存、模型整理与 Markdown 导出。结构和依赖由 project_brief.py 校验；详细接口见 [项目描述工作区](../06-interaction/项目描述工作区.md)。

`submit_task_from_chat` 接受可选 `task_id` 和 `input_data`。确认下发由 BriefHandoff 先记录再调用；任务库沿用 JSON 字段保存 `_brief`，无 schema 迁移。已存在任务 ID 直接返回，不覆盖原数据。此检查并非数据库级并发去重；同版本派发由活跃 Secretary 的 BriefStore 锁与准备记录协调。

交付接口在写入前检查任务存在（404）、已有 PM 关联不冲突（409）、非空字符串产物（422），保存成功后返回匹配任务 ID 的确认。iter-46 在启动前登记绑定，任务尚无 pm_agent_id 时也拒绝交付；此为任务归属校验，仍依赖现有可信内网认证。数据库写入失败不会返回成功。

iter-42：任务处于 cancelled/paused 时 `/deliver` 返回 409，不新增交付物；`/api/pm/{id}/status` 对覆盖已停止任务/PM 的晚到状态返回当前状态与 ignored=true，不广播新状态。该保护不撤销停止前已保存的产物，亦不是远程进程终止确认。

## 交付重试与验收事务（iter-43）

`Database.record_task_delivery` 在 SQLite `BEGIN IMMEDIATE` 事务内检查任务、PM 关联和已有交付，再更新 output_data。相同 PM、正文、摘要及子任务统计被视为相同交付；重试时间戳不改变初次交付时间。相同交付返回 duplicate=true 和当前 accepted，不重写数据、不重复广播/推送；不同内容返回 409，当前每个任务仅保存一份不可替换交付，修改后应建立新任务。已取消/暂停但已有相同交付时可确认旧记录，不接收新交付。

`review_task_delivery` 在同类事务中只更新交付评审字段和 accepted/rejected 状态，保留其他任务数据；重新验收会清除旧退回原因。Web 和聊天共用该方法。普通 save_task 在事务内合并已有交付并保留已评审或停止的任务状态，防止启动回执等旧对象整条覆盖交付。PM 完成/失败上报采用单条条件 UPDATE，仅修改仍为 running 的任务，避免查询后被用户验收再遭回写。

不需要 schema 迁移。事务锁依赖共享 SQLite 文件，只验证本机多连接/多线程，不声明网络文件系统上的跨机强一致。通知在提交之后执行，尚无可靠事件队列；回执丢失后的重试可以确认已落库数据，但不会补发首次失败的通知。没有新增修改交付版本、自动重新执行或多人审核权限。

iter-46 同时隔离交付保存后的 Bot 通知异常：通知失败记录告警，仍返回已落库的交付确认。没有引入通知 outbox 或补发保证。


## 离线完整备份（iter-47）

`backup.py` 根据显式根清单收集数据；CLI 自动选择状态目录、共享目录、主库与旁侧执行/启动回执及已存在配置。每个数据库使用 SQLite backup API 后 integrity_check，WAL 已提交内容进入独立快照，SHM 不作为数据或源变化信号。普通文件前后摘要与目录清单校验提供变化检测，但跨目录一致性仍要求所有写入者停止，不能称为在线一致性快照。

归档包含 SHA-256、大小、类型、可执行属性、根路径映射与空目录清单。校验严格核对归档成员，拒绝重复/越界/链接路径、超限体积、损坏 SQLite 及缺失的已提交文档历史。恢复先固定归档副本并完整验证，再写新目录；中断保留 RESTORE_INCOMPLETE，不自动覆盖生产数据或续跑任务。保留执行和启动记录，确保同编号重放不会因恢复丢失去重依据。备份早于某次执行时不能为该执行提供去重保证。

未改动自动启动备份；该旧机制仍只备份主库，不能替代本工具。归档不加密，敏感配置依目录权限保护；全量多机恢复、自动切换和定时备份仍未提供。具体范围、命令和手工切换步骤见 [备份恢复说明](../../backup-restore.md)。

## iter-48：任务核实 API

`GET /api/tasks/{task_id}/execution-check` 在线程池中读取原节点证据。内嵌 Worker 新增 `GET /role/pm-starts/{pm_id}?task_id=...`，与独立 Worker 共用只读查询。保留原 `/role/pm-status` 行为；沿用已有 Secretary 激活检查与 mesh 认证，查询不修改持久化状态。

## iter-49：停止事务与接口语义

数据库 `begin_task_control` 先保存停止意图并保护已有交付/终态，PM记录保持unknown直到确认；`finish_task_control` 仅接受当前请求编号，已确认不降级。普通任务保存保留 `_pm_control` 及归属，PM 状态更新在同一事务内再次检查停止状态。交互删除事务保留运行中或停止未确认记录。无数据库 schema 迁移。

取消/暂停路由在线程池执行并返回 200（确认）或 202（待核实）；不能操作终态返回409。Station 和 Worker 装配同一控制路由，旧路径保留但必须提供身份。

## iter-50：Secretary 重新激活时保留执行现场

`mark_tasks_for_recovery` 在单个写事务内标记全部活动任务为重启后待核实，不再按状态仅处理100条，也不通过整条任务回写覆盖并发交付。Secretary 在对外标记 active 之前完成此步骤；失败则不进入就绪状态。保留任务状态、PM归属、子任务与需求输入，已交付/终态/停止意图不重新分类。

旧 interrupted 且有PM归属的任务恢复为running，以允许原PM继续交付；旧中断原因移入 `_pm_recovery.legacy_interruption`。新的原PM运行状态或交付清理待核实标记。普通保存不能移除恢复标记或改变原归属；交互删除保留待核实/旧中断记录。缺少PM归属的历史活动任务不能直接得到“尚未派发”的停止确认。
