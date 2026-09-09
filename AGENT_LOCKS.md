# Agent 占用登记表

多 Agent（Codex CLI / Qoder Quest）并行开发时的文件占用与交接看板。
**开工前必读，认领后立即回写，完工后立即释放。** 规则见 AGENTS.md「多 Agent 协作」。

- 更新时间：2026-09-09
- 当前迭代：`iter-98`（Codex：F6 影子运行状态 WS 广播闭环，Quest→Codex 交回项已清空；iter-97 BUG-036 同批待发货；iter-91~96 已发货）

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

**Codex（iter-98 F6 影子开发运行状态 WS 实时广播，已验证待 Boss 发货）**
- `lan_mesh/shadow_dev.py`（**F6**，Quest 在 iter-91 UI 审计中交回的唯一遗留项：影子开发全链路**零 WS 广播** —— 后端状态机完整但状态只活在内存 `self._runs`，前端只能切 Tab 或手动点刷新；一次运行可长达 1800s，期间面板全程静默。新增 `SHADOW_RUN_EVENT` 常量与 `_emit_run_event` 单一出口，四处埋点 `queued`/`running`/终态/`cancelled`。三条不变式：广播**必须在释放 `self._condition` 之后**（锁内只取快照，`publish` 会走 `call_soon_threadsafe`，持锁回调有死锁风险）；`_emit_run_event` 整体 `try/except` 只记日志，**事件通道故障绝不阻断影子执行流**；`cancelled` 只报「排队中被丢弃」的，已进 CLI 的运行不误报）
- `lan_mesh/web/templates/dashboard.html`（`onStationEvent` 增 `shadow_run_update` 分支：**仅 `panel-shadowdev` 为 active 时**才 `refreshShadowDev()`——沿用 iter-91 F3 教训，那次恒真守卫失效正因判据选了静态 DOM 存在性；终态才弹 toast，排队/执行中不弹，否则一次运行连弹三次）
- `tests/test_shadow_dev.py`（5 例：全生命周期事件顺序 / 异常 ERROR 带错误文本 / `stop_guardian` cancelled 且不含运行中 / 事件通道爆炸不影响执行 / dashboard 接线断言。**12 变异全部致红**）
- `docs/design/02-station-core/README.md`（shadow_dev 状态事件小节）、`docs/design/09-frontend/README.md`（iter-98 变更记录）
- `loop_status.json`、`AGENT_LOCKS.md`（iter-98 收尾）

> **变异脚本踩坑（务必沿用）**：首轮 12 变异「全部 RED」是假象——脚本用 `subprocess.run(["python", ...])` 时 PATH 解析到了另一个解释器（idf-python）报 `No module named pytest`，退出码 1 被误判成 RED。必须用 `sys.executable`，并先打印 baseline rc 与 passed 行数自证 pytest 真跑起来了。

**Codex（iter-97 BUG-036 非指令语气护栏，已验证待 Boss 发货）**
- `lan_mesh/chat_handler.py`（**BUG-036**：认领 iter-96 待办「扫描关键词误触发面」时发现问题远超预期。系统扫描全部 69 个 `_ACTION_KEYWORDS`，用 21 句真实非指令样本实测 **15 句误触发**——BUG-035 只覆盖了疑问一类。三类漏网：否定（**最危险，语义完全相反**，真机实测「先不要创建项目」竟真创建了项目 `f16d07ff`「暂缓建项讨论蓝图」，已归档）、复述过去、议论提议。新增 `_looks_like_non_directive` 统合四类语气；**否定与复述不受祈使豁免**（「请先不要创建项目」语义是不要做）；新增 `_READONLY_ACTIONS` 白名单修正 BUG-035 把「查看任务列表」一并拦掉的过度拦截；**护栏同时加在 `_looks_like_command` 上**，否则关键词被拦后 LLM 分类兜底会把否定句重新判成动作，整条防线被绕过）
- `tests/test_core.py`（`TestIter97NonDirectiveGuard` 8 例：否定/否定不受祈使豁免/复述/议论/只读放行/18 条真实指令 0 回归/helper 四类语气/**LLM 闸门同受护栏**。另同步更新 `TestIter96` 一条 marker 用例——「查看」已从疑问标记移除）
- `docs/design/06-interaction/README.md`（BUG-036 节，含真机误建项目证据与两条路径说明 + iter-97 变更记录）
- `loop_status.json`、`AGENT_LOCKS.md`（iter-97 收尾）

**Codex（iter-96 BUG-035 询问句误触发 + 重启脚本修复，已随 iter-91~96 发货）**
- `lan_mesh/chat_handler.py`（**BUG-035**，iter-95 真机验证时实测抓到：向秘书提问「M1 阶段的**验收标准**有哪些」，回答正确但末尾追加「📋 已验收任务…的交付物」——一次提问执行了一次验收。根因是 `_detect_action` 朴素子串匹配，`_ACTION_KEYWORDS` 把「验收」单列为触发词而「验收标准」必然命中；同类风险词还有退回/取消/暂停，且模块第 178 行本就把「验收标准」列为需求收集问句，两处语义冲突。修复：新增 `_looks_like_question` 在关键词匹配前拦截询问句，`_QUESTION_MARKERS` + `_IMPERATIVE_MARKERS`（祈使优先，保证「帮我验收一下任务A好吗」仍执行））
- `scripts/stop_workstation.bat`、`scripts/restart_workstation.bat`（**Boss 首次重启未生效的根因**：`station.lock` 缺失时 stop 落入 `:fallback_netstat`，发了 taskkill 后既不校验是否杀掉、也不等端口释放就 `exit /b 0` 报成功，restart 于是带着旧进程去 start → 端口冲突 → 「脚本跑完了但服务还是旧的」。修复：fallback 补等端口释放 + 15s 仍占用则报错退出并给出可复制的 taskkill 命令；restart 检查 stop 返回码非 0 即中断；新增 `LANMESH_NO_PAUSE=1` 避免串联时卡在 pause；僵尸锁分支同样转入 netstat 兜底）
- `tests/test_core.py`（`TestIter96QuestionNotAction` 6 例：询问不触发/真指令仍触发/疑问词不挡祈使/纯查询无动作/helper 语义/**每个标记独立生效**。最后一例是补的——首轮变异 M3、M4 未致红，因样本句同时命中多个标记形成冗余掩盖）
- `docs/design/06-interaction/README.md`（BUG-035 节 + iter-96 变更记录）
- `loop_status.json`、`AGENT_LOCKS.md`（iter-96 收尾）

> 说明：`scripts/{stop,restart}_workstation.ps1` 的三级兜底（taskkill → Stop-Process → wmic）是 Boss/他方在本轮重启时的改动，**不属于 Codex 本轮工作**；我仅清理了其尾部空行以过 `git diff --check`，未改动其逻辑。

**Codex（iter-95 蓝图回流闭环 + 秘书角色卡定位修正，已验证待 Boss 发货）**
- `lan_mesh/project.py`（新增 `record_delivery_to_blueprint`：把 PM 交付结论回流到蓝图当前路线图阶段——pass 且无 unmet 则推进为 `review`，有 unmet 则保持状态并记录缺口清单；只动 `deliveries` 不改 Boss 手填的 phase/goal/branch；同阶段保留最近 5 条防膨胀。新增模块级 `_current_phase_index`，选取口径与 `pm_planner._current_roadmap_phase` 严格一致——口径漂移会让结论写错阶段，已固化为变异用例 M4）
- `lan_mesh/station_routes_tasks.py`（`receive_pm_delivery` 交付入库后调用新增的 `_reflow_blueprint` 桥梁函数，由 `task.project_id` 定位项目；广播增补 `blueprint_updated`；任何异常只记日志绝不阻断交付入库）
- `lan_mesh/role_cards.py`（**Boss 定调后修正**：移除「(如股票交易、编程开发等)」整类拒答——拒答**交易策略**幻觉与开发**交易系统**并不冲突，后者是业务软件项目管理属秘书本职；新增「负责管理项目、不充当领域专家」与「产出物是项目蓝图」两条；「回复必须简洁明了」放宽为「简洁为默认，但蓝图/需求/验收标准/阶段计划须完整输出」，修掉 iter-93 实测拆解计划被压到 605 字的问题；能力范围新增第 7 项）
- `tests/test_core.py`（`TestIter95BlueprintReflow` 7 例：pass 推进阶段 / unmet 保持并记缺口 / 回流阶段与 PM 读取口径一致 / 无蓝图静默跳过 / 交付记录封顶 5 条 / helper 由 task 串到 project 且异常不抛 / 角色卡定位断言。另给 BUG-034 那轮的角色卡断言补了原文全串校验——原断言存在「部分替换仍过关」的漏洞）
- `docs/design/03-task-orchestration/README.md`（蓝图回流闭环节 + iter-95 变更记录）
- `docs/design/06-interaction/README.md`（秘书角色定位修正节，含协作模式链路图 + iter-95 变更记录）
- `loop_status.json`、`AGENT_LOCKS.md`（iter-95 收尾）

**Codex（iter-94 BUG-034 跨站子任务结果回传，已验证待 Boss 发货）**
- `lan_mesh/station_local_pm.py`（**根因 2**：`_local_execute_task` 原先无条件把结果注入本机 PM，完全不看 `payload.pm_id` → 远程 PM 分发到本机的子任务，结果被注入错误 PM 或直接丢弃，原始 PM 只能耗到全局超时；新增 `_report_subtask_result` 按 `pm_id` 路由，跨站则 POST `secretary_url + /pm/progress-report` 回传原始 PM）
- `lan_mesh/pm_monitor.py`（**根因 1**：新增 `_clear_subtask_timer` 在 completed/failed 时清理 `subtask_start_times`，此前已完成子任务满 1800s 会被误判超时并触发重试；新增 `_trace_retry` 让重试进入任务流追踪；`progress_loop` 进度未变即跳过上报（实测 361 条报告中 341 条重复）；顺带抽出 `_log_self_check` 压回函数长度门禁）
- `lan_mesh/pm_dispatcher.py`（`_record_subtask_start` 从两处调用点移入 `dispatch_subtask` 方法体，确保重试分发也重新注册超时计时器；`/tasks/execute` payload 增补 `secretary_url` 供跨站回传定位）
- `lan_mesh/runtime_trace.py`（`TASK_STAGE_LABELS` 新增 `subtask_retry` → 「子任务重试」）
- `tests/test_core.py`（`TestIter94CrossStationSubtaskCallback` 6 例：跨站回传/同 PM 本地注入/缺 secretary_url 不误注入/终态清理计时器/重试刷新时间戳/去重游标初值；并给 `TestIter87` 的 SimpleNamespace 桩补 `_report_subtask_result` 委托；全量 481 passed）
- `docs/design/03-task-orchestration/README.md`（BUG-034 双根因分析节 + iter-94 变更记录）
- `loop_status.json`、`AGENT_LOCKS.md`（iter-94 收尾）

**Codex（iter-93 G0 闸门 + 股票项目交接 + BUG-033，已验证待 Boss 发货）**
- `lan_mesh/station_scheduler.py`（BUG-033：`submit_task_from_chat` 新增 `project_id` 参数并落到 `Task`；成本预估由硬编码空项目改为传入真实项目，预算适配随之生效）
- `lan_mesh/chat_handler.py`（新增 `_resolve_project_from_message` 三级解析：完整 uuid → 8 位短码 → 活跃项目名最长匹配（≥4 字防短名误命中）；`_action_submit_task` 下传并回显绑定结果）
- `scripts/fix_task_project_binding.py`（**新增**：存量任务补绑项目的一次性修正脚本，须先停 Station——运行中 SQLite 写锁独占会报 readonly database）
- `scripts/sync_docs.py`（MAPPING 登记 fix_task_project_binding.py）
- `tests/test_core.py`（`TestIter93TaskProjectBinding` 7 例：uuid/短码/名称最长匹配/无匹配/manager 缺失/动作下传/scheduler 落库；顺带修一处旧用例固定签名 lambda；全量 475 passed）
- `docs/design/03-task-orchestration/README.md`（对话派发任务的项目绑定节 + iter-93 变更记录）
- `docs/design/06-interaction/README.md`（秘书项目归属解析 + **已知局限**：角色卡「拒答股票交易类问题」「回复必须简洁」同交接定位冲突 + iter-93 变更记录）
- `docs/reference/G0-答题卡.md`（**新增**：Boss 已答完 12 题，交接依据留档）
- `docs/reference/stock-player-blueprint.json`（**新增**：据答题卡生成的项目蓝图，9 条验收标准 / 4 阶段 / 9 条决策，已写入 Station 项目 821230b9）
- `loop_status.json`、`AGENT_LOCKS.md`（iter-93 收尾）

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
| ✅ G0 澄清闸门 + 股票项目交接（iter-93 已完成：12 题答完 → 蓝图 → 建项目 821230b9 → 派发 task-0cdc51ad40dd） | ~~Codex~~ | ~~已释放~~ |
| ✅ BUG-033 对话派发任务无项目归属（iter-93 已完成：致蓝图驱动+验收自检静默失效，7 例专项 + 2 处变异致红） | ~~Codex~~ | ~~已释放~~ |
| 秘书角色卡定位冲突（拒答股票交易类问题 + 回复必须简洁，同项目交接入口定位冲突） | Codex（待 Boss 定调） | `lan_mesh/role_cards.py`；需先决策再动手 |
| PM 进度报告去重（实测每 10 秒重复上报同一条进度，未变时应抑制落库） | Codex | `lan_mesh/pm_agent.py`；排期在 M1 交付后 |
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
