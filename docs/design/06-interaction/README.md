# 06 交互通道

用户与系统的对话入口：Web 秘书聊天、手机 Bot 网关、统一角色人格。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| bot_gateway.py | Bot 网关 — 手机消息通道 |
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
