# 09 Web 前端

单文件深色仪表盘，多 Tab 布局，与 station_api 的 REST/WebSocket 对接。
iter-56 (F5.1) 起新增 React SPA 新版（webui/），与旧版仪表盘并存演进。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| lan_mesh/web/static/ | CSS/JS 静态资源 (含 spa/ React 构建产物) |
| lan_mesh/web/templates/dashboard.html | Station Web 控制台 (10 Tab, 含运行时性能) |
| webui/ | React SPA 源码 (Vite+TS+xyflow, 构建产物 → web/static/spa/) |
<!-- /AUTO:module-list -->
---

## dashboard.html — 仪表盘单文件

**结构**: 单文件内联 CSS/JS（无构建步骤，便于模板渲染与分发）。

**10 Tab**: Station 总览 / 技能库 / 秘书对话（L1 项目对话 + L2 PM 线程）/
项目工作台 / Work Station 主机 / MCP工具 / 模型资源（配置向导 + 余额 + 消费）/
手机通道 / 主机通讯 / 运行时性能（其中秘书相关 Tab 激活后才显示）。

**运行时性能 Tab** (iter-37, 📈):
- 数据源: `/api/runtime/metrics` (SQLite 聚合) + `/api/runtime/calls` (调用明细) + `/api/runtime/trace` (JSONL 轨迹)
- 指标卡: 调用总数/平均延迟(含P99)/平均TTFT/Token 总量, 时间窗可选 (1h/6h/24h/3d/7d)
- 表格: 按模型统计 (调用数降序) + 调用明细 50 条 + 子任务轨迹 30 条 (过滤 subtask_start 无终态记录)
- 渲染函数: `refreshRuntime()` / `renderRuntimeMetrics()` / `renderRuntimeCalls()` / `renderRuntimeTrace()`

**任务流总览 + 瀑布** (iter-38~41, P3):
- 数据源: `/api/runtime/task-flow-list` (最近任务阶段聚合总览) + `/api/runtime/task-flow?task_id=` (单任务瀑布)
- 总览表 `loadTaskFlowList()` / `renderTaskFlowList()`: 任务(点击复制)/最新阶段徽章/状态/阶段数/总耗时/末活动/「瀑布」按钮 `jumpTaskFlow()` 自动填充并查询；状态列三态 ✅已收尾/🔄进行中/⚠️可能停滞 (iter-40 停滞检测: 未到终态且超 30 分钟无事件, 红色带空闲时长提示) + 顶部红色停滞告警横幅；iter-41 实时告警: `onStationEvent` 收 `task_stall_alert` 事件 → toast 提示停滞任务数+ID (Lv3 红色) + 自动调 `loadTaskFlowList()` 刷新总览表 (无需手动切 Tab)
- 瀑布: `queryTaskFlow()` / `renderTaskFlow()` — 阶段徽章 + 详情 + 间隔(gap_ms) + 总耗时；空输入与未知任务均渲染占位提示；调用明细表任务列点击 `copyTaskId()` 复制

**任务记忆面板** (iter-42, F4.1 可视化):
- 数据源: `/api/task-memory/overview?limit=10` (全局统计 + 按类型分组 + 最近沉淀)
- `loadTaskMemory()` / `renderTaskMemory()`: 统计卡片 (记忆总数/总体成功率/推荐协作模式/常见错误预警) + 按类型分组表 (成功率绿/橙分级, 耗时自动换算 m/s) + 最近沉淀列表 (关键词截断, 失败记录 hover 显示 error_pattern)；空记忆渲染引导提示；秘书未激活 503 时降级显示「加载失败: 503 (秘书未激活)」不报错

**错误追踪面板** (iter-44, F1.4 可视化):
- 数据源: `/api/errors/stats` + `/api/errors/recent?limit=15` 并行拉取 (复用既有 F1.4 端点)
- `loadErrors()` / `renderErrors()`: 统计卡片 (错误总数 绿/橙分级/告警窗口计数 vs 阈值 超限红/按模块分布) + 最近错误表 (倒序, 消息截断 hover 全文)；空记录渲染引导提示
- 实时联动: `onStationEvent` 收 `error_captured` → 面板刷新；收 `error_burst` → 红色 toast 告警 + 刷新；`refreshRuntime()` 挂载 `loadErrors()`
- 自愈诊断建议区 (iter-46, F4.2): `loadErrors()` 并行拉取 `/api/errors/diagnosis?window=200` (失败降级为无建议不影响主面板)；`renderErrors()` 渲染 🔧 诊断区 — 按模式分组卡片 (超时/连接/认证/限流/上游5xx 图标徽章 + 命中数 + 影响模块 + 建议文案, hover 显示样例消息), 命中数降序；无命中显示扫描统计提示, 空缓冲整区隐藏
- 持久化历史区 (iter-47): `loadErrors()` 并行拉取 `/api/errors/history?limit=20` (失败降级隐藏该区块)；`renderErrors()` 在实时错误表后渲染 📜 持久化历史表 (时间/模块/类型蓝紫 #74c0fc/消息, 倒序展示, 跨重启保留)；空缓冲时历史区仍显示 (与进程内缓冲解耦)
- 历史诊断区 (iter-48): `loadErrors()` 并行拉取 `/api/errors/diagnosis?source=history&window=200` (失败降级隐藏)；诊断卡片渲染抽为 `mkDiag()` 同构渲染器复用于缓冲诊断 (🔧) 与历史诊断 (🗂) 双区块 — 重启后缓冲空时历史诊断仍展示分组建议, 诊断不断档；iter-49 起每张诊断卡片右侧增绿色 🩹 执行按钮 (`runHeal()` → `POST /api/errors/heal?action=&category=`, Toast 结果后刷新面板)
- 自愈执行历史区 (iter-49): `loadErrors()` 并行拉取 `/api/errors/heal/history?limit=10` (失败降级隐藏)；`renderErrors()` 渲染 🩹 自愈执行历史表 (时间/动作/类别/结果徽标 ✅已执行·❌失败·🙋需人工/详情, 倒序展示, heal_log 跨重启保留)
- 自动自愈状态条 (iter-50): `loadErrors()` 并行拉取 `/api/errors/heal/status` (失败降级隐藏)；`renderErrors()` 渲染 🛡 状态条 — 已启用(绿)/已禁用(灰) + 周期/冷却 + 已扫描轮次 + 🔍 立即检查按钮 (`runAutoHealCheck()` → `POST /api/errors/heal/auto-check`, 有执行 Toast ok/无执行 Toast info 后刷新面板)

**DAG 图编辑面板** (iter-51, F4.3, 项目工作台 📈 DAG 子视图):
- 数据源: `GET /api/tasks/{tid}/graph` 加载 (dagNodes/dagEdges, SVG 渲染 + 连线模式 dagConnectMode + 节点拖拽)
- `saveDAG()` 保存已恢复: 收集当前节点/边 → `PUT /api/tasks/{tid}/graph` 回写 → 成功 Toast ✅/失败 Toast (环或非 pending 提示); 新节点 ID 改 st- 前缀 (`dagAddNode()`); 需浏览器强刷清除旧版缓存 (旧版仍提示「手工保存已停用」)

**任务卡片成本预估徽章** (iter-52, F4.4, 项目工作台任务卡片):
- `renderTasks()` 卡片 meta 行读取 `input_data._cost_estimate` →
  💰 ~token 徽章 (`fmtTokens()`: 千分位 K/百万 M 缩写) + 预算适配状态小徽章
  (✅ 充足 / ⚠️ 紧张 / ❌ 不足, hover 显示建议文案; unknown 时不显示小徽章)
- `onStationEvent` 收 `cost_budget_warning` → info toast (任务名/预估
  tokens/状态) + 自动 `refreshTasks()` 刷新卡片

**关键渲染函数**（改动时注意同步更新本文档）:
- `renderHosts()`: 主机卡片 + 统计行（含版本分布 `vMap`/`vTxt`，
  多版本告警色；S3 新增 ✅最新/⚠️落后/未知版本 标记）
- `showHostDetail()`: 主机详情弹窗（kv-grid，含代码版本行）
- WebSocket `/ws`: event_bus 事件实时推送入口

**近期增量**:
- S2: 版本分布统计行
- S3: 卡片版本标记 + 详情版本时间
- UI 变更须在 `test_bug/test_checklist.csv` 登记（UI-0xx 编号），
  并经 Browser 实测（截图存 temp_resault/）

**前端已知坑**: 秘书回复双渲染问题（历史修复）、删除任务后需主动刷新列表。

## React SPA 新版 — webui/ (iter-56, F5.1)

**技术栈**: Vite 5 + React 18 + TypeScript 5.6 + @xyflow/react 12（DAG 画布）。

**挂载**: 构建产物输出到 `lan_mesh/web/static/spa/`，服务端
`app.mount("/spa", StaticFiles(html=True))` 静态托管；hash 路由
（`#/station` `#/tasks` `#/dag[/:taskId]`）免服务端 fallback 配置；
`/spa` 路径加入 `station_routes_common` 认证白名单放行。

**认证**: `GET /api/station/auth-token` → `localStorage('lan_mesh_token')` →
`apiFetch` 自动注入 Bearer（与旧版 dashboard 同模式）。

**多用户权限** (iter-58, F5.2): 用户个人 token
(`lan_mesh_user_token`) 优先于 mesh token; auth-token 回显角色
(`lan_mesh_role`) — 顶栏角色徽章 (boss/operator/viewer/未登录) +
点击弹出身份切换面板 (token 输入/切换/退出); `ensureMeshToken`
共享 in-flight Promise 防竞态 (并发调用方 await 同一请求, 修复
未登录误显 boss); DAG 编辑器 viewer 与未登录只读 (保存/加节点
禁用 + 黄色提示, key=role 重挂载联动), 服务端角色校验兜底。
多用户模式下未登录不获 mesh_token (auth-token 收紧), 需先登录。

**三页面**:
- Station 总览（`StationPage`）: `/api/health` 轮询 + 健康徽章
- 任务列表（`TasksPage`）: `/api/tasks` + WS `task_updated` 300ms 防抖刷新，
  7 项状态筛选 + 状态色徽章 + 行内「DAG」跳转 `#/dag/{taskId}`
- DAG 编辑器（`DagEditorPage`）: `GET /api/tasks/{id}/graph` 加载渲染
  （自定义节点: 状态色边框 + 圆点 + skill 标签），加节点/连线后
  `PUT /api/tasks/{id}/graph` 保存（仅 pending 可编辑 + 环检测拒绝）

**入口**: 旧版 dashboard 顶栏「⚛️ SPA 新版」按钮新窗口打开 `/spa/`；
preflight `_check_spa_bundle` 检查构建产物（缺失仅提示不阻断）。

**开发**: `cd webui && npm run dev`（vite proxy `/api` `/ws` → 127.0.0.1:45500），
改动后 `npm run build` 重新生成产物（产物入库）。

## 移动端 PWA — Service Worker (iter-62, F5.4)

**定位**: 免 SDK 的移动端方案（保留 RN/Flutter 选项为后续扩展）：
manifest.json PWA 声明 + Service Worker 离线壳。

**sw.js** (`lan_mesh/web/static/sw.js`, 缓存名 `lan-mesh-shell-v1`):
- `install`: `cache.addAll(['/', '/static/manifest.json'])` + skipWaiting
- `activate`: 清旧缓存 + clients.claim（首装后立即接管）
- `fetch` 三策略: `/api/` 一律 network-only 不缓存;
  导航请求 network-first 回退 `caches.match('/')` 离线壳;
  静态资源 stale-while-revalidate

**挂载与认证**: `/sw.js` 经根路径路由挂载（FileResponse + no-cache 头,
scope 默认 `/`）; SW 注册请求由浏览器发起不带 Authorization 头,
故 `/sw.js` 加入 `_AUTH_WHITELIST`。dashboard 在安全上下文
(https/localhost/127.0.0.1) 才 `navigator.serviceWorker.register('/sw.js')`,
失败静默降级。

**移动端导航**: 640px 断点隐藏 `.tabs`、显示 `.mobile-nav` 底部导航
(桌面由 `@media(min-width:641px)` 隐藏)。iter-62 缺陷修复: 曾存在
普通规则 `.mobile-nav{display:none}` 与断点内 `display:flex` 同特异性且
靠后 → 覆盖移动端显示; 已删除冗余规则 + 回归测试 `test_mobile_nav_css_layering`。

**验证**: CDP 直连真实验证 7/7 (Edge headless +
`Network.emulateNetworkConditions` 真实断网离线壳渲染 +
`Emulation.setDeviceMetricsOverride` 390x844 移动视口底部导航可见),
截图 temp_resault/x62_offline_real.png + x62_mobile_real.png。

## 用户管理页 — SPA #/users (iter-63, 团队场景)

**定位**: 多用户团队的账号管理入口 (F5.2 权限体系的 UI 闭环)。

**路由与导航**: hash 路由 `#/users` (Route = "station"|"tasks"|"dag"|"users");
导航栏新增 👥 用户入口。

**视图按角色分层** (`isBoss = getRole() === "boss"`):
- **boss**: 新增表单 (用户名+角色下拉+新增) → 一次性 token 弹层
  (.overlay/.modal/.token-box, 复制按钮 + 「明文仅展示一次」警示);
  用户表行内角色 select 修改 / 🔄 轮换 / 🗑 移除 (二次确认);
  admin_view 时显示 token 尾 4 位列
- **非 boss (operator/viewer/未登录)**: 只读列表, 无表单/无尾4位/无操作钮

**数据链路**: GET /api/station/users (boss 含 token_tail4, 其余脱敏);
写操作 POST/PUT/DELETE /api/station/users[/{name}/role|rotate-token] —
403/401 由后端 api_guard_middleware 角色检查兜底 (前端只做显隐)。

**验证**: Browser 实测 12/12 (Edge headless CDP 直连, boss/viewer 双视图
+ boss 真实新增 ui63 → token 弹层), 截图 temp_resault/x63_users_*.png;
后端真机 16 项 + 重启持久化 (轮换 token 跨重启保留) 全过。

**联邦徽标** (iter-64, F3.4):
- Station 舰队表格设备名列: `source=fed` 主机显示 🌐 联邦徽标
  (绿底 + title 提示联邦名), lan 主机无徽标; 徽标行内不换行

**任务卡片联邦转发徽标** (iter-65, F3.4 遗留):
- 项目工作台任务卡片: `status=forwarded` 时标题栏显示 ↗ 联邦转发 徽标
  (suspended 样式 + title=已委托联邦对端执行), 紧随 forwarded 状态徽标;
  非 forwarded 任务无徽标; 徽标行内不换行

## 工作站优化 UI (iter-72)

**定位**: 常驻「自我优化」工作流的 UI 呈现与秘书交互入口。
优化项三类来源 (boss 要求 / bottleneck 瓶颈 / agent 建议),
六种状态 (candidate/waiting_boss/queued/running/completed/rejected)。
后端 API (`/api/workstation-optimization/*`) 由 Codex 实现;
Quest 仅前端, 后端未实现时自动回退 localStorage mock。

**Mock 适配层**: `optIsApiMissing()` (dashboard) / `optRawFetch` (SPA)
判定 404/405/501 → 回退本地存储 (键 `lan_mesh_opt_items_v1`,
两处 UI 共享实现数据互通), 面板显示「本地演示数据」徽标;
其他错误 (401/5xx) → 错误态 + 重试按钮 (未登录 401 不回退 mock 属设计决策)。
mock 守护定时器 5s 推进 queued→running→completed 演示状态流转;
空数组被尊重 (不重新播种), 空队列/加载中/接口错误三态 UI 齐全。

**dashboard 三大区域**:
- Station 首页 `opt-status-card`: 守护状态圆点/队列数/执行中项/
  待 Boss 决策数/最近完成项 + 「查看优化面板」跳转秘书页
- 秘书页 `opt-panel` (🛠️ 优化按钮展开, 待决策角标): 按状态分六组
  展示 (waiting_boss→running→queued→candidate→completed→rejected),
  每条含标题/来源徽标/优先级徽标/状态徽标/创建时间/说明,
  操作: 确认执行/拒绝/补充说明 (clarify modal)/查看详情
- 对话流: `handleOptChatCommand()` 拦截快捷入口
  (「优化工作站:…」直入队/「遇到瓶颈:…」high/「添加优化建议:…」waiting/
  「查看优化…」渲染卡片组) 本地处理不回秘书 API;
  `appendChatMessage` 支持 `extra.type='opt_items'` 渲染优化项卡片组
  (含确认/拒绝/补充按钮, 决策后写回当前对话流)

**SPA 同步** (`OptimizationCard.tsx`): Station 首页卡片位新增
🛠️ 工作站优化卡 — KPI kv (守护/队列/执行中/待决策/最近完成) +
⏳ 待决策列表 (优先级排序前 5 条, 确认/拒绝按钮) + 10s 轮询 +
空/加载/错误三态 + mock 徽标; 完整队列引导回旧版仪表盘面板。

**WS 事件**: `onStationEvent` 已接 `workstation_optimization_created/updated/
waiting_boss/completed` 四事件 → `handleOptEvent()` 按 id 幂等合并
(本地操作与推送同入口, rAF + JSON 快照防闪烁), 后端接通即自动生效。

**iter-72 移动端修复** (CDP 真实 390x844 + safe-area 模拟实测):
聊天输入区曾因 `.chat-layout` 固定高 `calc(100vh-160px)` 未计入底部
导航 (62px) 且断点规则位于普通规则之前被覆盖 (iter-62 同型陷阱),
加 opt-panel 展开后输入条被 chat-container overflow:hidden 裁截。
修复: 断点规则移至普通规则之后
(`height:calc(100vh - 190px - env(safe-area-inset-bottom))`) +
`.chat-container{min-height:96px}` + `.opt-panel{flex-shrink:1}`;
实测 inputAboveNav/inputBelowPanel/inputUsable/inputFocusable 全 true,
桌面 1280x800 回归无影响。

**iter-73 优化讨论窗口 (面板内嵌, 与秘书互动)**:
Boss 反馈「优化 UI 缺与秘书互动的聊天窗口」→ opt-panel 改两段式:
上段优化项队列列表, 下段 `opt-discuss` 讨论区 (头部话题标签 + 「通道待接入」
徽标 + 消息流 + 输入条)。优化项 meta 行新增「💬 讨论」按钮
(`optDiscussSelect(id)`): 切换话题上下文 (卡片 `topic-active` 高亮 +
输入框 placeholder 跟随话题), 话题键为优化项 id 或 `__all__` (总体)。
消息按话题持久化 localStorage (`lan_mesh_opt_discuss_v1`), 折叠按钮
(`optDiscussToggle`) 可收起讨论区; 移动端 640px 断点压缩讨论区高度
(104px) 保证列表可见。

**发送通道契约 (iter-73 后端已接入)**: `optDiscussSend()` 曾为骨架 —
消息暂存本地并提示「通道待接入」。后端约定 (已实现):
`POST /api/secretary/chat` payload 增 `discuss_context: {topic: itemId}`,
后端据此跳过命令式关键词检测 (_ACTION_KEYWORDS 子串匹配会误伤
「状态/帮我写/遇到瓶颈」等讨论文本) 并将话题优化项注入 system prompt;
秘书回复由响应返回 + WS 广播 (`chat_reply`)。
后端已落地 (见 docs/design/06-interaction iter-73 段): `chat_handler.chat` 收
`discuss_context` 后注入话题上下文并跳过命令检测, 返回 `action_taken=opt_discuss`;
UI 侧只需去掉 pending 徽标并改调真实端点即可点亮 (Quest 后续一轮)。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-27 后 | 初建；收录 S2/S3 版本统计 UI |
| 2026-08-25 | iter-37 | 新增 📈 运行时性能 Tab (指标卡/按模型统计/调用明细/子任务轨迹)，UI-035 实测通过 |
| 2026-08-25 | iter-38 | 运行时 Tab 增任务流瀑布查询 (task_id → 生命周期阶段时间线)，UI-036 实测通过 |
| 2026-08-26 | iter-39 | 运行时 Tab 增任务流总览表 (最近任务阶段聚合 + 一键查瀑布)，UI-037 实测通过 |
| 2026-08-26 | iter-40 | 总览表增停滞检测 (状态列三态 + 红色告警横幅)，UI-038 实测通过 |
| 2026-08-26 | iter-41 | 停滞告警实时推送 (WS task_stall_alert → toast + 总览表自动刷新)，UI-039 实测通过 |
| 2026-08-26 | iter-42 | 运行时 Tab 增任务记忆面板 (统计卡片/按类型分组/最近沉淀，503 优雅降级)，UI-040 实测通过 |
| 2026-08-27 | iter-44 | 运行时 Tab 增错误追踪面板 (统计卡片/按模块分布/最近错误表，error_captured/error_burst 事件实时联动)，UI-041 实测通过 |
| 2026-08-27 | iter-46 | 错误面板增 🔧 自愈诊断建议区 (模式分组卡片 + 接口失败降级 + 空缓冲隐藏)，UI-042 实测通过 |
| 2026-08-27 | iter-47 | 错误面板增 📜 持久化历史区 (error_log 表跨重启保留 + 接口失败降级隐藏 + 与进程内缓冲解耦)，UI-043 实测通过 |
| 2026-08-27 | iter-48 | 错误面板增 🗂 历史诊断区 (mkDiag 同构渲染器双源复用 + 重启后诊断不断档)，UI-044 实测通过 |
| 2026-08-27 | iter-49 | 错误面板增 🩹 自愈执行能力 (诊断卡片执行按钮 + 自愈执行历史区 + Toast 结果反馈)，UI-045 实测通过 |
| 2026-08-28 | iter-50 | 错误面板增 🛡 自动自愈守护状态条 (已启用/已禁用 + 周期/冷却/扫描轮次 + 🔍 立即检查按钮)，UI-046 实测通过 |
| 2026-08-28 | iter-51 | DAG 图编辑面板保存恢复 (saveDAG 接 PUT /api/tasks/{tid}/graph 回写 + 新节点 st- 前缀)，UI-047 实测通过 |
| 2026-08-28 | iter-52 | 任务卡片成本预估徽章 (💰 token 徽章 + 预算适配状态小徽章) + cost_budget_warning toast 实时告警，UI-048 实测通过 |
| 2026-08-29 | iter-56 | React SPA 新版 (webui/: Vite+React+TS+xyflow, /spa 挂载 + hash 路由 + 认证白名单; 三页面: Station 总览/任务列表/DAG 编辑器; 旧版顶栏 SPA 入口)，UI-049 实测通过 |
| 2026-08-29 | iter-58 | SPA 多用户权限 (F5.2): 顶栏角色徽章 + 身份切换面板 (用户 token 优先/登录/退出); DAG 编辑器 viewer 与未登录只读; ensureMeshToken 共享 Promise 防竞态，UI-050 实测通过 |
| 2026-08-29 | iter-61 | 技能库 Tab 插件市场 (F5.3): 🛒 市场按钮 + 弹窗列表 (名称/版本/大小/已装标记); 安装/卸载按钮 + 内置/第三方来源徽标 + 空市场引导，UI-051 实测通过 (含 Toast 重复弹出缺陷修复) |
| 2026-08-29 | iter-62 | 移动端 PWA (F5.4): sw.js 离线壳三策略 + /sw.js 根路由挂载 + 认证白名单 + SW 注册脚本; 640px 断点底部导航缺陷修复 (CSS 层叠覆盖)，UI-052 CDP 真实断网+移动视口 7/7 实测通过 |
| 2026-08-29 | iter-63 | SPA 用户管理页 (团队场景): #/users 路由 + 👥 导航; boss 视图新增/改角色/轮换/移除 + 一次性 token 弹层; 非 boss 只读脱敏，UI-053 Browser 12/12 实测通过 |
| 2026-08-29 | iter-64 | Station 舰队表格 🌐 联邦徽标 (F3.4): fed 来源主机设备名旁绿底徽标 + title 联邦名提示; lan 主机无徽标，UI-054 Browser 6/6 实测通过 |
| 2026-08-29 | iter-65 | 任务卡片 ↗ 联邦转发徽标 (F3.4 遗留): forwarded 任务标题栏徽标 + title 提示委托执行，UI-055 Browser 实测通过 (截图 temp_resault/x65_fwd_badge.png) |
| 2026-08-30 | iter-72 | 工作站优化 UI: dashboard 优化面板+Station 状态卡+秘书快捷入口与聊天卡片 (mock 适配层 404/405/501→localStorage 回退) + SPA OptimizationCard 同步 + 移动端聊天输入区让位修复，UI-056/057/058 CDP 桌面+移动实测通过 (截图 temp_resault/x72_opt_*.png) |
| 2026-08-31 | iter-73 | 优化讨论窗口 (面板内嵌): opt-panel 两段式 + 💬 讨论话题切换 (卡片高亮/placeholder 跟随) + 消息按话题 localStorage 持久化 + 折叠/移动端适配; 发送通道骨架暂存 (契约留 Codex: /api/secretary/chat 增 discuss_context)，UI-059 CDP 桌面+移动实测通过 (截图 temp_resault/_x73_desktop.png / _x73_mobile.png) |
| 2026-09-01 | iter-73 | 优化讨论发送通道后端接入 (Codex): /api/secretary/chat 透传 discuss_context → chat_handler 注入话题上下文 + 跳过命令检测 (action_taken=opt_discuss); UI 侧待去掉 pending 徽标改调真实端点 |
