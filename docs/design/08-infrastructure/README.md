# 08 基础设施

横切关注点：配置、日志、事件总线、错误追踪、评级、启动自检、旧版路由。

## 模块清单

| 模块 | 职责一句话 |
|---|---|
| config.py | Pydantic 强类型配置 (config.yaml + 环境变量) |
| logger.py | 结构化日志 (控制台+文件轮转) |
| event_bus.py | 进程内事件总线 (M5, 发布订阅 → /ws 广播) |
| error_tracker.py | 本地错误聚合追踪 (F1.4, 环形缓冲+落盘) |
| host_rating.py | 主机评级 (S/A/B/C/D 五级) |
| preflight.py | 启动前自检 (11 项检查) |
| api.py | 旧版路由层 (Worker API + 早期 Secretary API, 部分被 station_api 取代) |

---

## config.py — 配置管理

Pydantic 强类型校验，读取 config.yaml 与环境变量，提供全局配置访问。
关键段: 端口、security.auth_enabled、发现参数、同步间隔。

## logger.py — 结构化日志

统一格式 `[时间] [级别] [模块] 消息`，控制台 + 文件双输出，
文件自动轮转（5MB × 3 备份）。环境变量 `LAN_MESH_LOG_LEVEL` /
`LAN_MESH_LOG_FILE` 可调。接口: `get_logger(name)`。

## event_bus.py — 事件总线（M5）

进程内发布/订阅：后台线程（资源记账/R7 预警等）发布事件，station_api
启动时装配 sink（经 asyncio 事件循环线程安全广播到 /ws 客户端），
替代纯 HTTP 轮询的实时感知。

**事件结构**: `{"type": <event_type>, "data": {...}, "ts": unix秒}`
**首版事件**: usage_reported / resource_alert / resource_config / host_event
**线程安全**: publish 可从任意线程调用；sink 投递由 asyncio loop 兜底。

## error_tracker.py — 错误聚合追踪（F1.4）

捕获聚合异常（按模块/类型分组）、统计 API（频率/最近时间/影响模块）、
错误率告警。实现: 内存环形缓冲 + 定期落盘 SQLite。支持装饰器
`@error_tracker.track("station")`。

## host_rating.py — 主机评级

CPU/内存/磁盘综合得分（0~100）→ S/A/B/C/D 五级 + 可读摘要。
用于 Station Director 资源池筛选与项目规划按难度匹配主机。

## preflight.py — 启动自检

11 项前置检查（Python 版本/依赖/配置/目录/端口/网络/DB/Web UI 模板/
CLI Agent 后端），打印检查报告，失败即终止启动（CLI Agent 检测非致命）。

## api.py — 旧版路由层（部分遗留）

Worker API（/info、/shared 上传下载）仍在使用；早期 Secretary API
（register/heartbeat/hosts）能力已被 station_api.py 扩展取代。
新端点一律加在 station_api.py。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-27 后 | 初建 |
