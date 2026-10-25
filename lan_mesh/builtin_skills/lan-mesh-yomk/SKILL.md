---
name: lan-mesh-yomk
description: 维护 LAN Mesh 工作站的 YomkServerPy 服务、兼容门面和退出流程；用于工作站代码开发与架构审查。
metadata:
  category: architecture
  tags: [lan_mesh, lan-mesh, 工作站, service_runtime, componentservice]
  default_access: [station, secretary, pm, worker]
  version: "1.0"
  activation:
    any: [lan_mesh, lan mesh, lan-mesh, 工作站, stationcontroller, service_runtime, componentservice]
---

# LAN Mesh 的 Yomk 开发约定

工作站已直接集成 YomkServerPy，先读目标目录 AGENTS.md，再读目标门面及领域服务。
本技能只约束工作站工程；外部项目按其自身技术栈和用户需求处理。

- 使用 `lan_mesh.service_runtime.ServiceRuntime` 和 `ComponentService`；每个 Station、Worker 或组件 owner
  拥有自己的容器。不要用 `YomkApi.server()` 全局单例替换实例级容器。
- 原有公开门面保留参数和返回约定，使用 `call_component(owner, XxxService, operation, **arguments)`
  进入单一业务实现；新领域服务显式声明 `OPERATIONS`，状态和锁归 owner 管理。
- 同步调用用 `runtime.request_sync('/Service/op', **arguments)`；异步适配层用
  `await runtime.request('/Service/op', **arguments)`，由封装隔离阻塞工作。总线地址参数为位置参数，业务数据可含 `url`。
- 纯计算、策略及 SQLite repository 保持普通函数/类，不把每条 SQL 包装成总线请求。
  数据库使用 `_get_conn()` 的线程本地连接，不改主键；已有 HTTP/WS/IPC 接口保持兼容。
- 控制请求要有异常隔离和超时；大文件/模型流的现有专用超时不能盲目缩短。
- 新后台任务必须明确停止事件、取消、线程/协程等待与退出状态；停止后不能复活容器。
  原生容器等在途及嵌套请求完成后再销毁；未退出的线程应如实报告。
- 实例内省只展示操作元数据，不展示密钥或任务载荷。版本统一读取 `VERSION.json` / `lan_mesh.version`。

开发时复用相邻领域的服务、兼容门面和测试模式，编译/导入后验证受影响行为及接口契约，
后端改动按 AGENTS.md 跑全量测试和 `python scripts/sync_docs.py`，必要时同步设计文档及状态文件。
上库遵循 `scripts/sync_push.ps1`。技能不自动触发提交、推送、生产部署或调用在线模型。

源码定位：`lan_mesh/component_service.py`、`lan_mesh/service_runtime.py`、`lan_mesh/application_lifecycle.py`。
整体设计见 `docs/design/yomk-refactor-assessment.md`，实机放行见 `docs/design/08-infrastructure/yomk-rollout.md`。
