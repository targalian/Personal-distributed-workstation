# 01 网络与发现

局域网组网的底座层：UDP 广播发现、协议数据结构、节点认证、HTTP 重试。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| auth.py | 节点间通信认证 — 轻量级 Shared Token 机制 |
| discovery.py | UDP 广播局域网设备发现 - 参考 QuickLAN 的 DiscoveryService |
| http_retry.py | 内部 HTTP 通信重试工具 |
| protocol.py | 协议定义 - 端口常量、发现数据包、主机信息模型 |
<!-- /AUTO:module-list -->
---

## discovery.py — UDP 广播设备发现

**职责**: 定期 UDP 广播 presence 包（携带设备身份 + 角色 + 配置摘要 + 代码版本），
监听其他设备广播并维护设备列表，TTL 超时标记离线。

**设计要点**:
- 参考 QuickLAN DiscoveryService；区别在于携带硬件摘要且采用 Secretary/Worker 角色模型
- 发现包由 `protocol.DiscoveryPacket` 序列化（JSON）
- S2 起发现包携带 `code_version` / `version_ts`（git commit 与提交时间戳），
  支撑跨主机版本统计
- `_on_device_seen`（在 station_controller 中）是发现事件的唯一汇聚点：
  轻量心跳落库、首次发现自动注册、新入网即时密钥同步均挂在此处

**关键接口**: `DiscoveryService.start()` / `list_devices()` / 回调注入

**依赖**: protocol, config, logger

## protocol.py — 协议与数据模型

**职责**: 全项目共享的协议常量与数据结构单一事实源。

**核心结构**:
- `DiscoveryPacket`: device_id / device_name / role / ip / api_port /
  code_version / version_ts 等（S2 新增版本字段）
- `HostInfo`: 主机完整画像（硬件 + 共享目录 + 版本字段）
- `HostRecord`: 数据库行模型（含 code_version / version_ts，S2/S3 落库用）

**设计要点**: 新增跨节点字段时必须**同时**更新 DiscoveryPacket、HostInfo、
database.py 迁移三处，缺一不可（S2 迭代教训）。

**依赖**: 无（纯数据定义）

## auth.py — mesh_token 认证

**职责**: 局域网节点间轻量认证。所有节点共享一个 mesh_token（32 字节 hex），
内部 API 请求携带 `Authorization: Bearer <token>`。

**设计要点**:
- 默认启用（config.yaml `security.auth_enabled`，P2 #5 起默认 true，可显式关闭）
- 安全边界: 仅防未授权设备误接入，不替代 TLS；token 经明文 HTTP 注册引导下发
  （局域网信任假设，全项目一致）
- mesh_token 同时是密钥同步（secret_sync）的 HKDF 信任根

**关键接口**: `get_mesh_token()` / `verify_token()` / `AuthDependency`

## http_retry.py — HTTP 重试工具

**职责**: 节点间（PM ↔ Secretary ↔ Worker）HTTP 调用统一封装。

**设计要点**:
- 指数退避重试（默认 3 次，0.5s → 1s → 2s），仅对网络错误 / 5xx 重试，4xx 不重试
- 自动附加 mesh_token（`set_auth_token` 全局设置）
- 线程安全，无全局状态

**关键接口**: `http_get()` / `http_post()` / `set_auth_token()`

**主要使用方**: station_controller（注册/推送/拉取）、pm_monitor（进度轮询）、
orchestrator（任务分发）

## station_controller.py — 跨网段联邦 (F3.4, iter-64)

**职责**: 静态 peer 联邦发现层 — 跨网段(不同 UDP 广播域)的 Secretary 节点互相
感知、主机记录互通、离线检测。任务跨网段转发留待 iter-65。

**配置** (config.py `federation`):
- `enabled` / `interval` (轮询秒) / `offline_after` (连续失败 N 次置离线)
- `peers`: 静态联邦对端列表 `{name, host, port}` (name 即联邦名, 用于主机来源标记)

**机制**:
- `_federation_loop` 轮询线程: 定期拉取对端 `GET /api/federation/info`
  (mesh token 认证) → 对端自身 + 对端网段主机写入 hosts 表 (source=fed,
  federation=peer.name) → 连续失败 offline_after 次标记该联邦主机离线
- 防自环: 转播时跳过 device_id 与本机相同的主机
- 选举/仲裁隔离: `_find_existing_secretary` / `_secretary_failover_check`
  仅查询 `list_hosts(source="lan")` — 联邦远端 Secretary 不参与本网段仲裁,
  各网段 Secretary 联邦共存
- DB 迁移 v10: hosts 表新增 source / federation 列

**端点** (station_routes_basic): `GET /api/federation/info` — 本机身份
(device_id/device_name/role/api_port/secretary_active/代码版本) + 网段主机摘要,
供对端联邦节点轮询

## station_controller.py — 联邦任务跨网段转发 (F3.4 遗留, iter-65)

**职责**: 任务层联邦 — 本网段无法执行的任务委任给对端网段 Secretary 全权接管。

**选站分层** (`_pick_task_host`):
- lan 优先: 本网段主机 (source=lan) 按评级 + 负载排序
- fed 兜底: 本网段无可用主机时, 从 source=fed 主机中选 (优先 role=secretary
  的对端, 无 secretary 时退化任意 fed 主机)

**转发链路**: 本机 PM 忙/不可用 + 命中 fed 主机 → `_federation_forward_task`
POST 对端 `/api/federation/tasks/forward` (task_data + forwarded_from) →
对端 submit_task_from_chat 创建新任务 (created_by=federation:*), 本侧任务置
forwarded + output_data.forwarded_to/federation → WS 广播 + Bot 通知;
转发失败置 failed

**联邦防环** (跳数上限 1):
- 转发时 task_data.input_data 注入 `_federation_relay=True` 标记
- 对端转发端点读出标记透传 `fed_relay` 参数, 任务落库时写回 input_data
  (审计可见)
- relay 任务在对端再次命中 fed 主机时不再回传, 直接 failed
  (错误信息含「跳数上限 (防环)」) — 防止 A↔B 互相委托死循环

**修复** (iter-65 真机验证发现):
- `http_post` 改为模块级导入 — 此前仅在方法内导入, 模块级调用
  (submit_task_from_chat 派发/转发/cancel/pause 等) LOAD_GLOBAL NameError
  被 except 静默吞掉, 远程路径静默损坏
- `_federation_sync_peer` hosts 循环跳过对端自身 — 此前对端被其报告网卡
  ip 覆盖 peer.host, 跨网段转发全部不可达

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-29 | iter-65 | F3.4 遗留 联邦任务跨网段转发: 选站分层 (lan 优先/fed 兜底) + 转发端点 + forwarded 徽标 + 联邦防环 (跳数上限 1); 修复 http_post 静默损坏与对端 ip 覆盖 |
| 2026-08-29 | iter-64 | F3.4 跨网段多 Secretary 联邦 (发现层): 静态 peer 配置 + /api/federation/info 端点 + 联邦轮询同步 (source=fed 隔离) + 选举仅限本网段 + 离线检测 |
| 2026-08-16 | iter-30 补③ | P2 #5: 节点间认证默认启用 (auth_enabled 默认 true, 可显式关闭; 白名单保障注册引导/健康检查免认证) |
| 2026-08-16 | iter-27 后 | 初建 |
