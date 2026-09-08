# UI 待开发项交接清单（Codex → Quest）

- 出具方：Codex CLI ・ 日期：2026-09-08 ・ 基线：`iter-90` 完成后（pytest 463 passed）
- 归属依据：`AGENT_LOCKS.md` —— `webui/**`、`dashboard.html` 前端项归 Quest；
  `lan_mesh/**.py` 后端归 Codex。本清单只描述**已就绪的后端契约 + 期望的前端行为**，
  不含任何后端改动要求；若某项确实缺后端字段，在下方「需 Codex 补后端」栏注明。
- 盘点方法：抽取 `lan_mesh/**.py` 全部 181 个路由，与 `dashboard.html` + `webui/src/**`
  的实际调用做路径正则比对，剔除节点间内部协议（`/api/heartbeat`、`/api/register`、
  `/pm/*`、`/role/*`、`/api/secrets/*` 等），得到下列**前端零覆盖**能力。

## 一、优先级总览

| 优先 | 编号 | 主题 | 载体 | 后端就绪 |
|---|---|---|---|---|
| P0 | UI-064 | 影子开发面板（提交/队列/报告/守护） | dashboard | ✅ |
| P0 | UI-065 | 任务停滞主动告警面板与手动巡检 | dashboard | ✅ |
| P1 | UI-066 | 任务记忆统计与成本预估/断点视图 | dashboard | ✅ |
| P1 | UI-067 | 团队与子 Agent 拓扑视图 | dashboard | ✅ |
| P1 | UI-068 | 运行时 JSONL 统计（stats）面板 | dashboard | ✅ |
| P2 | UI-069 | 云存储同步状态卡（状态/手动同步/连通性测试） | dashboard | ✅ |
| P2 | UI-070 | 路由 dry-run 调试台 | dashboard | ✅ |
| P2 | UI-071 | 日志容量修剪运维入口 | dashboard | ✅ |
| P2 | UI-072 | 联邦信息卡（本站联邦身份与对端） | dashboard | ✅ |
| P2 | UI-073 | 对话管理增强（改标题/解绑 PM 线程） | dashboard | ✅ |
| P3 | UI-074 | SPA 侧补齐项目与蓝图页 | webui | ✅ |
| P3 | UI-075 | SPA 侧补齐交付验收（含 iter-90 自检） | webui | ✅ |

## 二、逐项交付要求

### UI-064 影子开发面板（P0）

后端契约（`lan_mesh/station_routes_shadow.py`）：

- `POST /api/shadow-dev/runs` → 202，body `{task: str(1..4000), backend?: str, timeout?: 30..1800}`；
  返回 run 记录（含 `run_id`）。422=参数非法，409=队列冲突，503=守护未初始化。
- `GET /api/shadow-dev/runs` → `{runs: [...], guardian: {...}}`
- `GET /api/shadow-dev/runs/{run_id}` → 单次报告，404=不存在
- `GET /api/shadow-dev/status` → 守护状态

期望前端行为：

- 新增「影子开发」子视图：任务提交表单（描述 + backend 下拉 + 超时）→ 提交后进队列列表。
- 队列/历史列表：`run_id` 短码、状态、开始/结束时间、耗时；点击进详情抽屉展示完整报告。
- 守护状态条：运行中/空闲、当前串行执行项；503 时整块降级为「守护未启动」提示而非报错。
- 校验前置：`task` 为空或超 4000 字符时前端拦截，不发请求。

### UI-065 任务停滞告警面板（P0）

后端契约（`lan_mesh/station_routes_basic.py`）：

- `GET /api/runtime/task-stall-alerts` → `{alerts: [{task_id, level, last_stage, last_label, idle_min, message}], watching, interval, stall_minutes}`
- `POST /api/runtime/task-stall-alerts/check` → `{pushed: [...]}`（手动触发一轮巡检）

期望前端行为：

- 运行时 Tab 增「停滞告警」区：守护状态（watching/interval/阈值）+ 当前活跃告警表。
- 「立即巡检」按钮调 check，按 `pushed` 数量给 ok/info toast 后刷新列表。
- 已有 WS `task_stall_alert` toast 保持不变，本面板作为可回溯的常驻视图。

### UI-066 任务记忆统计与成本/断点视图（P1）

- `GET /api/task-memory/stats?task_type=` → 同类任务总数/成功率/平均耗时/推荐协作模式/常见错误
- `GET /api/tasks/{task_id}/cost-estimate` → 预估 token 与预算适配
- `GET /api/tasks/{task_id}/checkpoints` → `{checkpoints: [...], total: n}`

期望前端行为：

- 现有任务记忆面板补「按 task_type 下钻」：选类型后展示统计卡与常见错误 Top。
- 任务详情弹窗补「成本预估」与「断点（checkpoints）」两段；无数据时静默隐藏。
- 断点列表提供跳转「断点恢复」（复用已有 `POST /api/tasks/{id}/resume`）。

### UI-067 团队与子 Agent 拓扑（P1）

- `GET /api/teams` → 团队列表；`GET /api/teams/{team_id}` → 团队详情（成员/子 Agent）
- `GET /api/agents/{agent_id}` → 单 Agent 详情

期望前端行为：PM → 团队 → 子 Agent 三层折叠视图；成员点击弹 Agent 详情（技能/状态/所在主机）。

### UI-068 运行时 JSONL 统计面板（P1）

- `GET /api/runtime/stats?hours=0.1..168` → 子任务成功率、模型分布、错误 Top5

期望前端行为：运行时 Tab 增「执行统计」区，时间窗切换（1h/6h/24h/7d）；与既有 `/api/runtime/metrics`（SQLite 审计）并列展示，标题需标明数据源差异。

### UI-069 云存储同步状态卡（P2）

- `GET /api/cloud-sync/status` → `{enabled, configured, running, message}` 或完整状态
- `POST /api/cloud-sync/sync` → 手动同步结果（同时广播 WS `cloud_sync`）
- `POST /api/cloud-sync/test` → 连通性测试结果；未启动时两个 POST 返回 503

期望前端行为：资源/配置页增状态卡（启用/已配置/运行中）+「立即同步」「测试连接」按钮；503 降级为「未启用」说明并禁用按钮；监听 WS `cloud_sync` 刷新。

### UI-070 路由 dry-run 调试台（P2）

- `POST /api/route/dry-run`（`lan_mesh/station_routes_projects.py`）

期望前端行为：输入任务描述/技能/项目，展示将命中的模型池、fallback 链与预估成本；面向选型排查，只读不落库。

### UI-071 日志修剪运维入口（P2）

- `POST /api/runtime/logs/prune?days=1..365` → `{pruned: {表: 行数}, vacuum: bool}`

期望前端行为：运维区按钮 + 保留天数输入；执行前二次确认（不可恢复），结果以表格展示各表删除行数与 VACUUM 结果。

### UI-072 联邦信息卡（P2）

- `GET /api/federation/info` → 本站联邦身份与对端信息

期望前端行为：Station 页联邦卡，展示联邦名/本站角色/已知对端；与已有 🌐 联邦徽标（UI-054）呼应。

### UI-073 对话管理增强（P2）

- `PUT /api/conversations/{conv_id}/title`
- `DELETE /api/conversations/{conv_id}/pm-threads/{pm_id}`

期望前端行为：对话列表支持重命名（内联编辑）；对话内 PM 线程支持解绑，解绑前二次确认。

### UI-074 / UI-075 SPA 补齐（P3）

SPA（`webui/src`）当前只有 Station/Tasks/DAG/Users 四页，下列旧版已有能力尚未同步：

- UI-074：项目列表 + 项目蓝图查看/编辑（`GET/PUT /api/projects/{id}/blueprint`，iter-88）
- UI-075：交付物查看与验收/退回（`/api/tasks/{id}/accept|reject`），
  **含 iter-90 新增 `output_data._delivery.acceptance_review`**：
  `{verdict: 'pass'|'risk', reviewed: n, unmet: [...], summary, checks: [{criterion, met, evidence}]}`；
  参照旧版 `dashboard.html` 的 `showDelivery()` 与任务卡片徽章实现。

## 三、验收与登记要求（对齐 AGENTS.md / ui-change-checklist）

1. 每项在 `test_bug/test_checklist.csv` 登记一行，编号沿用本清单（UI-064…UI-075），
   CSV 必须 **11 列**（近期有过 12 列越界，务必核对）。
2. 浏览器实测：Playwright/CDP headless，桌面 1366x900 + 移动 390x844 两档；
   截图存 `temp_resault/`，文件名带 UI 编号。
3. 每项必须覆盖三态：正常数据 / 空数据 / 接口失败（含 503 降级），失败态不得白屏或抛未捕获异常。
4. 改 `dashboard.html` 后跑 JS 语法检查（抽取 `<script>` 后 `node --check`）；改 `webui/` 后
   `npm run build` 并把产物入库（`lan_mesh/web/static/spa/`）。
5. 后端零改动即可完成上述全部项；若发现字段缺失，请写进 `loop_status.json.notes`
   并标 `[Quest→Codex]`，由 Codex 下一轮补，不要直接改 `lan_mesh/**.py`。

## 四、需 Codex 补后端（当前为空）

暂无。本清单 12 项的后端契约均已就绪并有测试覆盖。
