---
kind: design
name: 采用 PM Agent 架构替代 Orchestrator 进行任务编排
source: session
category: adr
---

# 采用 PM Agent 架构替代 Orchestrator 进行任务编排

_来源：809d77b → bcdf817 提交周期内记录的编码计划——内容为规划时意图，实现可能滞后或有出入。_

**状态：** accepted

## 背景
原有的 Boss → Secretary → Orchestrator → DAG分解 → Worker 链路中，Orchestrator 负责集中式 DAG 分解和分发，缺乏动态团队组建能力和细粒度的进度监控。为了支持更复杂的协作模式（如并行、流水线、嵌套团队）和实时进度反馈，需要引入具备自主决策能力的 Project Manager Agent。

## 决策驱动
- 动态团队组建能力
- 细粒度进度监控
- LLM 驱动的架构决策
- 去中心化的任务分发

## 备选方案
- **保留 Orchestrator 并增强其功能** _（已否决）_ — 优点：改动较小，复用现有 DAG 逻辑；缺点：难以实现动态团队结构（如 nested/parallel），缺乏基于 LLM 的复杂任务拆解能力，集中式瓶颈明显
- **引入 PM Agent 运行在 Worker 节点** — 优点：利用 LLM skill (multi-agent-architect) 动态决定团队结构，支持多种协作模式，PM 直接管理子 Agent 生命周期和进度，Secretary 仅作为聊天入口和状态聚合器；缺点：架构复杂度增加，需新增 PM/Team/Progress 数据模型和 API，Worker 需承载 PM 运行时

## 决策
废除 Orchestrator 模块，引入 ProjectManagerAgent 运行在选定的 Work Station 上。新流程为：Boss 通过 Secretary 聊天提交任务 -> Secretary 选择 Work Station 启动 PM Agent -> PM Agent 加载 multi-agent-architect skill 分析任务并决定团队结构 -> PM 直接调用其他 Worker 创建子 Agent 并分发任务 -> PM 收集进度并通过 API 上报给 Secretary。

## 影响
1. 需在 lan_mesh/protocol.py 和 database.py 中新增 PMAgent, AgentTeam, TeamMember, ProgressReport 数据模型及对应表。2. lan_mesh/station_controller.py 中 activate_secretary 不再创建 Orchestrator，改为创建 ChatHandler。3. Worker 需扩展 API (/role/start-pm, /pm/create-subagent) 以支持 PM 生命周期管理和子 Agent 创建。4. Web Dashboard 需新增“秘书对话”和“团队管理” Tab 以展示新的层级结构和进度。