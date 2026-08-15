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

**S1/S3 密钥与版本同步**（本模块近期核心增量）:
- `push_resource_secrets()`: Secretary 将 resources.yaml 加密推送到在线节点
- `_startup_sync_once()`: 启动一次性同步（发现层 + DB 双通道查对端 →
  等选举 → 版本领先检测 + 密钥推/拉），**替代 60s 轮询**
- `pull_resource_secrets()`: 非 Secretary 节点向 Secretary 拉取密文，
  解密 → 指纹校验 → 幂等跳过/落盘热重载
- `_sync_with_new_peer()`: 新主机入网即时同步（免轮询）
- `activate_secretary()` 激活后兜底推送一次（覆盖选举晚于启动同步的时机差）
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
  `/api/secrets/fetch`（S3 拉取端点）、`/api/version/*`（S2）
- Secretary 路由（`secretary_active` 为真才可用，否则 503）: 任务/Agent/
  项目/MCP 工具/模型路由/聊天/`/api/secrets/sync-all`（手动推送）
- `/ws`: WebSocket 实时推送（event_bus sink 装配于此）

**设计要点**:
- 所有组件经 `controller` 可变引用访问，支持免重启激活/停用 Secretary
- `_AUTH_WHITELIST`: 免认证端点白名单（注册引导/版本查询/secrets 拉取）
- `/api/secrets/receive` 接收端自愈: 解密失败且报 mesh_token 不匹配时,
  自动从推送方（请求来源 IP + 报文 src_port）收敛信任根后重试一次
  （`_heal_mesh_token_from`，E4）

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
| 2026-08-16 | iter-28 | E4: 双 Secretary 冲突仲裁 (真实角色广播 + device_id 字典序让位) + role 落库 + 密钥接收端自愈 |
| 2026-08-16 | iter-27 后 | 初建；收录 S1/S2/S3 同步链路设计 |
