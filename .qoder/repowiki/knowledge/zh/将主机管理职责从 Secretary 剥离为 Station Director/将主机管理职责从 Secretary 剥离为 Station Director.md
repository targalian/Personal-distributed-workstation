---
kind: design
name: 将主机管理职责从 Secretary 剥离为 Station Director
source: session
category: adr
---

# 将主机管理职责从 Secretary 剥离为 Station Director

_来源：c07750f → e83d993 提交周期内记录的编码计划——内容为规划时意图，实现可能滞后或有出入。_

**状态：** accepted

## 背景
原有的 Secretary 模块承担了发现、注册、心跳、数据库、项目管理、编排、MCP 和 UI 等过多职责，导致逻辑耦合严重。为了清晰分离“管机器”（基础设施层）与“管项目”（业务逻辑层）的关注点，需要重构架构。

## 决策驱动
- 关注点分离 (Separation of Concerns)
- 降低模块耦合度
- 提升代码可维护性

## 备选方案
- **保持 Secretary 单体结构** _（已否决）_ — 优点：无需重构现有代码，短期开发成本低；缺点：随着功能增加，Secretary 将继续膨胀，机器管理与项目逻辑混杂，难以独立演进
- **拆分为独立进程/微服务** _（已否决）_ — 优点：物理隔离，独立部署和扩展；缺点：引入分布式系统复杂性（网络通信、服务发现、部署运维），对于当前规模过于沉重
- **在 Secretary 进程内逻辑拆分出 Station Director 组件** — 优点：逻辑清晰解耦，保留进程内调用的简单性和低延迟，无需处理分布式一致性；缺点：仍共享同一进程资源，故障隔离性不如独立进程

## 决策
在 Secretary 同一进程内引入 `StationDirector` 类作为内部组件，逻辑上独立于 `Secretary`。`StationDirector` 接管主机发现、注册、心跳、数据库主机表管理及评级逻辑；`Secretary` 专注于项目编排与协调。两者通过内部方法调用交互，不拆分进程。

## 影响
代码结构更清晰，`lan_mesh/station_director.py` 成为主机管理的唯一入口。`SecretaryController` 需初始化并委托请求给 `StationDirector`。未来若需物理拆分，由于接口已逻辑隔离，迁移成本较低。