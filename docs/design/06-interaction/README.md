# 06 交互通道

用户与系统的对话入口：Web 秘书聊天、手机 Bot 网关、统一角色人格。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| bot_gateway.py | Bot 网关 — 手机消息通道 |
| chat_handler.py | 秘书聊天处理器 — Web 端对话接口 |
| role_cards.py | 统一角色卡定义 (M6) — Secretary/PM/Worker 人格的单一事实源。 |
<!-- /AUTO:module-list -->
---

## chat_handler.py — 秘书聊天处理器（57KB）

**职责**: Boss 通过 Web 聊天窗口与秘书对话的主入口。

**处理流程**:
1. 接收消息 → 构建工作站状态摘要作为 system prompt 上下文
2. 调用 LLM 生成回复
3. 解析回复中的操作意图并执行
4. 返回回复 + 操作结果

**设计要点**:
- 聊天历史持久化 SQLite（chat_history 表），重启不丢失
- 支持多对话架构（L1 项目对话 / L2 PM 线程），分层混合交互的载体
- 历史曾出现「前端双渲染重复回复」缺陷（已修复，前后端各渲染一次的问题）

**自然语言 DAG 编辑意图** (iter-51, F4.3): `_ACTION_KEYWORDS` 新增
graph 编辑关键词组（编辑图/修改图/加一步/删除步骤/跳过步骤/加依赖等,
置映射首位保证优先级）→ `_action_edit_task_graph()` 执行 —
定位任务 (task-xxx 正则优先 + 名称匹配) → 读图 → `_parse_graph_edit()`
LLM 解析结构化编辑指令 (add_node/remove_node/add_edge/remove_edge) →
TaskDAG 应用 (自带环检测回滚) → `update_task_graph` 落盘；
防幻觉: 真实执行并返回落盘结果, 解析失败/未找到任务均明确报错不虚报。

**优化讨论纯对话通道** (iter-73): `chat()` 新增 `discuss_context` 参数
(契约 `{topic: 优化项 id | __all__}`, 由 `POST /api/secretary/chat` 透传) —
非空即进入讨论模式: (1) `_build_discussion_context()` 把话题对应优化项详情
(标题/来源/优先级/状态/说明/Boss 补充) 或队列总览 (守护状态/队列数/待决策数 +
活跃项前 10 条) 追加进 system prompt; (2) `_apply_chat_action()` 跳过
`_detect_action` 关键词检测, `action_taken` 固定为 `opt_discuss`。
**动因**: `_ACTION_KEYWORDS` 是子串匹配, 讨论文本里的「状态」「帮我写」
「遇到瓶颈」会误触发查询/建任务/优化守护等副作用; 讨论区需要的是纯解释而非执行。
优化器缺失或取数异常时降级为占位提示文案, 不打断对话。

## bot_gateway.py — Bot 网关（手机通道）

**架构**:
```
StationController
  └── BotGateway
        ├── 企业微信群机器人 Webhook (单向推送)
        └── Telegram Bot API (双向: sendMessage 推 + getUpdates 收)
```

**职责**: 工作站事件推送手机、接收手机端命令、Secretary 与手机用户的
异步交互通道。

**优化项**: 消息聚合防刷屏（短时间窗口事件合并）、Telegram Inline
Keyboard（PM 决策交互）。

**事件模板** (iter-44): 新增 `error_burst` (错误突发, high 优先级) —
窗口内错误数超阈值且冷却到期时推送, 与错误追踪闭环联动。
**事件模板** (iter-52): 新增 `cost_budget_warning` (预算适配告警,
normal 优先级, 💰 图标) — 任务提交时预估超预算 (tight/insufficient)
推送任务名/预估 tokens/状态/建议文案, 与预算顾问联动。
**错误追踪埋点** (iter-45, F1.4 数据源): bot_gateway 两处接入 `error_tracker.capture`
— 推送重试耗尽进离线队列前 (module=bot, 携带通道/事件类型)、秘书对话链异常兜底前；
异常隔离不影响原降级行为。

## role_cards.py — 统一角色卡（M6）

**背景**: 角色人设此前散布于 chat_handler（秘书）、pm_planner / agent_prompt
（PM）、agent_prompt（Worker）多处，人格调整需改多份。

**设计**: 三角色（Secretary/PM/Worker）的 identity / mission / sections
集中定义于此，各模块仅引用；**人格调整只改一处**。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-27 | iter-44 | 新增 error_burst 事件模板与优先级 (错误突发告警, 与错误追踪闭环联动) |
| 2026-08-27 | iter-45 | bot_gateway 两处错误追踪埋点 (推送重试耗尽/秘书对话链异常, 异常隔离) |
| 2026-08-28 | iter-51 | chat_handler 自然语言 DAG 编辑意图 (F4.3): 图编辑关键词组 + _action_edit_task_graph (任务定位/LLM 指令解析/TaskDAG 应用/落盘, 防幻觉真实执行) |
| 2026-08-28 | iter-52 | bot_gateway 新增 cost_budget_warning 事件模板 (预算适配告警, normal 优先级, 任务提交预估超预算时推送) |
| 2026-09-01 | iter-73 | chat_handler 优化讨论纯对话通道: chat() 增 discuss_context (话题优化项/队列总览注入 system prompt + 跳过命令关键词检测, action_taken=opt_discuss), /api/secretary/chat 透传 |
| 2026-08-16 | iter-27 后 | 初建 |
