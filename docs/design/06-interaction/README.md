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
| 2026-08-16 | iter-27 后 | 初建 |
