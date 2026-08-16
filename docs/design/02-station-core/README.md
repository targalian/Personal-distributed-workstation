# 02 Station 核心

系统骨架：进程控制器、路由层、基础设施管理器、数据库持久化。
设计原则: **Station Director 只管「机器」，Secretary 只管「项目」**。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| database.py | SQLite 数据库存储层 - Secretary 端主机注册记录持久化 |
| master.py | Master 占位模块 — 历史遗留空文件, 保留以兼容旧引用 (职责已并入 Station Director)。 |
| secretary.py | Secretary Controller - 中心控制节点 |
| station_api.py | Station Director API 路由层 |
| station_controller.py | Station Director 独立控制器 — 基础设施管理入口 |
| station_director.py | Station Director (工作站主管) — 基础设施资源管理器 |
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

## station_api.py — 路由层（最大文件 115KB）

**职责**: 全部 HTTP/WebSocket 端点定义。

**路由分层**:
- 基础路由（始终可用）: 主机注册/心跳/查询、角色激活、bootstrap-token、
  `/api/secrets/fetch`（S3 拉取端点，F1 起附带 `config_ts`）、
  `/api/secrets/sync-all`（F1 起角色无关对齐，任意节点可用）、
  `/api/resources/config`（F1 起放开 Secretary 限制，任意节点可保存后
  自动全网对齐）、`/api/version/*`（S2）
- Secretary 路由（`secretary_active` 为真才可用，否则 503）: 任务/Agent/
  项目/MCP 工具/模型路由/聊天
- `/ws`: WebSocket 实时推送（event_bus sink 装配于此）

**设计要点**:
- 所有组件经 `controller` 可变引用访问，支持免重启激活/停用 Secretary
- `_AUTH_WHITELIST`: 免认证端点白名单（注册引导/版本查询/secrets 拉取）
- `/api/secrets/receive` 接收端自愈: 解密失败且报 mesh_token 不匹配时,
  自动从推送方（请求来源 IP + 报文 src_port）收敛信任根后重试一次
  （`_heal_mesh_token_from`，E4）
- `/api/version/upgrade-notice` 收到领先通知时触发 `_auto_upgrade`
  自动升级（F1，工作区脏则安全跳过）

## secretary.py — 旧版中心控制器（历史遗留）

早期独立 Secretary 进程实现（UDP 发现 + 注册 + SQLite + Web UI），
能力已被 station_controller + station_api 取代。保留供考古，
新代码一律走 station_* 三件套。

## master.py — 空占位文件（待清理）

历史遗留文件（原 0 字节，现仅补模块 docstring 满足钩子检查），
保留供考古，清理时需确认无 import 引用。

## database.py — SQLite 存储层

**职责**: Secretary 端全部持久化（类名 `Database`）。

**表结构**: hosts（主机 + 版本列）、tasks/subtasks、chat_history、skills、
skill_assignments、resource_usage_log、events 等。

**迁移机制**: `SCHEMA_VERSION` + `_MIGRATIONS` 字典（当前 **v4**）：
- v4: hosts 表新增 `code_version` / `version_ts` 列（S2/S3 版本统计）
- 迁移函数必须幂等（ALTER TABLE 包 try/except）；新增列的索引放迁移函数内

**关键接口**: `upsert_host()` / `list_hosts()` / `on_heartbeat` 相关 /
`record_usage()` 等

**依赖**: 无外部依赖（标准库 sqlite3）

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-30 | F1: 角色无关自动对齐 — config_ts 仲裁密钥收敛 (推/拉主从解耦) + 落后节点自动 git pull 升级 + 保存端点/周期对齐线程 |
| 2026-08-16 | iter-30 补 | E5: Secretary 离线故障转移 (prune 循环挂接管检查 + device_id 仲裁接任 + WS/Bot 通知) |
| 2026-08-16 | iter-28 | E4: 双 Secretary 冲突仲裁 (真实角色广播 + device_id 字典序让位) + role 落库 + 密钥接收端自愈 |
| 2026-08-16 | iter-27 后 | 初建；收录 S1/S2/S3 同步链路设计 |
