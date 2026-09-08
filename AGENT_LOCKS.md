# Agent 占用登记表

多 Agent（Codex CLI / Qoder Quest）并行开发时的文件占用与交接看板。
**开工前必读，认领后立即回写，完工后立即释放。** 规则见 AGENTS.md「多 Agent 协作」。

- 更新时间：2026-09-09
- 当前迭代：`iter-92`（Codex：Boss 通道 + stock_player 任务图交接 + BUG-032 停滞告警修复，已验证待 Boss 发货；iter-91 Quest UI 审计修复轮同批待发）

## 一、职责边界（长期约定）

| 范围 | 归属 | 说明 |
|---|---|---|
| `lan_mesh/**.py` | **Codex** | 后端主控/PM/调度/DB 逻辑，改动即需跑 pytest 全量 |
| `tests/test_core.py` | **Codex** | 专项用例随代码同轮提交（追加在文件尾部，减少冲突面） |
| `docs/design/**` | **Codex 主** / Quest 补 | 代码行为变更由改代码方同步；纯表述修订 Quest 可动 |
| `.qoder/repowiki/**` | **Quest** | post-commit hook 拉起 `qoderclicn` 自动维护，Codex 不得手改 |
| `.qoder/skills/**` | **Quest** | 技能手册（loop-engineering / repowiki-update 等） |
| `docs/reference/**` | **Quest** | 审查报告、能力评估、重构提案（只出结论不改代码） |
| `webui/**`、`quicklan-main/**` | **Quest** | 前端 SPA / Tauri 子项目 |
| `AGENTS.md`、`loop_status.json`、`VERSION.json` | **共享·串行** | 单次只允许一方持有，改完立刻提交释放 |
| `.githooks/**`、`scripts/sync_push.ps1` | **需先登记** | 门禁基础设施，改动前在下表登记并说明理由 |

## 二、当前占用

| Agent | 占用文件 | 任务 | 开始 | 状态 |
|---|---|---|---|---|
| Codex | 空闲 | 无 | —— | 已释放 |
| Quest | 空闲 | 无 | —— | 已释放 |

> 认领格式：一行一个 Agent，`占用文件` 写通配范围（如 `lan_mesh/station_*.py`），
> `状态` 取 `进行中` / `待验证` / `已释放`。释放后把该行改回 `——`。

## 三、待推送内容归属（工作区当前脏文件）

`scripts/sync_push.ps1` 要求工作区干净，因此下列改动必须**分两次提交**、
按归属各自提交，不要互相 `git add .`：

**Quest（无待推送改动）**

**Codex（iter-92 Boss 通道 + 股票项目交接 + BUG-032，已验证待 Boss 发货）**
- `scripts/boss_channel.py`（**新增**：Codex ↔ 秘书/PM CLI 通道，12 子命令；mesh token 自动加载；网络异常收敛不抛出；401/503/409 附成因提示）
- `scripts/sync_docs.py`（MAPPING 登记 boss_channel.py。**坑**：扫描范围内的 `.py` 必须用纯字符串值，带描述的 tuple 值会被 `find_unmapped` 判为未登记）
- `lan_mesh/runtime_trace.py`（BUG-032：`_stall_db_filter` 与 DB 对账，抑制幽灵/已终态任务；`_TASK_TERMINAL_STATUS` 常量；非终态照常告警，`_db_ref` 未注入退化旧行为）
- `tests/test_core.py`（`TestIter92StallDbReconcile` 5 例：幽灵/DB 终态/真实存活/99+1 混合批/无 DB 退化；全量 468 passed）
- `docs/design/02-station-core/README.md`（runtime_trace 停滞告警 DB 对账节 + iter-92 变更记录）
- `docs/design/11-scripts-subprojects/README.md`（boss_channel.py 子命令表与设计约束 + iter-92 变更记录 + 清单条目）
- `docs/reference/stock-player-task-graph.json`（**新增**：股票项目 16 节点/22 边 DAG，schema `lan_mesh.task_graph/v1`，无环已验证）
- `docs/reference/stock-player-handoff.md`（**新增**：交接说明 — 为何用图、G0 澄清闸门 12 问、Agent 编队分配、通道用法、本轮抓到的工作站问题）
- `loop_status.json`、`AGENT_LOCKS.md`（iter-92 收尾）

> `docs/reference/**` 名义归 Quest，上述两份为 Codex 出具的交接/规划产物（同
> `ui-backlog-for-quest.md` 先例），一次性写入，后续如需维护再移交。

**Codex（iter-90 交付前蓝图验收自检，已验证待 Boss 发货）**
- `docs/reference/ui-backlog-for-quest.md`（**新增**：UI-064~UI-075 交接清单。该目录名义归 Quest，本文件属 Codex → Quest 的交接产物，由出具方 Codex 一次性写入，后续维护与勾销归 Quest）
- `lan_mesh/pm_planner.py`（`review_against_blueprint` 交付前按蓝图 `acceptance_criteria` 逐条审查交付物；`_build_acceptance_prompt` / `_parse_acceptance_review` 渲染与解析，异常一律空 dict 降级）
- `lan_mesh/pm_agent.py`（`deliver_result` 上报前挂 `acceptance_review`，未达成项打 warning；新增 `_review_delivery`）
- `lan_mesh/station_routes_tasks.py`（`/api/pm/{id}/deliver` 落 `output_data._delivery.acceptance_review` 并随 `task_delivered` 广播）
- `lan_mesh/chat_handler.py`（验收动作回复附自检结论与未达成清单）
- `lan_mesh/web/templates/dashboard.html`（任务卡片验收自检徽章 + 交付弹窗「验收自检」区 + toast 未达成计数）
- `tests/test_core.py`（TestIter90BlueprintAcceptance 5 例；全量 463 passed）
- `docs/design/03-task-orchestration/README.md`、`docs/design/09-frontend/README.md`（iter-90 节 + 变更记录）
- `test_bug/test_checklist.csv`（UI-063 检测通过，Playwright 实测）
- `loop_status.json`、`AGENT_LOCKS.md`（iter-90 收尾与锁释放）

**Codex（iter-79 LLM 意图分类兜底 + iter-80 创建对话失败修复，已验证待 Boss 发货）**
- `lan_mesh/chat_handler.py`（关键词/继承未命中且过 `_looks_like_command` 成本闸门时做一次 LLM 意图分类，结果必须落在 `_ACTION_DESCRIPTIONS` 白名单，非 JSON/越权/异常一律回退无动作；`_resolve_chat_model_pref` 抽取主回复与分类共用的模型偏好解析）
- `tests/test_core.py`（TestIter79LlmIntentClassifier 6 例：口语指令分类并执行/闲聊零分类成本/none 不执行/非 JSON 忽略/白名单拦越权/关键词快路径不进分类器；pytest 421 passed）
- `docs/design/06-interaction/README.md`（LLM 意图分类兜底（iter-79）节 + 变更记录）
- `loop_status.json`、`AGENT_LOCKS.md`（iter-79 收尾与锁释放）
- `lan_mesh/station_secretary.py`（iter-80: activate_secretary 新增 E4 仲裁预检, 已有优先 Secretary 直接返回 ok:false + conflict + secretary_url; _find_existing_secretary_host 过滤 self/offline/fed）
- `lan_mesh/web/templates/dashboard.html`（iter-80: 监听 secretary_yielded 立即降级 UI + toast 对端接管; createConversation 检查 response.ok 并展示 detail/message）
- `tests/test_core.py`（TestSecretaryConflict 新增 2 例: 优先 Secretary 拒绝手动激活并返回地址 / 在线 Secretary 过滤; 全量 423 passed）
- `docs/design/02-station-core/README.md`（E4 手动激活预检说明 + iter-80 变更记录）
- `docs/design/06-interaction/README.md`（创建对话失败与让位同步（iter-80）节 + 变更记录）
- `test_bug/test_checklist.csv`（UI-061 登记: 后端链路已验证, 浏览器待检测）
- `test_bug/reports/2026-09-03.md`（03:01 自动日报, 记录同一现象下的接口失败, 保留为 Boss 观察证据）
- `loop_status.json`、`AGENT_LOCKS.md`（iter-80 收尾与锁释放）

> 2026-09-03 更新: iter-75 收官 / iter-76 脚本对齐 / iter-77 BUG-031 /
> iter-78 需求收集 已由 Boss 经 ship.ps1 发货（a15bec5 + 60ce2b9 +
> 78db2d2，本地与远端齐平）。本轮 Codex 与 Boss 发货并发：iter-79 改动
> 全部发生在发货提交之后，未被卷入，为干净增量。

## 四、下一轮排期建议（避免同文件竞争）

| 任务 | 建议归属 | 冲突面 |
|---|---|---|
| ✅ 秘书需求收集状态机（iter-78 已完成：多轮收集 → Brief → 最终提示词 → 确认/快速退出派发，7 例专项） | ~~Codex~~ | ~~已释放~~ |
| ✅ BUG-031 秘书静默失败（iter-77 已完成：三层护栏 + 意图继承，8 例专项 + 三处反向验证） | ~~Codex~~ | ~~已释放~~ |
| ✅ 意图识别升级（iter-79 已完成：关键词快路径 → 确认继承 → LLM 分类兜底，成本闸门 + 白名单防幻觉，6 例专项） | ~~Codex~~ | ~~已释放~~ |
| ✅ 创建对话失败修复（iter-80 已完成：激活前 E4 预检 + secretary_yielded 前端同步 + 503 detail 展示，2 例专项 + 隔离实例端到端复现） | ~~Codex~~ | ~~已释放~~ |
| ✅ 点亮优化讨论发送 UI（iter-74 已完成: `optDiscussSend()` 调 `POST /api/secretary/chat` 带 `discuss_context`，WS 分流 + 历史过滤 + 503 提示，UI-060 通过） | ~~Quest~~ | ~~`dashboard.html` 单文件~~ |
| ✅ `station_controller.py` 拆 8 mixin Phase 1-2（iter-74 已完成：组合接线 + SelfHeal/Hosts/Sync 三块 28 方法搬入，壳类 3253→2322 行） | ~~Codex~~ | ~~已释放~~ |
| ✅ `station_controller.py` 拆分 Phase 3-5（iter-75 已完成：53 方法入 5 mixin，壳类 3253→246 行；修复 12 处测试 monkeypatch 目标迁移） | ~~Codex~~ | ~~已释放~~ |
| Phase 6 收尾：repowiki 同步 8 个 mixin 模块卡片 + 架构图（design 文档 Codex 已同步） | **Quest 承接** | 仅文档/知识库，代码侧已完结；等 Boss 推送后启动 |
| ✅ 交付前蓝图验收自检（iter-90 已完成：验收标准逐条审查 + 交付链路落库广播 + UI 展示，5 例专项 + 3 处变异验证 + Playwright 实测） | ~~Codex~~ | ~~已释放~~ |
| ✅ Boss 通道 + 股票项目任务图交接（iter-92 已完成：`boss_channel.py` 12 子命令 + 16 节点 DAG + G0 闸门 12 问） | ~~Codex~~ | ~~已释放~~ |
| ✅ BUG-032 停滞告警幽灵刷屏（iter-92 已完成：DB 对账，真实数据 100→0，5 例专项 + 3 处变异致红） | ~~Codex~~ | ~~已释放~~ |
| stock_player 首里程碑执行（等 G0 闸门：Boss 答完 Q1/Q7/Q8/Q11/Q12 后写蓝图并派发 PM） | **Codex（交接）+ PM（执行）** | 项目侧仓库，不碰 work_station |
| F6 shadow-dev WS 实时广播（Quest 已交回 `[Quest→Codex]`，建议走 `event_bus.publish_event("shadow_run_update", {run_id,status})`） | Codex | `lan_mesh/shadow_dev.py`；排期在 stock_player 之后 |
| 真物理多机实压 F3.1/F3.3 | Codex | 需真实主机，与拆分互斥（勿同轮） |
| 前端 Tab 与新端点字段对齐复查 | Quest | `webui/`、`dashboard.html` |
| ~~UI-064 影子开发面板（提交/队列/报告/守护，`/api/shadow-dev/*`）**P0**~~ ✅ 完成 (iter-91, UI-064 检测通过) | ~~Quest~~ | ~~`dashboard.html`~~ |
| ~~UI-065 任务停滞告警面板与手动巡检（`/api/runtime/task-stall-alerts[/check]`）**P0**~~ ✅ 完成 (iter-91, UI-065 检测通过) | ~~Quest~~ | ~~`dashboard.html`~~ |
| UI-066~UI-068 任务记忆统计 / 团队拓扑 / runtime stats 面板 **P1** | **Quest** | `dashboard.html` 单文件 |
| UI-069~UI-073 云同步卡 / 路由 dry-run / 日志修剪 / 联邦信息卡 / 对话管理增强 **P2** | **Quest** | `dashboard.html` 单文件 |
| UI-074~UI-075 SPA 补齐项目·蓝图页与交付验收（消费 `acceptance_review`）**P3** | **Quest** | `webui/src/**` |


> UI-064~UI-075 的逐项后端契约（端点/请求体/响应结构/错误码）与期望前端行为、验收要求见
> `docs/reference/ui-backlog-for-quest.md`（Codex 2026-09-08 出具）。12 项后端均已就绪，
> 无需 Codex 先行补接口；Quest 可按 P0 → P3 顺序单轮单项认领。

## 五、人在回路外（Boss 观察与发货）

Codex 的沙箱把 `.git` 挂为只读（NTFS 层其实有 Modify 权限），因此**提交/推送是
唯一必须由非 Codex 执行的步骤**。为此提供两个脚本，Boss 无需参与开发决策：

| 场景 | 命令 |
|---|---|
| 看全局（版本/迭代/归属/待推送/占用/队列） | `python scripts/dev_status.py` |
| 看全局 + 跨 Agent 交接详情 | `python scripts/dev_status.py --notes` |
| 看全局 + 门禁自检 | `python scripts/dev_status.py --verify` |
| **一键发货**（分批提交 + 推送，逐步 y/N） | `powershell -File scripts/ship.ps1` |
| 发货预演（只看不做） | `powershell -File scripts/ship.ps1 -DryRun` |
| 完全无人值守 | `powershell -File scripts/ship.ps1 -Yes` |
| 只提交不推送 | `powershell -File scripts/ship.ps1 -NoPush` |

`ship.ps1` 的固化逻辑（无需人工判断归属）：
1. 门禁前置（编译 + `sync_docs`），FAIL 即中止，不留半成品提交；
2. 批 1 = Quest/共享（wiki、文档、协作机制、配置示例），期间置
   `LAN_MESH_WIKI_DRY_RUN=1` 静音 post-commit，避免 Quest 后台任务与批 2 抢
   repowiki 脏文件；
3. 批 2 = Codex 代码（`lan_mesh/` + `tests/` + `docs/design/`），此时工作区已净，
   hook 可安全拉起 Quest 同步 wiki；
4. 工作区干净后调 `scripts/sync_push.ps1` 推双仓库（自动补 VERSION.json 同步提交）。

## 六、交接纪律

1. **结论落盘**：跨 Agent 结论写 `loop_status.json.notes`（带 `[Quest]` / `[Codex]` 前缀）
   或 `docs/reference/`，会话历史对方读不到。
2. **单轮单任务**：一轮只认领一个路线图任务，缩短占用窗口。
3. **推送前 rebase**：`git pull --rebase` 后再走 `scripts/sync_push.ps1`，
   撞 `loop_status.json` 时手工合并两方 notes，不要择一覆盖。
4. **hook 联动**：Codex 提交代码 → post-commit 入队 `.qoder/repowiki/.pending/` →
   Quest 侧 `qoderclicn` 后台消费。Codex 不要清空该队列。
5. **锁超时**：占用超过 24h 未更新视为失效，接手方在本表注明「接管原因」后可强占。
