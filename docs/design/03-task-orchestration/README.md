# 03 任务编排

项目定位的核心竞争力层：任务以 DAG 组织，PM Agent 驱动拆解、组队、分发、
监控、聚合。架构范式为 **Graph Engineering**（借鉴 LangGraph Supervisor）。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| orchestrator.py | 任务编排引擎 — 已废弃, 降级为工具库 (iter-30 收敛裁定) |
| pm_agent.py | PM Agent 协调器/门面 — 持有 Planner/Dispatcher/Monitor, 对外暴露统一接口。 |
| pm_dispatcher.py | PM 分发器 — 团队创建与子任务分发 |
| pm_monitor.py | PM 进度监控器 — 进度收集、超时检测、失败接管、质量验证 |
| pm_planner.py | PM 规划器 — 任务分析与分解 |
| pm_state.py | PM Agent 共享状态容器 |
| project.py | 项目管理与预算控制 — Phase 3 项目隔离核心 |
| task.py | 任务 DAG 管理 — 子任务依赖图与拓扑排序 (增强版: 条件边 + 动态路由 + 图序列化) |
| task_templates.py | F2.4: 任务模板库 — 预置常见任务 DAG |
<!-- /AUTO:module-list -->
---

## task.py — 任务 DAG 数据结构

**职责**: 子任务依赖图（Graph Engineering 基石）。

**能力**: 邻接表、拓扑排序、环检测、就绪子任务判定（依赖已满足）、
**条件边**（运行时上下文决定是否激活）、动态图操作（运行时增删节点/边）、
JSON 序列化（前端渲染 + checkpoint 恢复）。

## orchestrator.py — 已废弃, 降级为工具库 (iter-30 收敛裁定)

**历史职责**: 用户任务 → 分解 → DAG 构建 → Agent 匹配 → HTTP 分发
Worker → 结果聚合（显式状态机 + Checkpoint, 借鉴 LangGraph Supervisor）。

**收敛裁定**: 编排能力已由 PM 四件套全面接管, Orchestrator 兼容 stub
类已随 secretary.py 历史入口一并删除 (P3 清理), 仅保留:
- `_classify_task()`: 任务类型分类工具函数 (单测覆盖中)
- `GraphState` / `PHASE_TRANSITIONS`: 早期状态机数据定义 (考古资产)

**配套下线与恢复**: station_api 的 `POST /api/tasks/{id}/resume`、
`GET /api/tasks/{id}/graph-state` 两端点（原本永远 503）已删除;
`GET /api/tasks/{id}/graph` 改为纯 DB 重建（checkpoint 优先, 复用
StationController.get_task_graph_data）; `PUT /api/tasks/{id}/graph` 编辑端点
已随 iter-51 (F4.3) 恢复 — 重接 DB 路径 (update_task_graph: 仅 pending 可编辑 +
环检测拒绝 + 落盘子任务列表与 checkpoint dag_json), 前端图编辑器保存按钮同步恢复。

## pm_agent.py / pm_planner.py / pm_dispatcher.py / pm_monitor.py — PM 四件套

**PM Agent 是当前唯一的任务驱动者**（orchestrator 已于 iter-30 收敛废弃）。

- **pm_agent.py**: 门面/协调器，统一持有三子模块并对外暴露接口
- **pm_planner.py**: 加载 multi-agent-architect skill → 模板匹配（F2.4）
  或 LLM 规划 → 多轮细化（F2.3）；简单任务直接执行
- **项目蓝图驱动规划 (iter-89)**: 规划前按 `task.project_id` 拉取
  `GET /api/projects/{id}/blueprint`, 渲染使命/目标/非目标/验收标准/硬约束/
  当前阶段/近期决策为约束段落注入 LLM 规划 prompt（明令非目标不得进入
  `decomposition`）；结果按 project_id 缓存, 一次任务只查一次。
  `attach_blueprint_context()` 在规划后把同一提示写入
  `input_data._project_blueprint`, 使本地执行与远程分发共用同一份约束。
  Secretary 不可达 / 无 project_id / 蓝图为空时静默返回空串, 不影响规划主流程。
- **交付前蓝图验收自检 (iter-90)**: `deliver_result()` 上报前委浃
  `PMPlanner.review_against_blueprint()` 依据蓝图 `acceptance_criteria`
  逐条判断交付物, 结论 (`verdict`/`checks`/`unmet`)
  随 delivery 上报到 Secretary 并落到
  `output_data._delivery.acceptance_review`; 无验收标准、交付物为空、
  LLM 异常或结果无法解析时一律静默跳过, 不阻塞交付。
- **结构化项目上下文 (iter-81)**: `input_data` 支持
  `project_path` / `repo_url` / `execution_mode=planning_only`;
  模板规划时不再把目标项目折叠为 `.`, planning-only 会裁掉
  「代码实现 / 单元测试」并降级为 single 模式。
- **pm_dispatcher.py**: 获取可用 work_station 列表 → 创建团队与子 Agent →
  **依赖感知调度**（depends_on 满足才分发）→ 构建子 Agent 定制 prompt →
  本地执行回退
- **本机地址规范化 (iter-81)**: `get_available_stations()` 过滤非本机
  `169.254.*` 链路本地地址；本机命中时强制 `ip=127.0.0.1` 并排在首位,
  避免 PM 远程分发自己失败后再回退本地。
- **子任务可见性 (iter-83)**: 远程团队创建完成后立即 `sync_subtasks()`;
  本地回退执行也登记临时 subagent (`busy`) 并在执行前同步,
  `_build_subtask_status()` 因而非依赖完成后才会离开 `running`;
  子任务开始写 `subtask_started` 任务流事件, 结果注入后再次同步任务面板。
- **pm_monitor.py**: progress_loop 轮询 + 主动上报接收 → 超时检测 →
  **失败接管三级策略**（同站重试 → 换站重试 → PM 本地接管）→
  质量验证（F2.5 生成-验证器）→ 结果聚合 → 升级上报

**pm_state.py**: planner/dispatcher/monitor 共享的 dataclass 状态容器，
由 PM Agent 统一持有，`state.lock` 保证线程安全。

**错误追踪埋点** (iter-45, F1.4 数据源): pm_agent 三处关键异常路径接入
`error_tracker.capture` — 任务级失败 (`_run_task` 顶层 except, 携带
task_id/pm_id)、交付链异常 (`_deliver` 后, 交付丢失风险)、记忆沉淀链异常
(`_record_task_memory` 后, 经验丢失风险)；全部 try/except 隔离, 埋点异常不影响主流程。

**分层混合交互**:
- L1 项目对话: 与秘书交互（需求/决策/跨 PM 协调）
- L2 PM 线程: 项目对话内展开，与单个 PM 深度技术讨论
- PM `_request_clarification` 阻塞时，回复双写 L1 通知 + L2 线程

## task_templates.py — 任务模板库（F2.4）

预定义任务分解模板（DAG 结构），PM 规划时匹配加速；支持用户自定义注册与
`{{变量}}` 替换。接口: `list_templates()` / `match_template()` /
`apply_template()`。

## project.py — 项目管理与预算护栏（Phase 3）

**职责**: 项目 CRUD + 成本计算 + 预算护栏。

**设计要点**:
- 每个项目: 独立工作空间目录、独立预算配额、允许模型白名单、
  路由策略（cost_first / quality_first / balanced）
- 项目蓝图 (iter-88): `charter` (使命/目标/非目标/验收标准/约束)、
  `roadmap` (阶段路线图) 与 `decisions` (决策日志) 作为三段 JSON
  落 SQLite projects 表 (迁移 v12)，重启后保留；`update_project_blueprint`
  统一校验三类结构并复用项目更新时间戳
- 超支自动暂停项目并切换经济模型
- 消费记录基于 model_resources 的用量日志折算

**依赖**: model_resources, model_router, database

## 对话派发任务的项目绑定 (iter-93, BUG-033)

**症状**: 经秘书对话派发的任务, `project_id` 恒为空字符串。后果是蓝图驱动
规划 (iter-89) 与交付前验收自检 (iter-90) 对这类任务**双双静默失效** — 两者
都以 `task.project_id` 回查项目 charter, 查不到就降级成「无蓝图」路径, 既不
报错也无日志, 从外部完全看不出来。

**根因**: `StationSchedulerMixin.submit_task_from_chat()` 签名里没有
`project_id`, `Task(...)` 构造也不传该字段; `ChatHandler._action_submit_task()`
同样无从传递。HTTP 端点 `POST /api/tasks` 一直支持 `project_id`, 只有对话链路
漏了 — 属长期存在的隐性缺口。

**修复**:

| 层 | 改动 |
|---|---|
| `station_scheduler.py` | `submit_task_from_chat()` 新增 `project_id: str = ""` 参数, 落到 `Task.project_id`; 成本预估由硬编码 `project_id=""` 改为传入真实项目 (预算适配随之生效); 日志追加项目字段 |
| `chat_handler.py` | 新增 `_resolve_project_from_message()`: 按「完整 uuid → 8 位短码 → 活跃项目名最长匹配」三级解析; `_action_submit_task()` 解析后下传, 并在回复中回显项目归属让 Boss 当场看见绑定结果 |

**兼容性**: `project_id` 默认空串, 未提及项目的对话行为不变; 名称匹配要求
长度 ≥4 且取最长匹配, 避免「股票」这类短名误命中。

**存量数据**: 修复前派发的任务需一次性修正, 见
`scripts/fix_task_project_binding.py` (须先停 Station, 否则 SQLite 写锁独占)。

## 交付结论回流蓝图: 「蓝图↔项目」一一对应闭环 (iter-95)

### 协作模式

Boss ↔ 秘书讨论产出项目蓝图 (`project.charter / roadmap / decisions`)。
PM 依蓝图执行 (iter-89 蓝图驱动规划), 交付前按验收标准自检 (iter-90), 交付结论
`acceptance_review` 再由 `record_delivery_to_blueprint` 回流到蓝图对应路线图阶段,
Boss 据此提出下一轮修改 —— 蓝图与项目保持一一对应。

| 交付结论 | 蓝图阶段处理 |
|---|---|
| `verdict=pass` 且无 `unmet` | 阶段 `status` 推进为 `review` (待 Boss 确认) |
| 存在 `unmet` | 阶段状态**保持不变**, 记录 `deliveries[].unmet` 缺口清单供 Boss 决策 |

只更新当前阶段的 `deliveries` 字段, 不改 `phase/goal/branch` 等 Boss 手填内容;
同阶段只保留最近 5 条记录, 防蓝图无限膨胀。

### 实现

- `project.py` — `record_delivery_to_blueprint()` + 模块级 `_current_phase_index()`。
  后者选取口径 (优先 `in_progress/doing/active`, 其次首个非 `done/completed`) 与
  `pm_planner._current_roadmap_phase` 完全一致, 确保「PM 读到的阶段」与「回流写入的
  阶段」是同一个 —— 口径漂移会让结论写错阶段, 已列为变异用例。
- `station_routes_tasks.py` — `receive_pm_delivery` 交付入库后调用 `_reflow_blueprint`
  桥梁函数 (由 `task.project_id` 定位项目), 广播新增 `blueprint_updated` 字段。
  任何异常只记日志, 绝不阻断交付物入库。
- 无 `project_id` / 项目无 roadmap / 项目不存在 → 静默跳过返回 None。

### 验证

7 专项 + 7 变异全部致红 (含「通过也不推进」「未满足也推进」「不限条数」
「固定写第一个阶段」「helper 不触发回流」「恢复简洁压制」「恢复整类拒答」);
488 pytest passed。

## BUG-034 跨站子任务结果回传与超时计时器修复 (iter-94)

### 现象

M1 任务 (task-0cdc51ad40dd, 股票数据更新系统) 以「全局超时 (3600.0s)」失败。
需求分析子任务被反复执行 3 次, 代码实现子任务在 192.168.1.206 远程 Station 上启动后
零产出, 直到全局超时。

### 根因 1: 子任务完成不清理计时器 → 误判超时 → 重试但不重新注册

`_state.subtask_start_times[task_name]` 只在 `check_subtask_timeouts` 的 `del` 中移除,
`receive_progress_report` 收到 completed/failed 后不移除 → 1800s subtask_timeout 后
被误判超时, 触发 `handle_subagent_failure` → 重试 (同站或换站)。但重试调用的
`dispatch_subtask` 不重新注册开始时间 → 重试后的子任务无超时保护, 永久挂起。

**修复**: 在 `receive_progress_report` 中终态时清理 `subtask_start_times[task_name]`;
`_record_subtask_start` 移入 `dispatch_subtask` 方法体, 确保每一次调用 `dispatch_subtask`
(包括重试) 都重新注册开始时间。

### 根因 2: `_local_execute_task` 忽略 `pm_id` → 跨站结果永远回传错误 PM

`station_local_pm.py` 的 `_local_execute_task` 执行完毕后无条件调用
`self._local_pm_agent.receive_subtask_result(...)`, 完全不检查 payload 中的 `pm_id`。
当远程 PM 将子任务分发到本机执行时, 结果被注入本机 PM (或本机无 PM 时直接丢弃),
原始 PM 永远等不到回调, 只能耗到全局超时失败。

**修复**: 新增 `_report_subtask_result` 方法, 判断 `payload.pm_id` 是否匹配本机 PM。
不匹配 → 跨站 → HTTP POST 到 `payload.secretary_url + "/pm/progress-report"` 回传
原始 PM 所在 Station。匹配 → 原路本地注入。

### 影响范围

- `lan_mesh/pm_monitor.py` — 新增 `_clear_subtask_timer` / `_trace_retry` / `_log_self_check`
- `lan_mesh/pm_dispatcher.py` — `_record_subtask_start` 移入 `dispatch_subtask`; payload 新增 `secretary_url`
- `lan_mesh/station_local_pm.py` — 新增 `_report_subtask_result` 跨站路由
- `lan_mesh/runtime_trace.py` — 新增 `subtask_retry` 阶段标签
- `tests/test_core.py` — TestIter94 6 专项 + 4 变异

### 验证

- 481 pytest passed (基线 475)
- 4 变异全部致红 (M1 跨站路由 / M2 计时器清理 / M3 重试注册 / M4 端点正确)
- `sync_docs.py` / `check_unbound_names.py` / `git diff --check` 全部 PASS
- 函数长度: 0 个超 80 行

## BUG-037 子任务作业路径恒为 "." (iter-99)

### 现象

股票 M1 任务 `task-0cdc51ad40dd` 的三个子任务描述里项目路径是**字面量 `.`**:
「分析项目 **.** 的现有结构」「在 **.** 中实现功能代码」。子 Agent 于是在
Station 自身 cwd 里找 `stock_player` 代码 —— 三次「需求分析」全部报
`completed` 却毫无价值, 烧掉 448 万 input tokens 换回分析错目录的产物。

这类缺陷**不会报错**: 模板正常渲染、子任务正常完成、PM 正常聚合, 只有产物
内容是错的, 因此比崩溃类 bug 更隐蔽。

### 根因 1: 对话派发路径从不解析 `project_path`

`pm_planner.analyze_with_skill` 取 `input_data["project_path"]`, 缺省回退
`"."`。但带路径提取的 `_extract_project_context` **只在需求收集流里调用**;
「提交任务…」这类直接指令走 `_action_submit_task`, 该函数 iter-93 起只解析
`project_id`, 从不解析路径 → `input_data` 里恒无该键。

### 根因 2: 路径正则只认反斜杠

`_extract_project_context` 的两条正则都写 `[A-Za-z]:\\`, 而 Boss 蓝图与
消息里惯用**正斜杠**。实测蓝图原文「本地 E:/ingobj/stock_player」两条正则
**全部返回 None** —— 即便走了需求收集流也提取不到。

### 修复: 蓝图作为路径的权威来源

1. `charter.repo_path` 成为结构化字段 (兼容 `local_path`/`project_path`
   别名), 由 `_blueprint_repo_path` 读取 —— 蓝图是「项目」的单一事实来源,
   不依赖每次消息措辞。
2. 新增 `PMPlanner._resolve_project_path`, 优先级
   `input_data.project_path` → 蓝图 `repo_path` → `"."`;
   全程 `try/except`, 蓝图查询失败不影响规划主流程, 且兜底时打 WARNING
   提示「建议在蓝图填 repo_path」, 不再静默用错路径。
3. 路径正则收敛为模块级 `_extract_local_path`, 同时接受 `/` 与 `\\`、
   支持引号包裹, **带标签的写法优先于文中先出现的无关裸路径**
   (否则「参考 C:/tmp/sample, 项目路径为 E:/…」会取到前者)。
4. `_action_submit_task` 把解析到的路径经 `input_data` 传下去; 无路径时传
   `None` 保持旧行为, 不伪造值。
5. 蓝图提示新增「本地仓库路径 (子任务须在此作业)」行, 让 LLM 规划路径也知情。

### 验证

12 专项 + 15 变异全部致红。真机经生产 Secretary API 实测: 写入
`repo_path` 后子任务描述由「分析项目 . 的现有结构」变为「分析项目
**E:/ingobj/stock_player** 的现有结构」, 蓝图提示出现路径行。

> 教训: 首轮真机验证曾失败返回 `.` —— 单元测试桩掉了 `_fetch_project_blueprint`
> 因而全绿, 而真机走的是带鉴权的 HTTP 路径。凡「读远端配置」的改动, 单测桩
> 之外必须补一次真机链路验证。

## BUG-038 子 Agent 实际工作目录未注入 (iter-100)

### 现象

BUG-037 修复后 M1 重跑 (`task-af7e8cde65d4`): 子任务描述已正确显示
`E:/ingobj/stock_player`, 但「需求分析」仍连续 **3 次**被质量门禁判不合格,
原因均为「达到最大轮次 30 后终止」+「后期脱离目标项目」, 约 414 万 input
tokens。已取消任务止损。

### 根因: 有人读 `cwd`, 没人写 `cwd`

`agent_runtime._handle_react_agent` 里 `cwd = input_data.get("cwd",
self.shared_folder)` —— 而**全仓库没有任何代码往 `input_data` 写 `cwd`**。
于是 `shell_exec`/`dir_list` 的注入值恒为空的 `shared_folder`: Agent 从描述
文本**读得到**项目路径, 却始终**进不去**, 只能靠反复试探漫游, 耗尽 30 轮。

这是 BUG-037 的**孪生缺陷**: 前者修「告诉 Agent 去哪」(描述里的路径),
本处修「把 Agent 放到哪」(实际工作目录), 只修其一无效。

附带问题: `file_read`/`file_write` 不接受 `cwd` 参数, 相对路径按 Station
进程启动目录 (即本仓库) 解析 —— 既读不到目标项目文件, 也有越界写入风险。

### 修复

1. `attach_blueprint_context` 由「只注入蓝图文本」扩展为**同时注入 `cwd`**;
   注入点选在 `pm_agent` 规划后那一次调用, 覆盖本地执行 + 远程分发全部路径。
   蓝图文本为空但路径可解析时仍须注入 (对话派发任务常无 charter 正文)。
2. 新增 `PMPlanner._resolve_workdir`: 复用 `_resolve_project_path` 的三级
   口径, 但**只在目录真实存在时返回**; `"."` 或不存在的路径一律返回空串,
   让 AgentRuntime 保持 `shared_folder` 旧行为 —— 绝不把 Agent 塞进无效目录。
3. 上游已显式指定 `cwd` 的任务**不覆盖** (允许精确控制作业目录)。
4. `cwd = input_data.get("cwd") or self.shared_folder`: 空串也回落, 避免
   把 Agent 放进当前进程目录。
5. 规则段 prompt 抽为模块级 `_build_react_rules_prompt(cwd)`, 新增三条:
   相对路径以 cwd 为基准、禁止跨盘/上级目录漫游、内容不符即说明并结束而非
   靠试探消耗轮次。仅注入 `cwd` 不够 —— 模型看不到它仍会自由探索。
6. 新增模块级 `_resolve_tool_path(cwd, raw_path)`: 把 `file_read`/
   `file_write` 的相对路径锚定到 `cwd`; 绝对路径与 `~` 展开后为绝对的路径
   原样返回, `cwd` 为空时不改写。

> 护栏边界: `validate_cli_agent_cwd` / `CLI_AGENT_ALLOW_SELF_REPO` 只作用于
> `_handle_cli_agent` (防自举改写本仓库), 与 ReAct 循环的 `cwd` 是两条独立
> 路径, 本改动不放宽自举护栏。

### 验证

10 专项 + 17 变异全部致红 (含 prompt 三条规则各删一条、`isabs` 反转、
`expanduser` 缺失、显式 `cwd` 被覆盖、目录存在性校验去除)。

> 教训: 「不报错但产物错」的缺陷 (路径错 / 目录错) 光看 `status` 发现不了 ——
> BUG-037 与 BUG-038 都表现为子任务正常结束, 前者产物分析错目录, 后者耗尽
> 轮次。定位靠 iter-94 的 `subtask_retry` 追踪记录了重试原因。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-09-09 | iter-95 | 交付结论回流蓝图: `record_delivery_to_blueprint` 按验收结论推进/保持路线图阶段并记录缺口, 交付端点集成 `_reflow_blueprint`; 回流阶段选取口径与 PM 读取口径统一; 7 专项 + 7 变异致红 |
| 2026-09-09 | iter-94 | BUG-034 跨站子任务结果回传修复: `_local_execute_task` 忽略 `pm_id` 致跨站结果注入错误 PM (或丢弃), 新增 `_report_subtask_result` 按 `pm_id` 路由回传原始 Secretary; `subtask_start_times` 终态清理 + `_record_subtask_start` 移入 `dispatch_subtask` 使重试也有超时保护; 新增 `subtask_retry` 追踪阶段与进度上报去重; 专项 6 例 + 变异 4 处致红 |
| 2026-09-09 | iter-93 | BUG-033 对话派发任务无项目归属修复: `submit_task_from_chat` 新增 `project_id` 并落库, `_resolve_project_from_message` 三级解析 (uuid/短码/名称最长匹配), 成本预估改用真实项目; 蓝图驱动与验收自检对对话任务恢复生效; 专项 7 例 + 变异 2 处致红 |
| 2026-09-08 | iter-90 | 交付前蓝图验收自检: PM 交付前按验收标准逐条审查交付物, 结论随 delivery 落库与广播; 异常静默降级不阻塞交付; 专项 5 passed |
| 2026-09-07 | iter-89 | 项目蓝图驱动规划: PMPlanner 拉取蓝图并注入 LLM 规划 prompt (非目标禁令 + 当前阶段), 按 project_id 缓存, `attach_blueprint_context` 随 input_data 下发至执行链路; 专项 8 passed |
| 2026-09-07 | iter-88 | 项目蓝图工作台: DB v12 持久化 charter/roadmap/decisions + ProjectManager 蓝图更新 + GET/PUT /api/projects/{id}/blueprint + dashboard 结构化编辑; 专项 3 passed |
| 2026-09-06 | iter-83 | PM 子任务可观测性修复: 本地回退登记临时 subagent 并在执行前同步 running, 远程团队创建后统一同步, 子任务开始/结果写任务流与任务面板; 专项 35 passed |
| 2026-09-05 | iter-81 | 真项目联测修复: 需求派发携带 project_path/repo_url/planning_only, 高优先级映射修正, PM 模板裁剪越界执行子任务, 本机链路本地地址规范化为 127.0.0.1 |
| 2026-08-27 | iter-45 | pm_agent 三处错误追踪埋点 (任务级失败/交付链/记忆沉淀链, 异常隔离) |
| 2026-08-28 | iter-51 | F4.3 自然语言 DAG 编辑: PUT /api/tasks/{id}/graph 编辑端点恢复 (重接 DB 路径, 仅 pending 可编辑 + 环检测) + GET 端点复用 get_task_graph_data + 秘书自然语言编辑意图 |
| 2026-08-16 | iter-30 补 | orchestrator 收敛裁定: 降级工具库 + stub 兼容, 3 个死端点下线, graph 端点改 DB 重建 |
| 2026-08-16 | iter-27 后 | 初建 |
| 2026-09-10 | iter-99 | BUG-037 子任务作业路径恒为 "." 修复: 蓝图 `charter.repo_path` 成为路径权威来源 + `_resolve_project_path` 三级回退 (input_data→蓝图→".") + 路径正则兼容正斜杠与引号且标签优先于裸路径 + `_action_submit_task` 传递 `project_path` + 蓝图提示暴露仓库路径; M1 失败复盘发现 (三次需求分析烧 448 万 tokens 分析错目录); 12 专项 + 15 变异致红 + 真机 API 实测 |
| 2026-09-10 | iter-100 | BUG-038 子 Agent 实际工作目录未注入修复: `attach_blueprint_context` 同时注入 `cwd` + `_resolve_workdir` 仅在目录真实存在时返回 (`"."`/不存在回落 shared_folder) + 显式 `cwd` 不覆盖 + ReAct `cwd` 空串也回落 + 规则段抽为 `_build_react_rules_prompt` 并新增「相对路径基准/禁止跨盘漫游/禁止试探消耗轮次」+ `_resolve_tool_path` 把 file_read/file_write 相对路径锚定到 cwd; BUG-037 孪生缺陷 (M1 重跑三次需求分析耗尽 30 轮烧 414 万 tokens); 10 专项 + 17 变异致红 |
