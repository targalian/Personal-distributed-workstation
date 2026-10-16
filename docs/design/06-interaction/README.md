# 06 交互通道

用户与系统的对话入口：Web 秘书聊天、手机 Bot 网关、统一角色人格。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| bot_gateway.py | Bot 网关 — 手机消息通道 |
| brief_generation.py | Durable background brief generation with idempotent starts and atomic proposals. |
| brief_handoff.py | Persist confirmed project descriptions before dispatch and prevent duplicate handoffs. |
| brief_review.py | Build reviewable AI changes and merge an explicit selection without breaking graphs. |
| chat_handler.py | 秘书聊天处理器 — Web 端对话接口 |
| project_brief.py | Conversation-scoped project specification; Markdown and DAG share one source. |
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

## role_cards.py — 统一角色卡（M6）

**背景**: 角色人设此前散布于 chat_handler（秘书）、pm_planner / agent_prompt
（PM）、agent_prompt（Worker）多处，人格调整需改多份。

**设计**: 三角色（Secretary/PM/Worker）的 identity / mission / sections
集中定义于此，各模块仅引用；**人格调整只改一处**。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-10-15 | iter-43 | 聊天验收与退回改用统一事务；状态与网页一致，退回不再假报重新执行 |
| 2026-10-14 | iter-40 | 确认版本与任务关联；先持久化下发快照、同版本去重、会话及版本保护、任务和验收追溯 |
| 2026-10-14 | iter-39 | 保存/采纳保留独立历史快照，原子发布索引，分页查询、版本比较与恢复为新版本 |
| 2026-10-14 | iter-38 | AI 回复与整理生成持久化建议，差异审阅后逐项采纳；原稿、版本冲突和 PM 下发保护 |
| 2026-10-10 | 项目描述工作区 | 新增对话产物、三层需求 DAG、版本保存、Markdown 导出与集成验证 |
| 2026-08-16 | iter-27 后 | 初建 |

## 项目描述工作区

Boss 与秘书对话新增文档／需求 DAG 双视图，支持三层框图、版本保存与 `项目描述.md` 导出。详情、格式、接口和后续优化见 [项目描述工作区](项目描述工作区.md)。

聊天「验收」「退回」通过 `review_task_delivery` 修改最近未评审交付，状态分别为 accepted/rejected，与网页一致。退回只记录原因，不把任务标成 running，也不宣称 PM 已自动重新执行；修改需求后应明确确认新版本并下发新任务。

## iter-49：聊天停止反馈

聊天取消/暂停共用持久化控制链路。已登记而未确认时使用等待提示与原节点诊断；确认后才显示成功反馈。已交付或已验收任务拒绝取消，保留审阅依据。通知异常不会撤销已保存的停止确认。

## 澄清继续点与回复确认（iter-55）

任务详情与 PM 线程发送固定问题编号，失败保留输入；已落盘确认后才记录“已发送”消息。线程可读取当前问题，有旧草稿时不切换编号。Bot 选项绑定问题编号与选项索引，旧无编号按钮拒绝；取消操作必须走任务控制，不把“终止”文本当作已取消。Bot 测试全部模拟网络，没有发送真实消息。

### 项目描述后台整理（iter-61）

「从对话整理」改走持久化后台任务：先保存并固定文档/对话输入，登记请求编号后调用模型，同编号请求重放只返回已有记录。任务终态和 AI 建议同一原子写入，不直接修改已保存文档。人工保存或另一份建议使结果过期时，保留候选供 Markdown 导出；进程退出后只标记待核实，不自动产生新调用。

每会话一个活动任务、进程最多两个，数据目录的进程生命周期锁拒绝重复实例；同进程文档锁共享，普通保存/审阅/历史恢复保留去重记录。旧同步 API 保持兼容。接口、使用和边界详见 [后台整理说明](../../brief-generation.md)。
