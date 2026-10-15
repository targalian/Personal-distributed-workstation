# 03 任务编排

项目定位的核心竞争力层：任务以 DAG 组织，PM Agent 驱动拆解、组队、分发、
监控、聚合。架构范式为 **Graph Engineering**（借鉴 LangGraph Supervisor）。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| orchestrator.py | 任务编排引擎 — 已废弃, 降级为工具库 (iter-30 收敛裁定) |
| pm_agent.py | PM Agent 协调器/门面 — 持有 Planner/Dispatcher/Monitor, 对外暴露统一接口。 |
| pm_control.py | Apply identity-bound PM stop requests and persist the distinction between intent and acknowledgment. |
| pm_dispatcher.py | PM 分发器 — 团队创建与子任务分发 |
| pm_inspection.py | Read original-node PM evidence without dispatching work or changing task state. |
| pm_monitor.py | PM 进度监控器 — 进度收集、超时检测、失败接管、质量验证 |
| pm_planner.py | PM 规划器 — 任务分析与分解 |
| pm_start.py | Start a preassigned PM once, after verifying its persisted task ownership. |
| pm_state.py | PM Agent 共享状态容器 |
| project.py | 项目管理与预算控制 — Phase 3 项目隔离核心 |
| task.py | 任务 DAG 管理 — 子任务依赖图与拓扑排序 (增强版: 条件边 + 动态路由 + 图序列化) |
| task_submission.py | Shared task submission: persist PM ownership before any local or remote execution. |
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
| 2026-10-15 | iter-46 | 统一聊天/网页/队列派发，先事务登记任务与 PM 再启动；PM 启动持久化去重；掉线保留归属，不盲目迁移 |
| 2026-10-15 | iter-45 | 按执行编号核对远端回执，HTTP 异常不本地重做；PM 轮询原站点结果；并发终态只消费一次，旧回调不能推动新尝试 |
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

iter-45 补充执行编号、旧回调隔离和远端回执查询，并取消普通 HTTP 异常的本地回退。已发出的远端工具仍不会被强制中止，blocked 表示调度停止，不证明外部执行已经结束；实机网络中断与恢复仍待验收。

## 执行回执与结果归属（iter-45）

每次本地或远端执行分配新的 subtask_id，PMState 保存当前尝试的 PM、父任务、执行者、站点和阶段（active → processing → done）。接收终态时在锁内保留消费权，随后才做质量检查或恢复；重复终态、缺少身份字段、旧执行编号、错误 PM/父任务/执行者的报告不修改任务。已成功子任务不会重新调度。

远端派发只发送一次 POST，连接断开、超时、非 200 或无效响应均不触发另一台机器执行。监控线程使用带节点认证的 GET `/tasks/executions/{subtask_id}` 查询原站点；只接受所有身份字段匹配的回执。已确认失败仍按有界策略创建新尝试；远端重启后的 unknown 回执停止整个计划并提示核实，缺失或不可达回执等待现有超时保护，不将 404 解释为“未执行”。

Station 不再将结果投递给可能无关的本机 PM，执行端先持久化结果，再由拥有该尝试的 PM 查询。旧同步 Worker 的原请求结果可兼容；旧异步 Station 不提供身份回执，立即以结果未知终止并提示升级和核实；需升级后使用，不能保证混合版本运行中的任务完成。升级应在无在途任务时进行，已有在途结果需核实。

回执去重范围是同一节点同一执行编号；不同编号的失败重试仍可能重复工具的部分副作用，不能称为整个任务恰好执行一次。PM 端尝试表仍在内存，PM 自身重启后尚无检查点续跑；取消仍不能强制终止远端外部操作。


## PM 启动与任务归属（iter-46）

`task_submission.py` 统一聊天、项目描述、网页与自动队列的派发。create_task_once 防止相同 task_id 并发覆盖；bind_task_pm 在同一 SQLite 事务中将 pending 任务绑定到预分配 PM 并登记 starting 记录。只有抢占成功者能发起启动，映射和对话线程在执行之前建立；返回时重新读取任务，避免快速失败、完成或验收被旧 running 快照覆盖。无主机记录但本机 Runtime 可用时仍可本机执行。

`pm_start.py` 在 Station/Worker 共用启动门控：接收预分配 pm_id，核对 Secretary 当前任务仍由该 PM 拥有且为 running，再启动 Runtime。独立启动回执库持久化请求指纹与首次结果；同请求重复不创建第二个 PM，不同内容拒绝；进程变化后的旧记录标为结果未知，禁止自动重启。单进程锁保护本机 PM 选择/赋值，所有启动均在 HTTP 事件循环外执行，避免远端回查 Secretary 时阻塞任务 API。

启动 POST 不重试，丢失或错误响应仅查询原节点 `/role/pm-status`，匹配 PM 与 task_id 才确认。未知状态保留任务 running、PM unknown、任务输出诊断，不当作可安全重派；后续有效 PM 状态上报清除诊断。明确未执行的拒绝标记 failed。未绑定的任务不接受交付，移除过去为了兼容早上报而放宽 PM 归属的例外。

掉线不证明执行结束：孤立 PM 保留任务归属、标记 unknown，不清空 PM、不重新派发。已完成、失败、暂停、取消任务不受掉线诊断覆盖。没有增加 PM 重启续跑、远程取消确认或工具副作用回滚；启动之后但进度尚未上报时的崩溃仍需核实。自动队列只接管未绑定的 pending 任务。

启动回执库：Station 主库旁 `<主库名>-pm-starts.sqlite3`，Worker 为 `~/.lan_mesh/worker-pm-starts.sqlite3`。备份恢复应保留这些记录，不可通过删库强行重试。升级须在无在途任务时进行，新启动协议要求执行节点支持预分配 PM 和任务归属校验。

## iter-48：原节点执行核实

`pm_inspection.py` 读取持久化任务/PM 归属并查询原节点。`inspect_pm_start` 以 SQLite mode=ro 读取启动回执，结合当前 PM 的身份及 running 标志区分运行、停止、历史记录、启动拒绝和无记录；缺失数据库不创建。旧成功启动回执不能证明进程仍运行或任务完成。

Secretary 只发一次带认证的 GET（5 秒超时，禁止重定向），校验节点/PM/任务/格式；任务在等待期间删除、换归属或改变状态时返回 changed。查询不依赖内存 `_pm_worker_map`，不写任务、回执或验收，不触发重派。不实现检查点恢复或强制停止。操作说明见 ../../task-recovery.md。

## iter-49：PM 停止意图与确认

`pm_control.py` 将用户停止意图与原节点确认分离：Secretary 事务写入 `_pm_control` 并设置 paused/cancelled 以拦截派发和晚到结果，再按持久化归属发送一次含节点/PM/任务/动作/请求编号的 POST。失败只读核实，不自动重发；用户重发沿用请求编号。旧节点不回退无身份接口。

接收端与启动共用 `_START_LOCK`，在 PM 状态锁内核对身份并发出协作式停止信号，确认范围为 pm_cooperative；无同步回调，避免阻塞 Secretary 事件循环。晚到暂停不能覆盖取消。节点回执仅存当前 PM 实例，Secretary 确认持久化；节点重启后无法核实的回执保留未知。没有工具级强杀或暂停后续跑。

## iter-50：重启观察与完成回报

Secretary重新激活只表示观察中断，不能证明本机或远端PM已经停止。原节点、PM和任务编号保持不变，不重新创建PM、不重派任务。原PM完成回报兼容running/monitoring/planning/executing/awaiting_input活动任务，仍不覆盖暂停、取消或验收终态。

这不是检查点续跑；如果原PM进程已退出，仍需核实原节点与产物。HTTP服务重启+独立恢复进程+原PM线程联验确认：原执行存活时无需重做即可继续交付。
