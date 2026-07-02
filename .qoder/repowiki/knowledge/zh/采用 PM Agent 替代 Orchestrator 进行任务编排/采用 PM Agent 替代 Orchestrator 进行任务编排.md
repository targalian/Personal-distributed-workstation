---
kind: design
name: 采用 PM Agent 替代 Orchestrator 进行任务编排
source: session
category: adr
---

# 采用 PM Agent 替代 Orchestrator 进行任务编排

_来源：db0c6cd → dd036cd 提交周期内记录的编码计划——内容为规划时意图，实现可能滞后或有出入。_

**状态：** accepted

## 背景
原有的 Boss → Secretary → Orchestrator → DAG分解 → Worker 架构中，Orchestrator 采用静态 DAG 分解和直接 HTTP 分发，缺乏动态决策能力和对复杂协作模式（如并行、流水线、嵌套团队）的支持。为了提升任务处理的智能化水平和灵活性，需要引入具备 LLM 决策能力的中间层。

## 决策驱动
- 动态任务分解与架构决策能力
- 支持复杂的团队协作模式（single/parallel/pipeline/nested）
- 实时进度监控与反馈
- 去中心化的编排逻辑

## 备选方案
- **保留 Orchestrator 并增强其逻辑** _（已否决）_ — 优点：改动较小，复用现有 DAG 分解机制；缺点：难以实现基于 LLM 的动态架构决策；静态 DAG 无法适应运行时变化；缺乏细粒度的团队管理和进度上报机制
- **引入 ProjectManagerAgent (PM Agent)** — 优点：利用 LLM + multi-agent-architect skill 动态分析任务复杂度并决定团队结构；支持多种协作模式；通过 Secretary API 动态选择 Work Station；提供细粒度的进度收集和 WebSocket 实时推送；缺点：架构复杂度增加；需要新增数据库表（pm_agents, agent_teams, progress_reports）和多个 API 端点；PM Agent 需运行在 Worker 进程中，占用资源

## 决策
废除 Orchestrator，引入 ProjectManagerAgent。新流程为：Boss 通过 Secretary 聊天窗口提交任务 → Secretary 选择 Work Station 并启动 PM Agent → PM Agent 加载 multi-agent-architect skill 进行 LLM 分析 → 动态创建子 Agent/团队 → 分发任务并收集进度 → 通过 WebSocket 向 Web Dashboard 推送实时更新。PM Agent 运行在 Worker 进程内，复用 AgentRuntime 进行 LLM 调用。

## 影响
1. 任务编排从静态 DAG 转变为动态 LLM 决策，提升了处理复杂任务的灵活性。
2. 新增了 pm_agents, agent_teams, progress_reports 三张数据库表及大量相关 CRUD 方法。
3. Secretary API 重构，POST /api/tasks 不再调用 orchestrator.submit_task，而是启动 PM Agent。
4. Web Dashboard 新增“秘书对话”和“团队管理” Tab，支持树形嵌套展示团队结构和实时进度。
5. Worker 进程需承载 PM Agent 实例，增加了单个 Worker 的资源负载和管理复杂度。