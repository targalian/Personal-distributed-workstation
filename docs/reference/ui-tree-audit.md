# UI 关系树与触发链审计 (dashboard.html + webui SPA)

- 审计日期: 2026-09-09 (iter-91, Quest)
- 审计对象: `lan_mesh/web/templates/dashboard.html` (4231 行) + `webui/src/` (React SPA)
- 审计方法: 静态抽取 (Tab/面板/区块/控件/handler/函数定义/WS 类型/getElementById) +
  交叉检查 (悬挂引用、缺失 DOM、Tab↔面板配对、switchTab 分派覆盖、后端字段契约核对)
- 结论速览: **无 Blocker 级漏洞**; 3 处低危转义弱化点 + 1 处失效守卫 + 2 处 UX/布局观察项,
  详见「五、漏洞发现」

---

## 一、触发链总览

### 1.1 初始化链 (DOMContentLoaded, L4211)

```
DOMContentLoaded
 ├─ checkSecretaryState()          # 秘书态 → 显隐 5 个 secretary-only Tab
 ├─ refreshStation()               # 默认 Tab 首刷
 ├─ connectWS()                    # WS 建连
 │    ├─ onopen  → refreshAllState()   # hosts/tasks/agents/projects/PM/threads/
 │    │                                 # secretary/version/opt (9 项, 不含 runtime/shadow)
 │    ├─ onmessage → 见 1.3
 │    └─ onclose → 3s 后自动重连
 ├─ setInterval(refreshHosts, 5000)    # 唯一常驻轮询
 ├─ refreshP2PHosts()
 └─ setTimeout(checkSecretaryState, 6000)  # 选举窗口安全网
```

### 1.2 switchTab 分派表 (L1047-1063) — 11/11 全覆盖 ✅

| Tab | 分派 loader | Tab | 分派 loader |
|---|---|---|---|
| station | refreshStation | skills | refreshSkills |
| chat | loadConversations | bot | refreshBotChannels |
| workspace | refreshWorkspace | p2p | refreshP2PHosts |
| hosts | refreshHosts | runtime | refreshRuntime |
| tools | refreshTools | shadowdev | refreshShadowDev |
| resources | refreshResources | | |

Tab 点击源: 桌面 `.tab[data-tab]` + 移动 `.mobile-nav-item[data-tab]` 双侧
addEventListener → 同一 `switchTab(tab)`; 两端 Tab 集合 1:1 同步 (11=11, 含新增
shadowdev) ✅。secretary-only Tab (chat/workspace/hosts/tools/resources) 由
`updateSecretaryUI()` 统一显隐, 桌面/移动两侧同选择器处理 ✅。

### 1.3 WS 事件处理链 (双层)

**顶层 `msg.type` (L1079-1124)**:

| WS 类型 | 触发动作 |
|---|---|
| hosts / heartbeat / host_registered | refreshHosts |
| task_submitted / task_updated / task_deleted | refreshTasks + renderWsOverview |
| task_delivered | toast(含 acceptance_review 摘要) + refreshTasks |
| task_cancelled / task_paused | toast + refreshTasks (+refreshPMAgents) |
| agent_registered | refreshAgents |
| project_created / updated / archived | refreshProjects |
| secretary_activated / deactivated / yielded | updateSecretaryUI + role-badge |
| secretary_assigned / revoked | refreshStation |
| skill_assigned / revoked / scanned / installed / uninstalled | refreshSkills |
| chat_reply | 聊天气泡渲染 |
| pm_registered | refreshPMAgents + refreshTasks + loadPMThreads |
| pm_status_change | refreshPMAgents + onPMThreadStatusChange (+failed/cancelled/awaiting_input toast) |
| team_update | refreshPMAgents |
| pm_thread_attached / pm_thread_message | onPMThreadAttached / noop |
| progress_report | _wsReports 环形缓存 + refreshPMAgents + renderWsActivity |
| p2p_chat | onP2PChatMessage |
| event | → onStationEvent (事件总线二层分发) |

**二层 `onStationEvent(evt.type)` (L1135-1175)**:

| 事件子类型 | 触发动作 |
|---|---|
| usage_reported / resource_config / resource_alert | refreshResources (+预警/热重载 toast) |
| version_upgrade_notice | showUpgradeBanner |
| workstation_optimization_* (×4) | handleOptEvent |
| task_stall_alert | toast + loadTaskFlowList + **loadStallAlerts** (iter-91 联动) |
| error_captured | loadErrors (守卫失效, 见 F4) |
| error_burst | toast + loadErrors |
| cost_budget_warning | toast + refreshTasks |

### 1.4 定时器清单

| 定时器 | 周期 | 备注 |
|---|---|---|
| refreshHosts | 5s | 全局常驻 |
| StationPage /health 轮询 (SPA) | 5s | 组件卸载时清理 ✅ |
| optGuardTimer | — | 仅 opt mock 模式, 真实模式自杀 ✅ |
| checkSecretaryState | 6s 单次 | 选举安全网 |

---

## 二、dashboard.html UI 关系树 (Tab → 面板 → 区块 → 控件 → 函数 → 端点)

```
🏢 station (panel-station, 默认 active)
 ├─ toolbar: 刷新→refreshStation | 重新评级→recomputeRatings | 秘书开关→toggleSecretary
 ├─ #stat-cards / #opt-status-card (openOptFromStation→优化面板)
 ├─ 舰队表 #fleet-body ← refreshStation → /api/hosts, /api/station/status ...
 └─ 最近事件 #event-list ← WS event + refreshStation

🧩 skills (panel-skills)
 ├─ toolbar: refreshSkills | scanSkills | showSkillMarket | #skill-role-filter(onchange)
 └─ 技能表 ← refreshSkills → /api/skills*

💬 chat* (panel-chat, secretary-only)
 ├─ 会话列表: createConversation / deleteCurrentConv / clearChat
 ├─ 消息流 + sendChatMessage → /api/secretary/chat (WS chat_reply 回渲染)
 └─ 优化讨论区: toggleOptPanel / optDiscussToggle / optDiscussSend

🗂️ workspace* (panel-workspace, secretary-only)
 ├─ 视图切换 switchWsView; 任务提交 submitTask → /api/tasks
 ├─ 项目: showCreateProject / onWsProjectChange ← WS project_*
 └─ DAG: loadDAGFromSelect / dagAddNode / dagToggleConnect / dagDeleteSelected /
     saveDAG → /api/tasks/{id}/graph

🖥️ hosts* (panel-hosts, secretary-only)
 └─ probeIP / showNetworkInfo ← refreshHosts → /api/hosts

🔧 tools* (panel-tools, secretary-only)
 ├─ MCP Servers #server-grid: showRegisterServer
 └─ 工具列表 #tool-grid ← refreshTools → /api/tools, /api/mcp/*

📦 resources* (panel-resources, secretary-only)
 ├─ 轮换调度 / 成本分摊 区块 ← refreshResources → /api/resources/*
 ├─ showResourceConfig; WS usage_reported/resource_* 实时刷新
 └─ (R5 同模型多池择优 / 按任务成本分摊)

🤖 bot (panel-bot)
 ├─ 消息通道 #bot-channel-list: showAddBotChannel ← refreshBotChannels → /api/bot/*
 └─ 命令帮助 / 推送事件

🔗 p2p (panel-p2p)
 └─ loadP2PMessages / sendP2PMessage / sendP2PFile ← refreshP2PHosts → /api/p2p/*
     (WS p2p_chat → onP2PChatMessage)

📈 runtime (panel-runtime)  ← refreshRuntime
 ├─ 📊 LLM 调用指标 #runtime-stat-cards      → /api/runtime/metrics
 ├─ 🧠 按模型统计 #runtime-model-table       → /api/runtime/metrics
 ├─ 📝 调用明细 #runtime-call-table          → /api/runtime/calls
 ├─ 🧵 子任务追踪 #runtime-trace-list        → /api/runtime/trace
 ├─ 📋 任务流总览 #taskflow-list             → /api/runtime/task-flow-list
 │    └─ queryTaskFlow → renderTaskFlow #taskflow-box
 ├─ 🚨 停滞告警 (iter-91 UI-065)
 │    ├─ #stall-watch-info (textContent, 守护状态/间隔/阈值)
 │    ├─ 「🔔 立即巡检」→ checkStallNow → POST /api/runtime/task-stall-alerts/check
 │    └─ #stall-alert-list ← loadStallAlerts → GET /api/runtime/task-stall-alerts
 │         (触发源: refreshRuntime + WS task_stall_alert + 巡检后)
 ├─ 🧠 任务记忆 #task-memory-panel           → /api/runtime/task-memory*
 └─ 🐞 错误追踪 #error-panel ← loadErrors    → /api/runtime/errors*

🕶️ shadowdev (panel-shadowdev, iter-91 UI-064)  ← refreshShadowDev
 ├─ 🛡️ 守护状态 #shadow-guardian ← renderShadowGuardian(guardian{running,busy,queued,stopping})
 ├─ 📝 提交表单: #shadow-task + #shadow-backend + #shadow-timeout
 │    └─ submitShadowRun → POST /api/shadow-dev/runs (202/409/422/503 分支)
 └─ 📜 队列历史 #shadow-run-list ← renderShadowRuns → GET /api/shadow-dev/runs
      ├─ 行点击 run_id → copyTaskId
      └─ 「详情」→ openShadowDetail → GET /api/shadow-dev/runs/{id} (encodeURIComponent)
           └─ renderShadowDetail: 门禁 kv-grid / Agent / Diff / 异常 四段 (modal)
      503 → renderShadowDegraded (守护未启动降级态)
```

`*` = secretary-only Tab。后端字段契约已逐一核对: `stall_watcher_status()`
返回 `{watching, interval, stall_minutes}`、告警记录 `{task_id, level, last_stage,
last_label, idle_min, message}`、shadow guardian/run 记录字段 — **与前端消费完全一致** ✅

---

## 三、webui SPA 树 (hash 路由)

```
App (App.tsx, hash router parseHash)
 ├─ 顶栏: NAV(4) + WsStatus(独立 /ws 连接) + auth-chip
 │    └─ role-badge → setAuthOpen → loginUser/logoutUser/refreshRole
 │         → GET /api/station/auth-token (getRole)
 ├─ #/station → StationPage: /health 5s 轮询 + OptimizationCard
 │    └─ optGetItems/optDecide → /api/workstation-optimization/items[/{id}/decision]
 ├─ #/tasks → TasksPage: GET /api/tasks
 ├─ #/dag[/{taskId}] → DagEditorPage (key=role 身份切换重挂载): /api/tasks/{id}/graph
 └─ #/users → UsersPage (key=role): /api/station/users[/{...}]
```

SPA 全量 JSX 渲染, **零 `innerHTML`/`dangerouslySetInnerHTML`** (grep 0 命中) →
React 自动转义, 无 DOM XSS 面 ✅。

---

## 四、一致性检查结果 (全部通过项)

| 检查项 | 结果 |
|---|---|
| HTML 内联 handler → JS 函数定义 (悬挂引用) | ✅ 0 个 (唯一命中 `if` 为 `onkeydown="if(...)"` 正则噪声) |
| JS getElementById → HTML id (缺失 DOM) | ✅ 0 个 (`typing-indicator` 为动态创建 + `if(el)` 守卫) |
| data-tab → panel-{tab} 配对 | ✅ 11/11 (station 面板 `class="panel active"` 初判误报, 实存 L740) |
| switchTab 分派覆盖 | ✅ 11/11 面板均有 loader |
| 桌面 tabs ↔ 移动 nav 同步 | ✅ 双侧各 11 项, secretary 标记一致 |
| WS task_stall_alert → 新面板联动 | ✅ loadTaskFlowList + loadStallAlerts (L1156-1157) |
| 新面板后端契约字段 | ✅ 与 runtime_trace.py / shadow_dev.py 逐字段一致 |
| openShadowDetail URL 注入 | ✅ encodeURIComponent(runId) |
| 新面板自由文本转义 (task/message/label/detail/error/shadow_path) | ✅ 全部 esc() |
| SPA innerHTML 使用 | ✅ 0 处 |
| WS 断线重连 | ✅ onclose 3s 重试 + onopen refreshAllState |

---

## 五、漏洞发现 (按严重度)

### F1 [低·潜在 DOM XSS 模式] `esc()` 用于 onclick 内 JS 字符串上下文 (5 处)

HTML 属性值**先做实体解码再执行 JS**: `onclick="copyTaskId('${esc(x)}')"` 中若 x 含
`'`, esc 产出的 `&#39;` 会被解码回 `'` → JS 字符串提前终止 → 可注入
`a');alert(1);//` 类载荷。项目在 L3118-3120 已有 `escJs()` 及注释明确此约定
(任务名/澄清选项等自由文本均已正确使用 escJs), 但 **ID 类插值** 5 处仍用 esc:

- L3813 `copyTaskId('${esc(t.task_id)}')` (任务流总览, iter-91 前已存在)
- L3854 `copyTaskId('${esc(a.task_id)}')` (停滞告警, iter-91 新增)
- L3933/3939 `copyTaskId/openShadowDetail('${esc(x.run_id)}')` (影子队列, iter-91 新增)
- L4152 `copyTaskId('${esc(c.task_id)}')` (调用明细, iter-91 前已存在)

**当前可利用性 ≈ 0**: task_id/run_id 均为服务端生成的 uuid/编号格式, 不含引号。
属**模式级隐患**: 一旦未来 ID 变为用户可命名 (如自定义任务别名做主键), 即成真实
XSS 向量。**建议**: 5 处统一换 `escJs()` (纯前端, Quest 可修, 半小时内含验证)。

### F2 [低] `_shadowStatusBadge` fallback 未转义 (L3877)

`m[s]||['❓ '+s,...]` — 未知 status 原样插入 innerHTML。status 来自 report.json
(服务端枚举写入), 但该文件**落盘可被本机其他进程篡改**, 篡改后即注入。
建议 `esc(s)`。同类: L3855 `L${a.level}` (level 为 int, 风险≈0, 可顺手加固)。

### F3 [低] `error_captured` 的 Tab 守卫失效 (L1161)

```js
if(typeof loadErrors==='function'&&document.getElementById('error-panel')){loadErrors();}
```
注释意图「仅当前在运行时 Tab 时避免无谓请求」, 但 `#error-panel` 是**静态 HTML
恒存在**, 守卫永真 → 每条 error_captured 都触发 loadErrors, 与注释意图不符。
应改为 `document.getElementById('panel-runtime').classList.contains('active')`。
(error_burst L1167 无守卫属有意为之, 突发告警应无条件刷新, 不算问题。)

### F4 [低·UX] 手动巡检双 toast

「🔔 立即巡检」命中推送时: 按钮 toast (巡检完成, 新推送 N 条) + WS
`task_stall_alert` toast (⚠️ N 个任务可能停滞) 同屏出现。此行为**符合 backlog 规格**
(「已有 WS toast 保持不变」), 但体验上重复。可选优化: 巡检后 3s 内抑制 WS stall
toast, 或接受现状。

### F5 [低·移动布局] `.mobile-nav` 无溢出策略

秘书激活时底部导航 11 项, `flex:1` 无 `overflow-x`/`min-width:0`, 390px 屏每项
≈35px; 英文标签 "Station" (~38px@10px 字号) 可能换行使导航增高 (fixed 定位,
面板 padding-bottom 80px 尚可容纳, 未破版)。桌面 `.tabs` 已有 `overflow-x:auto` ✅。
建议: `.mobile-nav{overflow-x:auto}` + `.mobile-nav-item{min-width:0;flex:1 0 auto}`
或缩短标签 (「Station」→「主站」)。

### F6 [信息] shadowdev 面板无实时刷新通道

后端 shadow-dev 无 WS 事件 (确认 station_routes_shadow.py 零 broadcast), 面板仅在
切 Tab / 手动刷新 / 提交后拉取; 长任务执行中界面不更新状态 (queued→running→完成)。
符合 backlog 契约 (未列 WS 要求)。若需实时, 属 **[Quest→Codex]** 后端需求:
shadow run 状态变更时 broadcast `shadow_run_update` 事件。

### F7 [信息] WS 重连后 runtime/shadow 面板不刷新

`refreshAllState()` (onopen 触发) 覆盖 9 项主状态, 不含 loadStallAlerts /
refreshShadowDev; 断线期间停留在 runtime/shadowdev Tab 的用户重连后看到旧数据,
需手动刷新。影响轻微 (切 Tab 即刷新), 可选: refreshAllState 末尾按当前 active
Tab 补一次对应 loader。

---

## 六、修复归属与建议排期

| 编号 | 归属 | 改动面 | 状态 |
|---|---|---|---|
| F1/F2/F3/F5 | Quest (纯 dashboard.html) | ~10 行 | ✅ 已修复 (iter-91 同轮, 见「七」) |
| F4/F7 | Quest | ~5 行 | ✅ 已实施 (可选优化一并落地) |
| F6 | [Quest→Codex] 后端 WS 事件 | shadow_dev.py + 路由 | ⏳ 已转 loop_status notes 待 Codex 排期 |

---

## 七、修复实施记录 (iter-91 同轮, 2026-09-09)

| 项 | 实施内容 |
|---|---|
| F1 | 5 处 onclick JS 字符串 ID 插值 `esc()`→`escJs()` (任务流总览/停滞告警/影子队列×2/调用明细); title 属性保持 esc (HTML 属性上下文正确用法) |
| F2 | `_shadowStatusBadge` fallback 改 `'❓ '+esc(s)`; 停滞级别改 `L${Number(a.level)||0}` 数字化 |
| F3 | `error_captured` 守卫改为 `panel-runtime.classList.contains('active')`, 与注释意图一致 |
| F4 | `checkStallNow` 成功后置 `window._stallManualTs`, WS stall toast 在 4s 窗口内抑制 (列表照常刷新) |
| F5 | `.mobile-nav` 加 `overflow-x:auto`; `.mobile-nav-item` 改 `flex:1 0 auto;min-width:52px;white-space:nowrap` — 6 项时仍铺满, 11 项时横向滚动 |
| F7 | `refreshAllState()` 末尾按 `.panel.active` 补刷 refreshRuntime / refreshShadowDev |

**验证**: node --check 通过; Playwright+Chromium mock 回归 **19/19 PASS** — 含带单引号
 task_id/run_id 实点 (onclick 属性值 `copyTaskId('T-9\'q')` 无截断)、恶意 status
 `<img onerror>` 注入实测 `window.__pwn===undefined`、WS 推送实测 error_captured
 守卫双路径 (非 runtime Tab 零请求 / active 时命中)、toast 抑制窗口双态、
 390px 导航 scrollWidth 572>clientWidth 390 可滚动且单行高 60px、全程零未捕获异常
 (截图 temp_resault/ui_audit_f5_mobile_nav.png)。
**登记**: test_checklist UI-066~068 (自增编号, 与 backlog 文档的 UI-066~068 为两套
编号体系) 已标记检测通过。
