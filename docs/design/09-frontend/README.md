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

**任务流瀑布** (iter-38, P3):
- 数据源: `/api/runtime/task-flow?task_id=` (JSONL task_flow 事件按任务聚合)
- 交互: 输入 task_id 查询全链路时间线；调用明细表任务列点击 `copyTaskId()` 复制
- 渲染: `queryTaskFlow()` / `renderTaskFlow()` — 阶段徽章 + 详情 + 间隔(gap_ms) + 总耗时；空输入与未知任务均渲染占位提示

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
