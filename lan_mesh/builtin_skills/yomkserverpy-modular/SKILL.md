---
name: yomkserverpy-modular
description: 使用 YomkServerPy 编写 Python 服务、消息与生命周期；适用于明确采用 Yomk 的 Python 项目。
metadata:
  category: architecture
  tags: [yomk, yomkserverpy, python, 服务化]
  default_access: [station, secretary, pm, worker]
  version: "1.4.20-1"
  activation:
    any: [yomk]
    unless: ["c++", cpp, yomkserver-modular]
---

# YomkServerPy 模块化开发

核心思想是“一切皆服务，一切皆请求”：以业务职责定义服务，以明确消息和操作组织调用。
先确定目标工程：修改 LAN Mesh 时同时遵循 `lan-mesh-yomk`，使用工作站现有实例级封装；
创建独立 Yomk Python 工程时才采用上游的 YomkApi / Boot 模板。技能本身不授权部署、联网或修改其他工程。

- `YomkServer` 管理容器；`YomkService` 提供 `/服务名/操作名`；消息描述输入输出。
- `YomkContext` 保存容器内共享状态，可用 Checker 校验迁移、Monitor 监听变化。
- `YomkEventLoop` 同循环有序、不同循环可并发；业务状态仍须有明确所有权和同步策略。
- `YomkFunctionPool` 管理公共函数；`YomkLogger` 记录调用；`YomkServerInfo` 供内省。
- 明确同步、异步、超时、取消和销毁行为；进程内请求不提供跨主机投递或 exactly-once 保证。
- 新建独立工程时提供可运行入口、Boot、配置、消息、服务和最小验证；扩展现有工程时复用其装配方式。

查 API、返回状态、Context/EventLoop/FunctionPool/Logger 用法时读 [reference.md](reference.md)。
创建工程或服务时按需读 [examples.md](examples.md) 对应示例。
完整上游指南保存在 [upstream-skill.md](upstream-skill.md)，其中命名、版本和全局入口约定仅适用于上游独立工程。
