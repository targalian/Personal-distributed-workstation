# 09 Web 前端

单文件深色仪表盘，多 Tab 布局，与 station_api 的 REST/WebSocket 对接。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| lan_mesh/web/static/ | CSS/JS 静态资源 |
| lan_mesh/web/templates/dashboard.html | Station Web 控制台 (10 Tab, 含运行时性能) |
<!-- /AUTO:module-list -->
---

## dashboard.html — 仪表盘单文件

**结构**: 单文件内联 CSS/JS（无构建步骤，便于模板渲染与分发）。

**10 Tab**: Station 总览 / 技能库 / 秘书对话（L1 项目对话 + L2 PM 线程）/
项目工作台 / Work Station 主机 / MCP工具 / 模型资源（配置向导 + 余额 + 消费）/
手机通道 / 主机通讯 / 运行时性能（其中秘书相关 Tab 激活后才显示）。

**运行时性能 Tab** (iter-37, 📈):
- 数据源: `/api/runtime/metrics` (SQLite 聚合) + `/api/runtime/calls` (调用明细) + `/api/runtime/trace` (JSONL 轨迹)
- 指标卡: 调用总数/平均延迟(含P99)/平均TTFT/Token 总量, 时间窗可选 (1h/6h/24h/3d/7d)
- 表格: 按模型统计 (调用数降序) + 调用明细 50 条 + 子任务轨迹 30 条 (过滤 subtask_start 无终态记录)
- 渲染函数: `refreshRuntime()` / `renderRuntimeMetrics()` / `renderRuntimeCalls()` / `renderRuntimeTrace()`

**任务流总览 + 瀑布** (iter-38~41, P3):
- 数据源: `/api/runtime/task-flow-list` (最近任务阶段聚合总览) + `/api/runtime/task-flow?task_id=` (单任务瀑布)
- 总览表 `loadTaskFlowList()` / `renderTaskFlowList()`: 任务(点击复制)/最新阶段徽章/状态/阶段数/总耗时/末活动/「瀑布」按钮 `jumpTaskFlow()` 自动填充并查询；状态列三态 ✅已收尾/🔄进行中/⚠️可能停滞 (iter-40 停滞检测: 未到终态且超 30 分钟无事件, 红色带空闲时长提示) + 顶部红色停滞告警横幅；iter-41 实时告警: `onStationEvent` 收 `task_stall_alert` 事件 → toast 提示停滞任务数+ID (Lv3 红色) + 自动调 `loadTaskFlowList()` 刷新总览表 (无需手动切 Tab)
- 瀑布: `queryTaskFlow()` / `renderTaskFlow()` — 阶段徽章 + 详情 + 间隔(gap_ms) + 总耗时；空输入与未知任务均渲染占位提示；调用明细表任务列点击 `copyTaskId()` 复制

**任务记忆面板** (iter-42, F4.1 可视化):
- 数据源: `/api/task-memory/overview?limit=10` (全局统计 + 按类型分组 + 最近沉淀)
- `loadTaskMemory()` / `renderTaskMemory()`: 统计卡片 (记忆总数/总体成功率/推荐协作模式/常见错误预警) + 按类型分组表 (成功率绿/橙分级, 耗时自动换算 m/s) + 最近沉淀列表 (关键词截断, 失败记录 hover 显示 error_pattern)；空记忆渲染引导提示；秘书未激活 503 时降级显示「加载失败: 503 (秘书未激活)」不报错

**错误追踪面板** (iter-44, F1.4 可视化):
- 数据源: `/api/errors/stats` + `/api/errors/recent?limit=15` 并行拉取 (复用既有 F1.4 端点)
- `loadErrors()` / `renderErrors()`: 统计卡片 (错误总数 绿/橙分级/告警窗口计数 vs 阈值 超限红/按模块分布) + 最近错误表 (倒序, 消息截断 hover 全文)；空记录渲染引导提示
- 实时联动: `onStationEvent` 收 `error_captured` → 面板刷新；收 `error_burst` → 红色 toast 告警 + 刷新；`refreshRuntime()` 挂载 `loadErrors()`

**关键渲染函数**（改动时注意同步更新本文档）:
- `renderHosts()`: 主机卡片 + 统计行（含版本分布 `vMap`/`vTxt`，
  多版本告警色；S3 新增 ✅最新/⚠️落后/未知版本 标记）
- `showHostDetail()`: 主机详情弹窗（kv-grid，含代码版本行）
- WebSocket `/ws`: event_bus 事件实时推送入口

**近期增量**:
- S2: 版本分布统计行
- S3: 卡片版本标记 + 详情版本时间
- UI 变更须在 `test_bug/test_checklist.csv` 登记（UI-0xx 编号），
  并经 Browser 实测（截图存 temp_resault/）

**前端已知坑**: 秘书回复双渲染问题（历史修复）、删除任务后需主动刷新列表。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-27 后 | 初建；收录 S2/S3 版本统计 UI |
| 2026-08-25 | iter-37 | 新增 📈 运行时性能 Tab (指标卡/按模型统计/调用明细/子任务轨迹)，UI-035 实测通过 |
| 2026-08-25 | iter-38 | 运行时 Tab 增任务流瀑布查询 (task_id → 生命周期阶段时间线)，UI-036 实测通过 |
| 2026-08-26 | iter-39 | 运行时 Tab 增任务流总览表 (最近任务阶段聚合 + 一键查瀑布)，UI-037 实测通过 |
| 2026-08-26 | iter-40 | 总览表增停滞检测 (状态列三态 + 红色告警横幅)，UI-038 实测通过 |
| 2026-08-26 | iter-41 | 停滞告警实时推送 (WS task_stall_alert → toast + 总览表自动刷新)，UI-039 实测通过 |
| 2026-08-26 | iter-42 | 运行时 Tab 增任务记忆面板 (统计卡片/按类型分组/最近沉淀，503 优雅降级)，UI-040 实测通过 |
| 2026-08-27 | iter-44 | 运行时 Tab 增错误追踪面板 (统计卡片/按模块分布/最近错误表，error_captured/error_burst 事件实时联动)，UI-041 实测通过 |
