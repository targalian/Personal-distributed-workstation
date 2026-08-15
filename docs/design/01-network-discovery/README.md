# 01 网络与发现

局域网组网的底座层：UDP 广播发现、协议数据结构、节点认证、HTTP 重试。

## 模块清单

| 模块 | 职责一句话 |
|---|---|
| discovery.py | UDP 广播设备发现 (presence 包收发 + TTL 离线清理) |
| protocol.py | 协议定义: 端口常量、DiscoveryPacket、HostInfo/HostRecord 数据模型 |
| auth.py | 节点间 mesh_token 认证 (Shared Token, 可选启用) |
| http_retry.py | 节点间 HTTP 调用封装 (指数退避重试 + 自动附加 token) |

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
- 可选启用（config.yaml `security.auth_enabled`，当前默认关闭）
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

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-27 后 | 初建 |
