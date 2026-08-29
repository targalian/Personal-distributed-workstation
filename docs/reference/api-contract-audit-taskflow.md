# API 契约审查报告：任务流瀑布端点与运行时 Tab

- 审查日期：2026-08-29
- 审查人：Quest（前端/文档 Agent）
- 范围：`webui/` 前端、`lan_mesh/web/templates/dashboard.html`（Dashboard 运行时 Tab）
  与 `lan_mesh/station_routes_basic.py`（及其依赖的 `runtime_trace.py` / `database.py` /
  `error_tracker.py`）之间的 API 契约一致性
- 结论：**契约一致，无破坏性不一致**。前端引用的全部字段均在后端响应中存在，
  未发现「前端引用不存在的字段」。

## 1. 背景与范围澄清

### 1.1 前端有两条线，任务流只挂在单文件 Dashboard 上

- `webui/` 是 React SPA（iter-56 引入，路由 `#/station`、`#/tasks`、`#/dag`、`#/users`），
  只消费 `/health`、`/api/station/auth-token`、`/api/station/users*`、`/api/tasks*`。
  **SPA 完全不消费 task-flow 相关端点**。
- 「Dashboard 运行时 Tab」（瀑布查询/总览表/停滞横幅/记忆面板/错误面板）位于
  `lan_mesh/web/templates/dashboard.html` 单文件 UI 中，任务流消费逻辑集中在
  `loadTaskFlowList()` / `renderTaskFlowList()` / `queryTaskFlow()` / `renderTaskFlow()`。

### 1.2 端点版本事实修正

用户提问中将 `/api/runtime/task-flow` 描述为「iter-68 新增」。经核对设计文档与
`loop_status.json`，该端点实际于 **iter-38**（2026-08-25，P3 任务流全链路追踪）引入；
iter-68 的交付内容是 F3.1 扩容批量派发修复（`_autoscale_check` 单轮连续派发），与
任务流端点无关。本文审查以 iter-38 起迭代累计的端点契约为准，该事实修正不影响结论。

## 2. `/api/runtime/task-flow`（瀑布端点）字段对照

后端 `runtime_task_flow()`（`station_routes_basic.py:798`）→
`task_flow_waterfall()`（`runtime_trace.py:239`），返回：

| 后端字段 | 类型 | 前端消费点（`renderTaskFlow`） | 消费情况 |
|---|---|---|---|
| `task_id` | str | 未使用（查询入参已知） | 未消费（正常） |
| `events[].stage` | str | `stageColor[e.stage]` 阶段着色 | ✅ 消费 |
| `events[].label` | str | 阶段徽章 `esc(e.label)` | ✅ 消费 |
| `events[].detail` | str | 详情列 + `title` 悬浮 | ✅ 消费 |
| `events[].pm_id` | str | **无任何消费点** | ⚠️ 未消费（后端白送） |
| `events[].ts` | float | `_rtFmtTs(e.ts)` 时间列 | ✅ 消费 |
| `events[].gap_ms` | float | `+${_rtFmtMs(e.gap_ms)}` 阶段间隔 | ✅ 消费 |
| `total_ms` | float | 头部「总耗时」 | ✅ 消费 |
| `stage_count` | int | 头部「共 N 个阶段」 | ✅ 消费 |

核对结果：

1. **无悬挂引用**：前端引用的 `stage/label/detail/ts/gap_ms/total_ms/stage_count`
   全部存在于后端响应。
2. **阶段映射对齐**：前端 `stageColor` 字典覆盖的 11 个阶段键与后端
   `TASK_STAGE_LABELS` 的全部 11 个键一一对应
   （`submitted` / `pm:planning` / `pm:executing` / `pm:monitoring` /
   `pm:awaiting_input` / `pm:completed` / `pm:failed` / `pm:cancelled` /
   `pm:paused` / `subtask_result` / `delivered`），未映射阶段有
   `var(--accent)` 兜底着色。
3. **参数与边界一致**：前端不传 `limit`（后端默认 200、夹取 1~500）；
   前端本地拦截空 task_id（「请输入 task_id」），后端 400 校验
   （必填且 ≤64 字符）双保险；未知任务后端返回 200 + 空 `events`，
   前端渲染「该任务无追踪记录」空态，行为一致。
4. **首事件 `gap_ms=0`**：后端首事件 gap 为 0，前端用真值判断
   `e.gap_ms ? ... : ''` 正确隐藏了「+0ms」标签。

## 3. `/api/runtime/task-flow-list`（总览端点）字段对照

后端 `runtime_task_flow_list()`（`station_routes_basic.py:811`）→
`task_flow_overview()`（`runtime_trace.py:280`），返回
`{tasks: [...], stall_minutes: 30.0}`：

| 后端字段 | 前端消费点（`renderTaskFlowList`） | 消费情况 |
|---|---|---|
| `task_id` | 复制/瀑布跳转 | ✅ |
| `stage_count` | 阶段数列 | ✅ |
| `last_stage` | `last_label \|\| last_stage` 兜底 | ✅ |
| `last_label` | 最新阶段徽章 | ✅ |
| `total_ms` | 总耗时列 | ✅ |
| `last_ts` | 末活动列 | ✅ |
| `done` | 已收尾/进行中状态 | ✅ |
| `idle_ms` | 停滞 title 悬浮 | ✅ |
| `stalled` | 状态三态 + 红色横幅 | ✅ |
| `first_ts` | **无任何消费点** | ⚠️ 未消费（后端白送） |
| 顶层 `stall_minutes` | 横幅「超过 N 分钟无阶段事件」 | ✅ |

核对结果：无悬挂引用；`first_ts` 与瀑布端点的 `pm_id` 同属「后端提供、前端
未消费」的可选字段，非缺陷。终态判断前端完全信任后端 `done` 字段
（`TASK_FLOW_TERMINAL_STAGES`），未在前端重复实现。

## 4. 运行时 Tab 其余端点快速核对

Dashboard 运行时 Tab 还消费以下端点，均逐一字段核对通过：

| 端点 | 后端实现 | 核对结果 |
|---|---|---|
| `/api/runtime/metrics` | `db.query_llm_metrics` | ✅ `window_hours/total_calls/avg_latency_ms/p99_latency_ms/avg_ttft_ms/total_input_tokens/total_output_tokens/by_model{calls,tokens,avg_ms}/by_status/recent_errors{model,error,count}` 全部对应 |
| `/api/runtime/calls` | `db.query_llm_recent` | ✅ `created_at/call_type/model/input_tokens/output_tokens/ttft_ms/total_ms/status/task_id/error` 全部对应 |
| `/api/runtime/trace` | `read_trace_lines` | ✅ `ts/type/model/skill/status/total_ms/elapsed_ms/input_tokens/output_tokens/error/task_id` 全部对应（`subtask_start` 被前端过滤） |
| `/api/task-memory/overview` | `station_routes_tasks.py:589` | ✅ `total/success_rate/recommended_mode/common_errors/by_type{task_type,count,success_rate,avg_duration,recommended_mode}/recent{...}` 全部对应 |
| `/api/errors/stats` | `error_tracker.get_stats` | ✅ `total_errors/recent_window_count/alert_threshold/by_module{total}` 对应 |
| `/api/errors/recent·history·diagnosis·heal*` | 同名实现 | ✅ 字段全部对应（`diagnose_records` 的 `findings{action,category,count,modules,suggestion,sample}` 与前端渲染一致） |

另：`/api/runtime/stats`（JSONL 统计）后端存在但 Dashboard 未消费，属
「后端多、前端少」方向，不构成契约不一致。

## 5. webui/ React SPA 契约核对

| SPA 消费端点 | 后端位置 | 核对结果 |
|---|---|---|
| `/health` | `station_routes_basic.py:46` | ✅ `status/uptime_secs/components/resources{memory_mb,cpu_percent,disk_percent,threads}/workload{active_tasks,active_pms,ws_clients}` 全部对应 |
| `/api/station/auth-token` | `station_routes_basic.py:242` | ✅ `auth_enabled/mesh_token/role` 对应（后端多返回 `name/users`，SPA 未消费，正常） |
| `/api/station/users`（GET/POST） | `station_routes_basic.py:344/356` | ✅ `users{name,role,token_tail4}/admin_view`；`token_tail4` 仅 `admin_view=True` 时存在，SPA 仅在 `adminView` 为真时渲染该列，防御正确 |
| `/api/station/users/{name}/role`（PUT） | 同上 `:372` | ✅ 请求体 `{role}` 对应 |
| `/api/station/users/{name}/rotate-token`（POST） | 同上 `:387` | ✅ 响应 `token` 对应 |
| `/api/station/users/{name}`（DELETE） | 同上 `:397` | ✅ |
| `/api/tasks?status=` | `station_routes_tasks.py:287` | ✅ 响应 `{tasks,total}`，`status` 过滤参数存在 |
| `/api/tasks/{id}` | `station_routes_tasks.py:293` | ✅ 响应含 `name/status`（`to_dict()`），SPA 消费 `name/status` |
| `/api/tasks/{id}/graph`（GET/PUT） | `station_routes_tasks.py:417/429` | ✅ GET 返回 `{nodes,edges}`（含 `x/y/condition`），PUT 接受 `{nodes,edges}` 返回 `{ok,message}`，与 SPA `TaskGraph` 类型一致 |

## 6. 发现清单汇总

| # | 级别 | 发现 |
|---|---|---|
| 1 | 信息 | 瀑布响应 `events[].pm_id` 前端未消费 —— 可选优化：瀑布行尾展示关联 PM ID（点击可跳 PM 详情），后端无需改动 |
| 2 | 信息 | 总览响应 `first_ts` 前端未消费 —— 可选优化：状态列 title 显示「起于 <时间>」 |
| 3 | 事实修正 | task-flow 端点引入于 iter-38 而非 iter-68（见 1.2），文档与 loop_status 记录一致 |
| 4 | 澄清 | webui/ SPA 与 Dashboard 单文件 UI 是两条前端线；运行时 Tab 全在 dashboard.html |
| 5 | 无 | 未发现前端引用不存在的字段；未发现后端字段命名/类型漂移；无需后端修改 |

## 7. 建议（均非必需，供后续迭代参考）

1. `pm_id` 未消费是 waterfall 字段利用率唯一缺口，若后续需要「按 PM 追溯任务流」，
   前端加一列即可，后端契约已就绪。
2. 若担心双前端漂移，可在 SPA `api.ts` 补 `TaskFlowWaterfall`/`TaskFlowOverview`
   类型定义（与后端 docstring 对齐），但 SPA 当前无运行时 Tab，优先级低。
3. 后续 UI 改动按 ui-change-checklist 规范在 `test_bug/test_checklist.csv` 登记。
