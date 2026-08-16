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

**跨主机用量上报（R3 → M5-2 双通道）**:
- **WS 直推（M5-2, 实时主通道）**: Worker 注册后 `set_report_target(url, token)`
  派生 `ws://<secretary>/ws/worker?token=…` 并启动推送线程
  （`websockets.sync` 同步客户端）；每 3s 轮询未上报记录 → 发
  `usage_batch` 帧 → 收到 ack 后推游标；断线指数退避重连
  （5s→60s 封顶）；Secretary 拒绝（未激活）时不推游标交给兜底
- **HTTP 批量（R3, 兜底通道）**: `report_once` 60s 周期批量 POST
  `/api/resources/usage`（usage_id 幂等去重）；WS 通道新鲜
  （最近一轮成功 < 上报周期）时自动跳过，避免双通道重复推送
- 两端点共用 `apply_usage_batch`（station_routes_resources）同一
  幂等路径；Secretary 端 `/ws/worker` 见
  [02-station-core](../02-station-core/README.md)

**轮换量化调度（R5 → R5-2）**:
- 同一模型多池候选时按量化价值公式排序（`_pool_priority` →
  `_pool_score` 分量拆解, `rotation_plan` 透出审计）:
  `基线(订阅 10/按量 5) + 沉没成本压力(剩余额度比例 × 窗口紧迫度 ×
  W_sunk) + 时段折扣(W_time) + 临期加压 + 高水位收尾`
- 窗口紧迫度: monthly/renew 按窗口已逝比例; one_time 恒 1.0
  （额度不刷新, 尽早消耗避免沉没）
- 时段折扣依据供应商能力信息（`docs/reference/vendor-capability/`）:
  DeepSeek 按量空闲时段半价（高峰 9-12/14-18）、百炼夜间 22-08
  qwen3.8-max / deepseek-v4-pro-0813 五折；权重与时段可经
  resources.yaml `rotation:` 段覆盖
- `rotation.quant: false` 回退 R5 首版纯规则（`_pool_priority_rule`）
- **合规红线**: 订阅套餐禁非交互式批量调用（供应商条款）;
  `set_usage_mode_global("batch")` + `batch_block_subscription: true`
  时订阅池从候选剔除（需 payg 池兜底）；开关默认关闭

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

**分发链路**（station_controller 编排，F1 起角色无关）:
1. 任意节点保存资源配置 → 自动与全网对端对齐（本机 config_ts 最新 → 推送）
2. 新主机入网 → 即时对齐（推/拉由仲裁决定，与 Secretary/Station 角色无关）
3. 节点启动 → 主动对齐（`GET /api/secrets/fetch` 探测 + 仲裁，S3）
4. 周期对齐线程（60s）→ 内容一致静默跳过，不一致自动收敛
5. 手动触发: `POST /api/secrets/sync-all`（任意节点，非 Secretary 专属）

**对齐仲裁规则（F1）**: 内容指纹一致（config_hash **排除 config_ts**
元数据，防止落盘时间戳引发 ping-pong 漂移）→ 跳过；不一致时
`config_ts` 新者胜（本机新推、对端新拉）；ts 缺失/相等按资源池数
仲裁，仍相同则跳过告警。`config_ts` 由 `save_config()` 自动注入。

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
5. **F1 自动对齐**: 落后节点收到通知/自检落后 → `_auto_upgrade` 自动
   git pull + 依赖安装（工作区脏则跳过；同 commit 仅试一次；
   `config.yaml auto_upgrade: false` 可关闭）

**S3 演进**: 60s 轮询检测已删除，改为启动一次性同步 + 入网即时同步
（见 [02-station-core](../02-station-core/README.md)）。

## collect_config.py — 主机配置报告

采集主机全量配置（含 GPU 探测、Python 包统计）→ 生成文本报告 /
`host_config.json`（Station 启动时写入共享目录）。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-17 | iter-33 | R5-2: 轮换量化价值公式 (沉没成本压力 × 窗口紧迫度 + 时段折扣窗口; 供应商能力信息落档 docs/reference/vendor-capability; batch 合规红线开关) |
| 2026-08-17 | iter-32 | M5-2: Worker 用量 WS 直推通道 (websockets.sync 推送线程 + 断线重连; HTTP 批量降为兜底, 双通道 usage_id 幂等) |
| 2026-08-16 | iter-30 | F1: 角色无关密钥对齐 (config_ts 仲裁 + config_hash 排除 ts) + 版本落后自动升级 |
| 2026-08-16 | iter-30 补③ | P2 #6: resources.yaml 备份移位 (~/.lan_mesh/backups/, 留 3 代; 含密钥明文的备份不再与源码同目录) |
| 2026-08-16 | iter-27 后 | 初建；收录 S1/S2/S3 完整链路设计 |
