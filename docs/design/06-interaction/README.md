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

## 秘书动作护栏 (BUG-031, iter-77)

**事故**: Boss 多轮澄清股票自动交易系统需求后回「是, 以上信息
应该也已经足够了」, 秘书答「系统正在处理您的项目创建指令」,
但后台零动作 —— `projects` 表无记录, `chat_history.action_taken`
全库只有 `''` 与 `opt_discuss`, 从未出现 `create_project`。
Boss 在等一个从未开始的操作。

**三重根因** (叠加才造成静默失败):
1. `_detect_action` 是纯字面子串匹配, `create_project` 仅认
   「创建项目/新建项目/建立项目」。确认类追问必然漏判。
2. 角色卡 `role_cards.SECRETARY_CARD` 无条件要求「回复收到, 系统
   正在处理您的指令」—— 未区分动作是否真被识别, LLM 照做。
3. `chat()` 先调 LLM 再检测意图, LLM 无从得知后台会不会执行。

**修复** (三层, 均经反向验证):
| 层 | 手法 |
|---|---|
| 时序 | `_detect_action` 提到 LLM 调用之前, 结果经 `_build_action_guard` 注入 system prompt |
| 提示词 | 命中则允许确认; 未命中则明令禁止「正在处理」并要求给出带触发词的指令样例 |
| 兔底 | `_append_no_action_notice` — prompt 约束不是硬保证, LLM 仍谎称时追加⚠️系统提示 |
| 意图继承 | `_detect_action_with_context` — 仅「本轮是纯确认」且「上轮秘书给过引号样例」时继承 |

**为何继承取引号样例而非整段回复**: 本例可继承信号在 assistant
建议里 (`id=116` 含「创建项目: ...」样例), 不在 user 消息里。
但若直接扫整段 assistant 文本, 秘书解释能力边界时提到的
「创建项目」也会误触发, 故只认 `「」`/`『』` 包裹或 `>`
引用行内的片段 (`_extract_quoted`), 并限 30 字以内确认句。

**测试**: `TestIter77SecretaryActionGuard` 8 例 — 护栏注入/兔底追加/
普通回复不动/引号继承/散文不继承/长句非确认/命中时照旧执行/
讨论模式不受影响。反向验证: 护栏不注入、去兔底、去继承
三种改法均即刻 FAIL。

## 秘书需求收集状态机 (iter-78)

**目标**: Boss 提出项目/任务想法后, 秘书先通过多轮对话收集结构化需求,
生成项目 Brief 与可执行最终提示词, 确认后再提交给 PM Agent。显式
`提交任务:` 指令不被拦截, 仍走原即时提交路径。

**状态机**:
`INTAKE (收集) → SYNTHESIZE (总结) → GAP_FILL (补缺) → CONFIRM (确认) → DISPATCH (提交)`

- 命中 `_REQ_INTAKE_KEYWORDS` (如「我想开发」「帮我规划一个项目」) 时创建草稿
- `INTAKE` 阶段按 checklist 逐项追问; 关键项齐全或 3 轮后进入总结
- `GAP_FILL` 阶段针对缺失关键项追问; 补齐或 5 轮后进入确认
- `CONFIRM` 阶段展示 Brief 与最终提示词; 确认则派发, 修改则回到 `INTAKE`
- 「够了」「开始吧」「按最终提示词执行」等快速退出词直接派发
- 「取消需求收集」只清草稿, 不创建任务

**最终提示词**: `_build_final_prompt` 生成
`提交任务: <目标>` 开头的自包含指令, 后跟 `【项目背景】`
等结构化字段。Boss 可直接复制粘贴, 也可回复「按最终提示词执行」;
若粘贴修改后的最终提示词, `_apply_final_prompt` 会把修改回填草稿
再派发, 不会丢失 Boss 的改动。

**持久化**: 草稿写入 `conv_index` 的
`meta["requirement_draft"]`, 重启后由 `_load_req_drafts` 恢复;
派发成功后 `_clear_req_draft` 清除, 避免残留活跃草稿。

**派发链路**: `_dispatch_from_draft` 调
`submit_task_from_chat(..., input_data={{"requirement": brief}})`。
Brief 在任务创建前就写入 `input_data`, 避免「先派发再补写」的竞态;
PM `refine_requirements` 检测到 `requirement` 后跳过重复追问。

**测试**: `TestRequirementGathering` 7 例 — 收集关键词建草稿/
确认派发并携带 Brief/最终提示词修改回填/取消不派发/显式提交不被拦截/
快速退出仍带 Brief/PM 收到 Brief 不再追问。pytest 415 passed。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-09-02 | iter-78 | 秘书需求收集状态机: INTAKE/SYNTHESIZE/GAP_FILL/CONFIRM/DISPATCH + checklist 模板 + 草稿持久化 + 最终提示词生成与修改回填 + 取消路径; submit_task_from_chat 支持 input_data, Brief 在创建前入库; PM 收到 requirement 后跳过重复追问; 专项 7 例; pytest 415 passed |
| 2026-09-02 | iter-77 | BUG-031 秘书静默失败修复: 意图检测提到 LLM 调用前 + _build_action_guard 把「后台是否会执行」注入 prompt; _append_no_action_notice 兔底追加清晰提示; _detect_action_with_context 仅对纯确认句继承上轮引号样例意图; 角色卡那条无条件「回复正在处理」改为以意图已识别为前提; 专项 8 例 + 三处反向验证; pytest 408 passed |
| 2026-08-27 | iter-44 | 新增 error_burst 事件模板与优先级 (错误突发告警, 与错误追踪闭环联动) |
| 2026-08-27 | iter-45 | bot_gateway 两处错误追踪埋点 (推送重试耗尽/秘书对话链异常, 异常隔离) |
| 2026-08-28 | iter-51 | chat_handler 自然语言 DAG 编辑意图 (F4.3): 图编辑关键词组 + _action_edit_task_graph (任务定位/LLM 指令解析/TaskDAG 应用/落盘, 防幻觉真实执行) |
| 2026-08-28 | iter-52 | bot_gateway 新增 cost_budget_warning 事件模板 (预算适配告警, normal 优先级, 任务提交预估超预算时推送) |
| 2026-09-01 | iter-73 | chat_handler 优化讨论纯对话通道: chat() 增 discuss_context (话题优化项/队列总览注入 system prompt + 跳过命令关键词检测, action_taken=opt_discuss), /api/secretary/chat 透传 |
| 2026-08-16 | iter-27 后 | 初建 |
