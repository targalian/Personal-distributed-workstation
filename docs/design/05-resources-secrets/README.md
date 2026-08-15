# 05 资源与密钥

LLM 资源的统一管理：预算池、模型路由、余额探测，以及跨主机的
API Key 加密分发与版本同步（S1/S2/S3 迭代成果集中于此）。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| balance_probe.py | 资源余额探测 (R2) — 从服务商 API 自动获取资源池余额 |
| collect_config.py | LAN Mesh 主机配置独立采集脚本 |
| model_resources.py | 模型资源管理 — 多主机 / 多 API Key 预算池管理 (R1) |
| model_router.py | 模型路由器 — Phase 2 核心模块 |
| secret_sync.py | S1-key-sync: API Key 加密自动分发 (节点间密钥同步) |
| version_sync.py | S2: 版本记录与升级提醒 — 单机版本文件 + 局域网版本比对 + 领先节点通知 |
<!-- /AUTO:module-list -->
---

## model_resources.py — 资源池预算管家（R1）

**核心概念**: 资源池 = 一个可计量的预算来源。三种 plan_type:
- `payg` 按量付费（金额单位，按 model_pool.yaml 价格实时折算）
- `token_plan` token 包（一次性额度，可带有效期）
- `coding_plan` 编程订阅（周期性重置，以 renew_at 为锚点的窗口）

**设计要点**: 配置在 resources.yaml；用量落 SQLite resource_usage_log
（每次 LLM 调用一行，可审计可聚合）；R7 到期/额度预警周期检查；
保存配置后热重载并触发全网密钥推送。

## model_router.py — 模型路由器

**职责**: 任务难度分级（L1-L4）→ 加权评分选模型 → 降级链重试。

**评分公式**:
`Score = 能力匹配度×W_cap + 成本反向×W_cost + 响应速度×W_speed − 负载率×W_load`

**策略适配**: cost_first / quality_first / balanced（对接 project.py 预算护栏）。

## balance_probe.py — 余额探测（R2）

适配器模式: provider → probe 函数。已调研接入 siliconflow / deepseek /
moonshot 等余额 API；未支持的 provider 返回 unsupported + 引导提示。
UI 一键测试入口: `POST /api/resources/test-key`。

## secret_sync.py — API Key 加密分发（S1-key-sync）

**密码学设计**:
- mesh_token 为信任根 → HKDF-SHA256 派生 32 字节 AES-256 密钥
- AES-256-GCM 加密资源配置，每次传输随机 12 字节 nonce
- 依赖 cryptography>=41.0；缺失时明确报错，**绝不降级明文**

**接口**: `encrypt_config(data, token)` / `decrypt_config(payload, token)` /
`config_hash(data)`（幂等指纹）/ `mask_secret()`（日志脱敏）

**安全边界**: 防局域网嗅探，不替代 TLS（与 auth.py 信任模型一致）。

**分发链路**（station_controller 编排）:
1. Secretary 保存资源配置 → 热推送全部在线节点
2. 新主机注册成功 → 定向推送
3. 节点启动 → 主动拉取（`GET /api/secrets/fetch`，S3）
4. Secretary 激活后 → 兜底推送一次
5. 手动触发: `POST /api/secrets/sync-all`

接收端统一逻辑: 解密 → config_hash 校验 → 指纹一致幂等跳过（不落盘）→
validate → 保存 resources.yaml → 热重载资源管理器。

**信任根自愈（S1 演进）**: mesh_token 分歧（历史双 Secretary 脑裂 /
token 文件重建）会使解密失败。接收端（receive/fetch 两路径）此时
不直接报错，而是从推送方拉取 `/api/station/bootstrap-token` 收敛
mesh_token 后重试解密一次：
- 推送路径：解密失败时用请求来源 IP + 推送报文 `src_port` 定位推送方
- 拉取路径：用目标 Secretary 的 ip/port 直接收敛
收敛复用 `_converge_mesh_token`（拉取 + 持久化 + 内存态同步），
收敛后仍失败才返回错误，保证密钥分发可自愈无需人工对账。

## version_sync.py — 版本记录与升级提醒（S2-update-notify）

**机制**:
1. `VERSION.json`（项目根）记录发布版本（版本号/commit/说明/升级提示），
   **每次发布需手动更新**
2. 节点 UDP 发现包携带自身 git commit + 提交时间戳
3. 领先检测: 某节点版本严格领先全部在线节点 → 主动 HTTP 通知落后节点
   `git pull` 升级；落后方自检同样发提醒（双保险）
4. 版本比较用 commit 时间戳（同仓库线性历史可靠全序）；相同视为同版本，
   时间戳缺失不告警

**S3 演进**: 60s 轮询检测已删除，改为启动一次性同步 + 入网即时同步
（见 [02-station-core](../02-station-core/README.md)）。

## collect_config.py — 主机配置报告

采集主机全量配置（含 GPU 探测、Python 包统计）→ 生成文本报告 /
`host_config.json`（Station 启动时写入共享目录）。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-27 后 | 初建；收录 S1/S2/S3 完整链路设计 |
