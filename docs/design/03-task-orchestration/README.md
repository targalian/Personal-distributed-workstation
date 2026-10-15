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

**配套下线**: station_api 的 `PUT /api/tasks/{id}/graph`、
`POST /api/tasks/{id}/resume`、`GET /api/tasks/{id}/graph-state` 三端点
（原本永远 503）已删除; `GET /api/tasks/{id}/graph` 改为纯 DB 重建
（checkpoint 优先）; 前端图编辑器保存按钮改为停用提示。

## pm_agent.py / pm_planner.py / pm_dispatcher.py / pm_monitor.py — PM 四件套

**PM Agent 是当前唯一的任务驱动者**（orchestrator 已于 iter-30 收敛废弃）。

- **pm_agent.py**: 门面/协调器，统一持有三子模块并对外暴露接口
- **pm_planner.py**: 加载 multi-agent-architect skill → 模板匹配（F2.4）
  或 LLM 规划 → 多轮细化（F2.3）；简单任务直接执行
- **pm_dispatcher.py**: 获取可用 work_station 列表 → 创建团队与子 Agent →
  **依赖感知调度**（depends_on 满足才分发）→ 构建子 Agent 定制 prompt →
  本地执行回退
- **pm_monitor.py**: progress_loop 轮询 + 主动上报接收 → 超时检测 →
  **失败接管三级策略**（同站重试 → 换站重试 → PM 本地接管）→
  质量验证（F2.5 生成-验证器）→ 完整成功结果聚合；最终失败停止调度并上报原因

**pm_state.py**: planner/dispatcher/monitor 共享的 dataclass 状态容器，
由 PM Agent 统一持有，`state.lock` 保证线程安全。

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
- 超支自动暂停项目并切换经济模型
- 消费记录基于 model_resources 的用量日志折算

**依赖**: model_resources, model_router, database

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-10-15 | iter-44 | 校验计划依赖；本地与远端失败恢复有界；最终失败阻塞未完成任务；未知超时不自动再执行；拒绝部分结果聚合 |
| 2026-10-15 | iter-42 | 多任务派发后保留监控；本地回退按依赖执行并注册成员；停止事件阻止晚到结果和后续派发，唤醒澄清与监控 |
| 2026-10-15 | iter-41 | 聚合错误/空结果禁止成功交付；PM 仅在收到匹配任务的交付回执后完成；子任务进度不再冒充父任务完成 |
| 2026-08-16 | iter-30 补 | orchestrator 收敛裁定: 降级工具库 + stub 兼容, 3 个死端点下线, graph 端点改 DB 重建 |
| 2026-08-16 | iter-27 后 | 初建 |

## 交付结果可信性（iter-41）

PM 的单任务与聚合路径只有在 `/api/pm/{id}/deliver` 返回 HTTP 200、`ok: true` 且 `task_id` 匹配后才上报 completed。异常、非 200、无效 JSON、错误任务 ID 均不算交付成功；本地文件保存尝试提前执行，成功任务记忆只在确认后记录。网络超时可能发生在服务端落库之后，因此失败提示要求核实记录，不代表服务端一定没有产物。没有新增自动重派或端到端恢复协议。

聚合响应的 `_error`、`error`、`model: none`、既有模型错误前缀及空内容视为失败，保留子任务输出，不把错误文字作为正式交付。整体进度在聚合完成前仅为 in_progress；本地子任务进度改用 member 身份，不能触发父任务 completed/failed 同步。

iter-41 仅验证结果边界；iter-42 补监控生命周期与协作式停止，iter-43 补齐交付重试去重及验收事务保护。API 与 SQLite 故障测试不替代真实模型和多机验收。

## PM 监控与协作式停止（iter-42）

多任务 `_run_task` 在派发完成后保留 running，由 monitor 在聚合与交付终态后停止。monitoring_ready 事件明确标识初始派发已完成，避免线程在团队尚未构建完时提前聚合；统计以计划子任务为单位，成功输出在质量检查后发布，完成后移除子任务超时计时。本地执行和远程创建失败后的回退同样登记成员，经 pending 依赖队列注入前置输出，不再按计划数组顺序直接执行。

PMState 的 stop_event/stop_reason 标识取消、暂停、停止或全局超时。监控使用可唤醒事件等待；澄清等待与停止在同一锁下操作，避免取消信号被 clear 丢失。规划、单任务执行、子任务执行、质量检查及聚合返回后检查事件，停止后不继续调度、交付或用晚到状态覆盖停止结果。每个 PM 实例只接管一次任务，避免旧线程污染新任务。

此为协作式停止：已进入外部模型、HTTP、CLI、Shell 或远端工具的操作不会被本轮强制终止；产生的外部副作用也不会回滚。暂停没有新增断点续跑入口。跨主机取消确认、在途派发与副作用隔离仍须实机验收，不能将线程测试称作完整取消能力。

## 有界失败处理（iter-44）

多任务计划在创建团队前校验非空、名称唯一、依赖存在及无环。本地运行异常、失败返回和既有模型错误标记进入同一恢复入口；质量验证拒绝的结果在重试耗尽后也不能作为成功输出。

默认恢复预算为原执行者重试两次。纯本地执行共最多三次；远端执行耗尽后仅尝试一个不同站点，再由 PM 本地接管一次。换站创建失败直接进入本地接管，不再空等；本地接管成功不发送失败升级。预算选择在状态锁内保留，已确认成功的子任务忽略重复完成和晚到失败。

最终失败时记录 terminal_failures，清空待派发队列并停止整个计划；其余未完成子任务均标为 blocked（包含独立分支，不只后代），原因通过子任务同步持久化。只有所有计划输出成功且依赖队列为空才聚合；直接调用聚合也拒绝缺少必需输出的情况。子任务和全局超时属于执行结果未知，停止调度并提示核实，不自动发起第二份执行。

此策略不提供远端执行次数标识、旧回调隔离或工具幂等保证。已发出的远端工具不会被强制中止，blocked 表示调度停止，不证明外部执行已经结束。普通 HTTP 异常的本地回退仍需后续收敛；实机网络中断与恢复仍待验收。
