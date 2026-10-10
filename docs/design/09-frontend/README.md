# 09 Web 前端

单文件深色仪表盘，7 Tab 布局，与 station_api 的 REST/WebSocket 对接。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| lan_mesh/web/static/ | CSS/JS 静态资源 |
| lan_mesh/web/templates/dashboard.html | Station Web 控制台 (7 Tab) |
<!-- /AUTO:module-list -->
---

## dashboard.html — 仪表盘单文件

**结构**: 单文件内联 CSS/JS（无构建步骤，便于模板渲染与分发）。

**7 Tab**: 总览 / 主机 / 任务 / 聊天（L1 项目对话 + L2 PM 线程）/
资源（资源池配置向导 + 余额 + 消费）/ 技能 / 设置。

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
| 2026-10-10 | 项目描述工作区 | 新增对话产物、三层需求 DAG、版本保存、Markdown 导出与集成验证 |
| 2026-08-16 | iter-27 后 | 初建；收录 S2/S3 版本统计 UI |

## 项目描述编辑器

`project-brief.js` 与 `project-brief.css` 在秘书聊天旁提供产物工作区。`ProjectBriefUI.open()` 绑定当前对话，`flush()` 在聊天前保存手工编辑，`refresh()` 同步秘书输出并保护未保存草稿。需求 DAG 支持 SVG 拖动、三层嵌套、折叠、依赖校验、缩放平移和撤销／重做；与 PM 执行 DAG 分开存储。异步加载不覆盖加载期间的新输入；保存响应即使晚于对话切换，也会更新原对话草稿的版本与同步状态。
