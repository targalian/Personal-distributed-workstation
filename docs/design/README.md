# Work Station 项目设计文档

> 本目录是与代码**一一对应**的设计文档库：按功能域划分子文件夹，每个功能域一份文档，
> 域内逐模块描述职责、设计要点、关键接口与依赖关系。
> **代码设计变更时，必须同步更新对应文档**（见文末「维护规范」）。

最后更新: 2026-08-16 (S3-startup-sync 之后)

---

## 一、项目定位

Work Station (LAN Mesh) 是一个**分布式个人 AI 工作站**：将局域网内异构主机组网成统一
调度网格，由 PM Agent 驱动「任务拆解 → 团队组建 → 分布式执行 → 结果聚合」全流程，
并通过模型路由器统一管理多家 LLM 服务商的额度、价格与密钥轮换。

核心理念：
- **Station Director 管「机器」，Secretary 管「项目」** —— 基础设施与任务编排解耦
- **Graph Engineering** —— 任务以 DAG 组织（条件边/动态路由/checkpoint 断点恢复）
- **分层混合交互** —— L1 项目对话(秘书) + L2 PM 线程(深度技术讨论)

## 二、系统架构总览

```
                    ┌─────────────────────────────────────────┐
                    │  Web UI (dashboard.html, 7 Tab)         │
                    └──────────────┬──────────────────────────┘
                                   │ HTTP + WebSocket
┌────────────── Secretary 主机 (第一台启动者自动当选) ──────────────┐
│  station_api.py ── 路由层 (基础路由 + Secretary 路由 + /ws)      │
│       │                                                          │
│  station_controller.py ── 控制器 (发现/注册/心跳/选举/密钥同步)   │
│       ├── station_director.py ── 主机评级/舰队管理 (只管机器)     │
│       ├── orchestrator.py + pm_*.py ── 任务编排 (只管项目)        │
│       ├── model_router.py + model_resources.py ── 模型路由/预算   │
│       ├── chat_handler.py ── 秘书对话                             │
│       └── database.py ── SQLite 持久化 (迁移 v1~v4)               │
└──────────┬──────────────────────┬───────────────────────────────┘
           │ UDP 发现 + HTTP       │ 任务分发/进度上报 (HTTP)
     ┌─────┴─────┐          ┌──────┴──────────┐
     │ Station B │          │ Worker 主机      │
     │ (同构进程) │          │ worker.py +      │
     └───────────┘          │ agent_runtime.py │
                            └─────────────────┘
```

关键机制：
- **Secretary 选举**: 局域网第一台启动 `station` 的主机自动当选，其余保持 station 模式
- **Secretary Failover (E5)**: Secretary 超时离线后由 device_id 字典序最小的在线 Station 自动接任
- **跨主机版本统计 (S2/S3)**: UDP 包/注册/心跳三通道携带 git commit，落库 hosts 表，Web 端展示版本分布
- **API Key 加密分发 (S1/S3)**: AES-256-GCM (mesh_token 派生密钥)，Secretary 推送 + 新节点启动拉取，替代轮询
- **事件实时推送**: event_bus 进程内发布订阅 → /ws WebSocket 广播

## 三、仓库结构总览

```
work_station/
├── main.py                 入口 (station / worker 两种角色)
├── config.yaml             全局配置 (端口/安全/发现参数)
├── VERSION.json            发布版本记录 (S2 起, 每次发布需手动更新)
├── loop_status.json        Loop Engineering 迭代状态机
├── lan_mesh/               ★ 主程序包 (全部模块见各功能域文档)
│   ├── web/templates/      dashboard.html (Web UI 单文件)
│   ├── resources.yaml      模型资源配置 (含 API Key, 不入版本库)
│   └── model_pool.yaml     模型能力/价格矩阵
├── scripts/                启动脚本 + sync_push.ps1 双库推送
├── skills/                 技能库 (SKILL.md 格式, 中央分发)
├── tests/                  pytest 基线测试
├── test_bug/               Loop Engineering 每日验证循环
├── temp_resault/           UI 验证截图存档
└── quicklan-main/          独立子项目: Tauri 桌面文件共享应用
```

## 四、功能域文档索引

| 子目录 | 功能域 | 覆盖模块 |
|---|---|---|
| [01-network-discovery](01-network-discovery/README.md) | 网络与发现 | discovery, protocol, auth, http_retry |
| [02-station-core](02-station-core/README.md) | Station 核心 | station_controller, station_director, station_api, secretary, master, database |
| [03-task-orchestration](03-task-orchestration/README.md) | 任务编排 | orchestrator, task, pm_agent/planner/dispatcher/monitor/state, task_templates, project |
| [04-execution-engine](04-execution-engine/README.md) | 执行引擎 | worker, agent_runtime, agent_card, agent_prompt, tool_registry, mcp_client, mcp_gateway, sandbox, skill_registry |
| [05-resources-secrets](05-resources-secrets/README.md) | 资源与密钥 | model_resources, model_router, balance_probe, secret_sync, version_sync, collect_config |
| [06-interaction](06-interaction/README.md) | 交互通道 | chat_handler, bot_gateway, role_cards |
| [07-data-sync](07-data-sync/README.md) | 数据与同步 | shared_folder, cloud_sync, host_info |
| [08-infrastructure](08-infrastructure/README.md) | 基础设施 | config, logger, event_bus, error_tracker, host_rating, preflight, api |
| [09-frontend](09-frontend/README.md) | Web 前端 | dashboard.html |
| [10-test-loop](10-test-loop/README.md) | 测试与验证循环 | tests/, test_bug/ |
| [11-scripts-subprojects](11-scripts-subprojects/README.md) | 脚本与子项目 | scripts/, skills/, quicklan-main/ |

## 五、维护规范（重要）

1. **同步更新**: 修改代码时，若模块的**职责、接口或设计决策**发生变化，必须在同一
   提交内更新对应功能域文档的相关章节。
2. **新模块**: 在 `scripts/sync_docs.py` 的 MAPPING 登记一行 (文件→域), 运行
   `--write` 自动同步「模块清单」表, 并补充详情小节; 无合适功能域时新建子目录
   并更新本 README 索引。
3. **变更记录**: 每次文档更新在对应文档末尾的「变更记录」追加一行
   （日期 + 迭代号 + 一句话摘要）。
4. **豁免情形**: 不改设计的纯实现优化（性能、bug 修复、注释）无需更新文档。
5. **命名**: 文档内引用模块一律用文件名（如 `station_controller.py`），与代码一一对应。
6. **清单表自动化**: 各域「模块清单」表位于 `<!-- AUTO:module-list -->` 区块内,
   由 `scripts/sync_docs.py` 依据模块 docstring 生成, 禁止手动编辑该区块;
   pre-push hook 第 8 项强制校验漂移 (详见 `.qoder/skills/docs-sync`)。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-30 | F1-role-free-align: 密钥与版本对齐与主从无关 (config_ts 仲裁 + 60s 对齐线程 + 落后节点自动升级); E5-secretary-failover: Secretary 离线故障转移收录至关键机制 |
| 2026-08-16 | iter-28 | D2-docs-sync: 模块清单自动化 (sync_docs.py + pre-push 第 8/9 项 + docs-sync skill) |
| 2026-08-16 | iter-27 后 | 设计文档库初建: 11 个功能域, 覆盖全部 lan_mesh 模块 |
