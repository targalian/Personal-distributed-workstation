# 02 Station 核心

系统骨架：进程控制器、路由层、基础设施管理器、数据库持久化。
设计原则: **Station Director 只管「机器」，Secretary 只管「项目」**。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| database.py | SQLite 数据库存储层 - Secretary 端主机注册记录持久化 |
| runtime_trace.py | 运行时追踪与性能审计 — P0/P1 运行时分析引擎 |
| shadow_dev.py | 影子开发模式 — 让 PM/CLI Agent 在仓库副本上自主开发, 产出 diff 供人审。 |
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
| station_routes_shadow.py | 影子开发 API 路由 - 提交、查询与守护状态接口。 |
| station_routes_tasks.py | Station 任务路由 — Agent 管理/任务生命周期/图结构/交付闭环/任务记忆 (P1 #2 拆分产物) |
| station_routes_worker.py | Station Worker 侧路由 — 内嵌 Worker 端点/P2P 通讯/云存储同步 (P1 #2 拆分产物) |
<!-- /AUTO:module-list -->
---

## shadow_dev.py - 自举安全开发 (iter-70)

**职责**: 把 CLI Agent 的自主开发收敛到仓库外影子副本, 主仓库全程只读;
产出 `changes.patch` 与 `report.json`, 由人工审核后应用, 不自动提交/推送。

**三道护栏**:
1. **目录隔离** - `_handle_cli_agent` 必须命中 `shared_folder` 或
   `CLI_AGENT_ALLOWED_ROOTS`; 主仓库默认拒绝, 只有
   `CLI_AGENT_ALLOW_SELF_REPO=1` 可显式放行。CLI 子进程仅获得最小环境
   (后端凭据 + 平台/网络变量), 并禁用 Git 全局凭据配置。
2. **副本隔离** - 影子副本跳过 `.env`、`model_pool.yaml`、`config.yaml`
   等本地敏感配置与全部符号链接, `SHADOW_DEV_HOME` 必须位于主仓库外。
3. **产出门禁** - 副本内执行编译、导入、pytest、`sync_docs`、护栏文件
   复核与新增行密钥扫描; 任一失败只能得到 `GATES_FAILED`, diff 保留供排查。

**不变式**: `SELF_MOD_FORBIDDEN` 覆盖 runtime 安全策略、影子模式、协作锁、
上库/发货脚本、Git hooks 与护栏测试; 这些文件出现在影子 diff 中即拒绝。

**常驻守护与 API (iter-71)**: `ShadowDevManager` 在 Station 启动时创建
并运行单队列守护线程; `POST /api/shadow-dev/runs` 提交任务后立即返回
202, `GET /api/shadow-dev/runs` / `GET /api/shadow-dev/runs/{run_id}`
查询队列与报告, `GET /api/shadow-dev/status` 查看守护状态。守护串行执行,
停止 Station 时未开始的排队任务会被取消, 不强制中断已进入 CLI 的任务。

## station_controller.py - Station 控制器（核心枢纽）

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

**PM 断点恢复 (iter-53)**:
- `_local_resume_pm(task_id)`: 重建 PM Agent 并从快照续跑 — 校验本机
  PM 未运行/chat_runtime 就绪/快照存在后重建, `resume_from_snapshot`
  失败回滚; 任务状态回到 running
- `_recover_stale_tasks()`: activate_secretary 末尾恢复扫描 — stale
  状态 (running/monitoring/planning/executing/awaiting_input/paused)
  有快照自动续跑, 无快照标记 interrupted
- 端点: `POST /api/tasks/{task_id}/resume` (手动恢复, 无快照 404/
  冲突 409); `POST|GET|DELETE /api/pm/{pm_id}/snapshot` (快照落库通道)

**日志容量修剪 (iter-54, 补强#2)**:
- `_prune_logs_if_due()`: `_prune_loop` 每轮节流调用 — 保留期/周期
  由 `observability.log_retention_days/log_prune_interval_hours` 驱动
  (默认 30 天/24h, ≤0 禁用), 无论成败推进时间戳防风暴
- 修剪后按 `log_vacuum` 开关执行 VACUUM 回收磁盘空间
- 手动端点: `POST /api/runtime/logs/prune?days=` (运维/排查, 1~365 夹取)

**多机实测加固 (iter-55, 补强#3)**:
- `_load_model_resources()`: 任何 station 模式启动即预加载模型池 +
  resources.yaml 注入 — 让位主机 (网络中已有 Secretary, 本机未激活)
  作为远程派发 Worker 执行 PM 任务时 LLM Key 就绪, 与激活解耦;
  `activate_secretary` 复用 `self._model_pool` 避免重复加载
- `_local_start_pm`/`_local_resume_pm`: `chat_runtime` 为 None 时惰性
  初始化 Worker AgentRuntime (agent_id=`worker-{device_id[:8]}`),
  修复让位主机远程派发被拒「AgentRuntime 未初始化」
- 实测结论: 双实例隔离模拟跨机链路全通 (S 提交 → W 让位主机执行 →
  WS 直推用量 → S 落库 ark 池), WS 直推/HTTP 兜底/幂等去重均验证通过

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
  auth 开启时页面必须先加载才能执行 auth-token 自举; 注册引导/版本查询/secrets 拉取;
  iter-56 起含 `/spa` React SPA 静态资源 — 与 `/` 同一信任假设, 页面自举后
  apiFetch 再取 auth-token）
- 限流双桶 (iter-57, 补强#5): `_RateLimiter` per-IP 滑动窗口分
  信任/严格双桶 — 携带合法 mesh token (verify_token 恒定时间比较)
  走信任桶高阈值 (`observability.api_rate_limit_trusted`, 默认 1000/min,
  覆盖 20 并发任务 + UI 轮询), 未认证流量走严格桶
  (`api_rate_limit`, 默认 120/min) 防滥用; 阈值 ≤0 禁用对应桶,
  set_limits 变化时清空窗口历史命中; auth 未启用时全部视为信任
- 任务排队接力 (iter-57, 补强#5): 本机 PM 单实例忙且无远程 worker
  时, POST /api/tasks 不再自调用远程派发 (压测发现 20 并发提交
  阻塞超时 + 19 任务瞬时 failed) — 任务保持 pending + 响应带
  `queued: true`; PM 上报 completed/failed/cancelled 时触发
  `_dispatch_queued_task` 接力派发最早 pending 任务 (PM 仍收尾时
  后台线程等待其空闲, 最多 120s)
- 多用户权限 (iter-58, 补强#6 F5.2): `security.users` 配置驱动用户表
  (name/role/token, 非法 role 归 viewer, 空 token 跳过, 空表 = 关闭
  多用户向后兼容); 中间件 `resolve_role` 判定 (mesh token → boss,
  用户 token 恒定时间比较 → 角色) 后按 `_check_role_access` 分级:
  boss 全权 / operator 写放行 (管理员前缀 `/api/station/ /api/runtime/`
  `/api/secrets/ /api/version/ /api/resources/ /api/network/ /api/agents/`
  写仅 boss) / viewer 与未登录仅 GET/HEAD/OPTIONS; `configure_users`
  由 station_controller 启动注入; `/api/station/auth-token` 收紧 —
  多用户模式下仅 boss 身份可获得 mesh_token (防低角色提权),
  未登录回显空角色
- mesh 认证态与 token 访问器 `get_mesh_auth_token()` 位于 common
  (原闭包全局迁移, 避免跨模块引用失效)
- 用户管理持久化 (iter-63, 团队场景): DB 迁移 v9 `users` 表
  (name/role/token_hash/token_tail4/created_at/updated_at) + CRUD 方法
  (list/upsert/update_role/delete); token 仅存 SHA256 哈希 (内存与 DB
  均不存明文, `_hash_token`/`secrets.compare_digest`); config 种子降级为
  首次导入 (DB 空时才写入, 之后以 DB 为准 → 轮换跨重启保留);
  5 个管理端点 (GET/POST /api/station/users,
  PUT .../{name}/role, POST .../{name}/rotate-token, DELETE .../{name})
  由中间件角色检查完成 (写仅 boss); `_last_boss_guard` 防自锁 (唯一
  boss 不可降级/删除, 升权不受限); 创建/轮换返回明文 token 仅一次
- station_api.py 兼容再导出 common 的中间件/工具，station_controller /
  worker 的既有导入路径不变
- `/api/secrets/receive` 接收端自愈: 解密失败且报 mesh_token 不匹配时,
  自动从推送方（请求来源 IP + 报文 src_port）收敛信任根后重试一次
  （`_heal_mesh_token_from`，E4）
- `/api/version/upgrade-notice` 收到领先通知时触发 `_auto_upgrade`
  自动升级（F1，工作区脏则安全跳过）

## 集群调度 (F3.1/F3.3) 与节点间派发协议 (iter-66 三机实测收敛)

**定位**: >2 节点集群的自动扩缩容与 PM 故障迁移, iter-66 三机集群
(1 Secretary + 2 Worker + mock LLM) 真机实测背书 (17/17)。

**节点间派发协议** (`_dispatch_task_to_worker`, F3.1/F3.3 共用):
- 必须携带 `auth_headers()` (Bearer mesh_token): Worker 端
  `/role/start-pm` 不在 `_AUTH_WHITELIST`, 认证启用时缺头 401 静默失败
- 必须携带 `task_data` (任务完整 dict): 任务仅存于 Secretary DB,
  Worker 本地查不到任务, 缺时 start-pm 409「无法获取任务详情」
- 派发成功即置 running 并回写 `pm_agent_id` (防重复派发)
- 映射 `_pm_worker_map[pm_id]` 统一含 `task_id` 键 (迁移/扩容/忙判定
  依赖), 取消成功路径 pop 清理 (防 `_is_worker_busy` 误判阻塞扩容)

**F3.1 自动扩缩容**: `_autoscaler_loop` 30s 轮询 —
队列 pending ≥ 1 且存在空闲在线 worker → `_next_pending_task` FIFO
取最早任务 (DESC LIFO 饥饿修复) → 派发。
(iter-67 Bug J: 门槛从水位式 `>= up_threshold` 改为 `>= 1` — 原水位
导致「最后 1 单滞留 pending」与「新 Worker 上线不接活」调度滞后;
每轮仅派发 1 个 + 30s 轮询承担防抖动。)
(iter-68 批量清空: 单轮 while 连续派发直至队列空/无空闲 Worker —
原「每轮 1 个」在积压 N 单时需 N×30s 滞后 (五节点实测 4 积压 120s+),
改为每次派发后重查队列与空闲 Worker, 派发失败/队列未减即停止本轮
防死循环; 五节点实测 4 积压 18s 内全部 running。)

**F3.3 PM 迁移**: `_prune_loop` 5s → `prune_offline(device_ttl=8)` →
`_migrate_orphaned_pms` 按 `device_id` 匹配孤立 PM → 任务重置 pending
(`save_task` upsert 语义, 无 `upsert_task`) → 排除离线/忙碌后精确派发
替代 Worker, 无可用时本机接管。

(iter-67 Bug K: autoscaler 派发成功路径补落 `pm_agents` 表 — 与其余
5 处派发路径对齐, 运维查询任务承载与 victim 定位依赖该表。)

(iter-69 Bug L: 本机接管 `_start_local_pm_for_task` 原自行构造
`ProjectManagerAgent` 且沿用早期签名 `task=/runtime=`, 与现签名
`(pm_id, agent_runtime, secretary_url, device_id, device_name)` 不符 —
七节点实压中 6 Worker 全灭走接管分支即 TypeError, 任务停在 pending 无人
推进。改为复用唯一入口 `_local_start_pm` (含 runtime 懒初始化 +
`start_task` 真正启动), 并把落库/映射/广播抽为 `_register_local_pm`
与接力派发共用; 接管返回 bool, 失败时任务保持 pending 由下轮扩容兜底。)

**控制命令端点** (cancel/pause/delete, Bug I): 端点保持 `async def`,
但跨节点阻塞调用丢 `run_in_threadpool` 执行 — 同步执行会阻塞事件循环
→ Worker 状态上报请求无法进入 → 跨节点死锁级联 (S 等 W2 响应, W2 卡在
report_status 等 S) ; 同时控制命令 http_post `retries=1` (默认 3 次
重试 + 退避在死锁场景放大超时至 40s+)。

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
skill_assignments、resource_usage_log、events、pm_snapshots（iter-53
PM 执行态快照, 断点恢复数据源）等。

**迁移机制**: `SCHEMA_VERSION` + `_MIGRATIONS` 字典（当前 **v5**）：
- v5: llm_call_log 审计表 (运行时 LLM 调用性能追踪)
- v4: hosts 表新增 `code_version` / `version_ts` 列（S2/S3 版本统计）
- 迁移函数必须幂等（ALTER TABLE 包 try/except）；新增列的索引放迁移函数内

**关键接口**: `upsert_host()` / `list_hosts()` / `on_heartbeat` 相关 /
`record_usage()` / `insert_llm_call()` / `query_llm_metrics()` /
`backup()` (P2 #7) / `save_pm_snapshot()` 系列 (iter-53 快照 UPSERT/
按任务查找/删除, delete_task 级联清理) / `prune_logs()` + `vacuum()`
(iter-54 日志容量修剪, 按保留期清理日志表并回收磁盘空间) 等

**并发加固** (iter-57, 补强#5): `_get_conn` 每线程独立连接 +
`PRAGMA busy_timeout=30000` (并发写锁等待 30s, 避免 database is
locked) + `PRAGMA journal_mode=WAL` (读写不互斥, 文件系统不支持时
降级默认 journal 不阻断启动)；真机压测 20 线程 × 1800 请求零异常。

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
- 错误追踪闭环接线 (iter-44, F1.4 后半): `start()` 装配 `error_tracker` 双回调 — 全局事件回调 → event_bus `error_captured` (WS 实时刷 Dashboard 错误面板); 突发告警 (窗口内超阈 + 冷却到期) → event_bus `error_burst` + Bot `error_burst` 模板; 接线异常隔离不影响启动
- 错误自愈诊断 (iter-46, F4.2 首层): `/api/errors/diagnosis?window=` 按 `DIAGNOSIS_RULES` 模式规则表分组缓冲错误 (超时/连接/认证/限流/上游5xx, 首命中归属防重复计数, 命中数降序), 返回命中数/影响模块/建议文案/动作标识 + 未命中计数; window 夹取 1~500
- 错误记录落盘持久化 (iter-47, F1.4 补齐): Database v6 迁移新增 `error_log` 表 (新库 executescript + 旧库迁移双补, module+timestamp 索引); `save_error_record()` 写入 (context JSON 化, 容量修剪保留最近 2000 行), `query_error_history()` 倒序查询 (模块过滤, limit 夹取 1~500); `start()` 装配 `error_tracker.set_persist_callback` 落盘 (异常隔离); 端点 `/api/errors/history?limit=&module=` 跨重启可读
- 历史诊断扩展 (iter-48, F4.2): `/api/errors/diagnosis` 增 `source=history` 参数 — 改诊断 `error_log` 持久化记录 (默认 `buffer` 行为不变), 规则匹配抽为模块级纯函数 `diagnose_records()` 双源复用; 重启后缓冲空仍可诊断落盘历史不断档
- 自愈动作执行 (iter-49, F4.2 修复环节): `run_heal_action(action, category)` 执行器 — 仅注册安全只读动作 (`check_peer` 向已知设备 UDP 探测 / `probe_balances` 资源池余额探测), 未注册动作返回 `manual_required`; 结果落盘 `heal_log` (Database v7 迁移, 容量修剪 500 行) + event_bus `heal_action` 广播; 端点 `POST /api/errors/heal?action=&category=` (rotate_key/switch_pool 映射为 probe_balances) + `GET /api/errors/heal/history?limit=`
- 自动自愈守护 (iter-50, F4.2 自动化环节): `_auto_heal_loop()` 守护线程周期扫描诊断缓冲 — 仅 `_AUTO_HEAL_ACTIONS` 安全动作 (check_peer/rotate_key/switch_pool) 自动执行, 同类别冷却去重防风暴 (config.yaml `observability.auto_heal_*` 驱动, 默认关/300s 周期/600s 冷却/周期最小 30s, 异常隔离); 端点 `GET /api/errors/heal/status` (开关/周期/冷却/累计轮次/最近动作) + `POST /api/errors/heal/auto-check` (手动触发一轮与守护同逻辑)
- DAG 图结构读写恢复 (iter-51, F4.3): `get_task_graph_data()` 读图 (checkpoint dag_json 优先 + 子任务列表 TaskDAG 重建, GET 端点复用) + `update_task_graph()` 写图 (任务存在 + 仅 pending 可编辑 + TaskDAG.from_graph_json 环检测拒绝, 落盘子任务列表 + checkpoint dag_json 同步); `PUT /api/tasks/{task_id}/graph` 编辑端点恢复 (Orchestrator 废弃后重接 DB 路径, 缺字段 400/环与状态 409, 成功 event_bus `task_graph_updated` 广播)
- 成本感知调度接入 (iter-52, F4.4): 双提交入口 (`POST /api/tasks` 与 `submit_task_from_chat`) 提交时调用 budget_advisor 预估落盘 `input_data._cost_estimate`, tight/insufficient 时 Bot 推送 + event_bus `cost_budget_warning` WS 广播 (异常静默不阻断); 新端点 `GET /api/tasks/{task_id}/cost-estimate` (秘书激活 503 护栏, 404 任务不存在, 返回实时预估+适配+落盘快照)

**P2 #7 DB 自动备份**: `__init__` 末尾调用 `backup()` — sqlite3 在线
备份 API 一致性快照至 `~/.lan_mesh/backups/<stem>-<时间戳>.sqlite3`,
保留最近 3 代; 失败仅告警不阻断启动。

**依赖**: 无外部依赖（标准库 sqlite3）

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-29 | iter-69 | F3.3 本机接管路径修复 (七节点实压 Bug L): `_start_local_pm_for_task` 自构 PM 用早期签名 (task=/runtime=) 必然 TypeError, 全 Worker 离线时接管失败任务滞留 pending; 改为复用 `_local_start_pm` 唯一入口 + 抽出 `_register_local_pm` 统一落库/映射/广播 (接力派发共用), 接管返回 bool 失败留 pending 由下轮扩容兜底; 专项 7/7 + 回归 380 passed |
| 2026-08-29 | iter-68 | F3.1 扩容同轮批量清空 (30s/轮×N 积压滞后修复): _autoscale_check 单轮 while 连续派发 (每次派发后重查队列与空闲 Worker, 失败/未减即 break 防死循环) + _dispatch_next_task_to_worker 返回 bool; 五节点真机 14/14 (同轮清空耗时 18s vs 旧 120s+ 滞后) + 专项 16/16 + 回归 373 passed |
| 2026-08-29 | iter-67 | 五节点集群实压 (评估报告边界 #2 五实例模拟): Bug J 扩容门槛水位→>=1 (最后 1 单不滞留/新 Worker 上线即接活) + Bug K autoscaler 派发落 pm_agents 表; 真机 13/13 (五机互认/4 积压全部派发/深度 4→3→2→1/FIFO/4 Worker 各 1 任务无重复/5 任务并发/杀机 F3.3) + 回归 371 passed |
| 2026-08-29 | iter-66 | F3.1/F3.3 三机集群实测背书 (评估报告剩余边界 #2): 节点间派发协议收敛 (auth_headers + task_data + 映射含 task_id + 取消清映射 + 派发即置 running + FIFO) 共修复 9 个真实 bug (A-I); 控制命令端点 run_in_threadpool 解除跨节点死锁级联; 真机 17/17 + 专项 12/12 + 回归 369 passed |
| 2026-08-29 | iter-57 | 并发压力验证 (补强#5): DB 加固 busy_timeout 30s + WAL (每线程独立连接); 限流双桶 (信任桶 token 高阈值/严格桶防滥用, 阈值配置化 observability.api_rate_limit[_trusted], ≤0 禁用); 本机 PM 忙时任务排队 pending 而非瞬时 failed, PM 结束接力派发 (_dispatch_queued_task); /api/health 补登白名单; 真机压测 1800 req 0 错误 + 20 并发提交全 200 (1 running + 19 排队) |
| 2026-08-29 | iter-58 | 多用户权限 (补强#6 F5.2): security.users 配置驱动用户表 + 中间件角色分层 (boss/operator/viewer) + auth-token 收紧 (仅 boss 获 mesh_token); SPA 角色徽章/登录面板/viewer 只读; 真机 API 13 项 + Browser 5 步实测通过 (UI-050), 发现并修复未登录误显 boss 竞态与退出后 DAG 可编辑两缺陷 |
| 2026-08-29 | iter-63 | 用户管理持久化 (团队场景): 迁移 v9 users 表 + token 哈希存储 (不存明文) + config 首次种子 + 5 管理端点 + 最后 boss 防自锁; 真机 16 项 + 重启持久化 (轮换 token 跨重启保留) + Browser 12/12 (UI-053) 全过 |
| 2026-08-29 | iter-56 | F5.1 React SPA (补强#4): /spa 挂载 StaticFiles(html=True) + 认证白名单放行 /spa 前缀 + preflight _check_spa_bundle; 三页面 (Station 总览/任务列表/DAG 编辑器) 数据链路 Browser 实测通过 (UI-049) |
| 2026-08-29 | iter-55 | 多机实测加固 (补强#3): _load_model_resources 启动预加载模型池 (让位主机 Key 就绪); _local_start_pm/_local_resume_pm 惰性初始化 Worker AgentRuntime; 双实例隔离跨机链路实测通过 |
| 2026-08-28 | iter-54 | 日志容量修剪 (补强#2): Database.prune_logs (llm_call_log/chat_history/resource_usage_log 仅删已上报/progress_reports/heartbeat_log 固定 24h) + vacuum; _prune_logs_if_due 节流接入 _prune_loop; POST /api/runtime/logs/prune 手动端点 |
| 2026-08-28 | iter-53 | PM 断点恢复: pm_snapshots 表 + save_pm_snapshot CRUD; _local_resume_pm + _recover_stale_tasks 快照自动续跑; /api/pm/{id}/snapshot 三端点 + POST /api/tasks/{id}/resume 恢复端点 |
| 2026-08-26 | iter-39 | P3 任务流总览: task_flow_overview 多任务聚合 (末阶段/终态判断/末活动倒序); /api/runtime/task-flow-list 端点; Dashboard 运行时 Tab 总览表 + 一键查瀑布 (UI-037) |
| 2026-08-26 | iter-40 | P3 任务停滞检测: task_flow_overview 增 idle_ms/stalled (stall_minutes 阈值, 终态免疫, ≤0 禁用); 端点参数透传与夹取 (0~1440); Dashboard 状态列三态 + 红色告警横幅 (UI-038) |
| 2026-08-26 | iter-41 | P3 任务停滞主动告警: check_stall_alerts 档位去重 (1/2/4 倍阈值 Lv1/2/3, 仅升级重推, 恢复清档) + 60s 守护线程; event_bus task_stall_alert → WS toast + 总览表自动刷新 + Bot 三档模板; 告警查询/手动检查端点 (UI-039) |
| 2026-08-26 | iter-42 | F4.1 任务记忆面板: /api/task-memory/overview 端点 (全局统计 + 按类型分组 + 最近沉淀); Dashboard 运行时 Tab 记忆面板 (统计卡片/分组表/最近列表, 503 优雅降级) (UI-040) |
| 2026-08-27 | iter-43 | 停滞检测参数配置化: config.yaml observability 段 (stall_check_interval/stall_minutes) 驱动守护线程, 缺省回退 60s/30min, ≤0 禁用 |
| 2026-08-27 | iter-46 | F4.2 异常自愈首层: /api/errors/diagnosis 模式规则表诊断端点 (分组建议 + 未命中计数) |
| 2026-08-27 | iter-47 | F1.4 错误记录落盘持久化: database v6 迁移 error_log 表 + save_error_record/query_error_history (容量修剪 2000 行) + start() 落盘回调接线 + /api/errors/history 端点 (跨重启保留) |
| 2026-08-27 | iter-48 | F4.2 诊断范围扩展: /api/errors/diagnosis 增 source=history (诊断 error_log 落盘记录) + diagnose_records 纯函数抽取双源复用, 重启后诊断不断档 |
| 2026-08-27 | iter-49 | F4.2 自愈动作执行 (修复环节): run_heal_action 执行器 (check_peer/probe_balances 安全动作 + 未注册返回 manual_required) + database v7 迁移 heal_log 表 (容量修剪 500 行) + /api/errors/heal 端点 (动作映射) + /api/errors/heal/history 历史端点 |
| 2026-08-28 | iter-50 | F4.2 自动自愈守护: _auto_heal_loop 周期扫描 + _auto_heal_once 冷却去重 (默认关, config.yaml observability.auto_heal_* 驱动) + /api/errors/heal/status 状态端点 + /api/errors/heal/auto-check 手动触发 (UI-046) |
| 2026-08-28 | iter-51 | F4.3 自然语言 DAG 编辑: get_task_graph_data/update_task_graph 读写图方法 (仅 pending 可编辑 + 环检测) + PUT /api/tasks/{id}/graph 编辑端点恢复 (Orchestrator 废弃后重接 DB) + 秘书自然语言编辑意图 (UI-047) |
| 2026-08-28 | iter-52 | F4.4 成本感知调度: 双提交入口预算预估落盘 (_cost_estimate) + cost_budget_warning 广播 + GET /api/tasks/{id}/cost-estimate 预估端点 |
| 2026-08-27 | iter-44 | F1.4 错误追踪闭环: start() 装配 error_tracker 双回调 (error_captured → WS 实时刷面板; error_burst → 事件总线 + Bot 突发告警, 冷却去重); Dashboard 错误追踪面板 (UI-041) |
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
