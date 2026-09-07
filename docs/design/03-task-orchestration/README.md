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

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-09-07 | iter-89 | 项目蓝图驱动规划: PMPlanner 拉取蓝图并注入 LLM 规划 prompt (非目标禁令 + 当前阶段), 按 project_id 缓存, `attach_blueprint_context` 随 input_data 下发至执行链路; 专项 8 passed |
| 2026-09-07 | iter-88 | 项目蓝图工作台: DB v12 持久化 charter/roadmap/decisions + ProjectManager 蓝图更新 + GET/PUT /api/projects/{id}/blueprint + dashboard 结构化编辑; 专项 3 passed |
| 2026-09-06 | iter-83 | PM 子任务可观测性修复: 本地回退登记临时 subagent 并在执行前同步 running, 远程团队创建后统一同步, 子任务开始/结果写任务流与任务面板; 专项 35 passed |
| 2026-09-05 | iter-81 | 真项目联测修复: 需求派发携带 project_path/repo_url/planning_only, 高优先级映射修正, PM 模板裁剪越界执行子任务, 本机链路本地地址规范化为 127.0.0.1 |
| 2026-08-27 | iter-45 | pm_agent 三处错误追踪埋点 (任务级失败/交付链/记忆沉淀链, 异常隔离) |
| 2026-08-28 | iter-51 | F4.3 自然语言 DAG 编辑: PUT /api/tasks/{id}/graph 编辑端点恢复 (重接 DB 路径, 仅 pending 可编辑 + 环检测) + GET 端点复用 get_task_graph_data + 秘书自然语言编辑意图 |
| 2026-08-16 | iter-30 补 | orchestrator 收敛裁定: 降级工具库 + stub 兼容, 3 个死端点下线, graph 端点改 DB 重建 |
| 2026-08-16 | iter-27 后 | 初建 |
