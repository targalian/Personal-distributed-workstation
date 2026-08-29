# 10 测试与验证循环

四层验证体系：pytest 基线、专项验证脚本、UI 走查、Loop Engineering 每日循环。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| .githooks/ | commit-msg / pre-push / post-merge / post-commit 钩子 |
| AGENT_LOCKS.md | 多 Agent 占用登记与交接看板 (根目录) |
| loop_status.json | 迭代状态机 (根目录) |
| test_bug/api_tests.py | Loop Engineering — 自动化白盒 API 测试 |
| test_bug/daily_loop.bat | 每日循环定时任务形态 (Windows) |
| test_bug/dev_loop.py | Dev Loop Engineering — 开发+测试闭环编排器 |
| test_bug/discover_tests.py | 测试项自动推导工具 — 从源码中机械式提取所有可测试点 |
| test_bug/loop_config.yaml | 循环配置 |
| test_bug/nightly_loop.py | Loop Engineering — 夜间自动巡检 (定时任务用) |
| test_bug/run_loop.py | Loop Engineering — 每日验证循环编排器 |
| test_bug/setup_scheduler.bat | 定时任务安装脚本 |
| test_bug/test_checklist.csv | UI 变更清单 (UI-0xx 编号) |
| test_bug/ui_change_log.py | Loop Engineering — UI 改动待检登记工具 |
| test_bug/ui_tests.py | Loop Engineering — 前端 UI 静态白盒验证 |
| tests/test_core.py | 核心模块单元测试 |
<!-- /AUTO:module-list -->
---

## tests/test_core.py — pytest 基线

**规范**: 全部改动后必须全绿（当前 154/154）。覆盖核心链路:
协议/DB 迁移/评级/任务 DAG/路由评分/密钥加解密/启动同步幂等/Secretary
冲突仲裁（TestSecretaryConflict, E4）/角色无关对齐（TestRoleFreeAlign,
F1）/Secretary 离线接管（TestSecretaryFailover, E5）/PM 三件套接口级
（TestPMPlanner/TestPMMonitor/TestPMDispatcher, P1 #9）等。

**编写约定**:
- 中文参数场景通过临时 .py 脚本执行，防 GBK 乱码
- 幂等类测试用 monkeypatch 隔离落盘，绝不意外写真实 resources.yaml
- 新增功能 = 新增测试类（如 TestStartupSync），docstring 维护覆盖列表

## test_bug/ — Loop Engineering 每日验证循环

**架构**: run_loop 调度 → discover_tests 发现用例 → api_tests / ui_tests
执行 → 报告输出 logs/ 与 reports/（按日期 md）。

**UI 走查规范**:
- UI 变更先在 test_checklist.csv 登记（UI-0xx），实现后标记检测通过
- Browser 实测截图存 temp_resault/，Console 零错误为准

**配套文件**: loop_status.json（根目录，迭代状态机: iteration_count /
current_phase / notes / next_tasks）、.githooks/（commit-msg / pre-push / post-merge）。

## 版本更新后自动验证（post-merge hook + F1 自动对齐）

`.githooks/post-merge` 在 git pull 合并升级完成后自动触发 `pytest tests/ -q`
回归验证（约 2s），失败输出红色告警但不阻断合并（合并已完成，告警提醒
修复后重新验证）。

- **触发时机**: `git pull` / `git merge` 完成后（含 fast-forward）；
  `git pull --rebase` 走 post-rewrite 路径，不触发本 hook
- **跳过场景**: squash 合并（参数=1，工作区含未提交合并内容时跳过）
- **Python 环境**: 优先 `.venv/Scripts/python.exe`，fallback 系统 python
- **退出码**: 恒为 0，验证失败不阻断 pull，仅告警
- **注册方式**: `core.hooksPath -> .githooks`，新增 hook 文件即自动生效

F1 起版本对齐与主从无关: 落后节点收到领先通知/自检落后 → 自动
`git pull` + 依赖安装（工作区脏则跳过），代码更新后由 dev-reload
自动重启（未开 dev 模式则提示手动重启）。自动 pull 同样触发
post-merge 回归验证，形成「自动对齐 → 自动验证」闭环。

配合机制: pre-push 静态审核（上库前 9 项检查）+ 每日 Loop 循环兜底
（`test_bug/setup_scheduler.bat` 注册计划任务，需管理员权限）。

## 四层验证流程（每个迭代必走）

1. 专项 `_xN_check.py`（编号递增，通过后删除）
2. pytest 基线全绿
3. 涉服务改动 → Station 重启冒烟
4. UI 改动 → checklist 登记 + Browser 实测

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-29 | iter-65 | test_checklist.csv 数据恢复: iter-64 上库误截断为单行 GBK (丢 54 行历史) → 从 git 历史恢复全量并统一 utf-8-sig (工具链标准编码); UI-055 登记检测通过 |
| 2026-08-16 | iter-30 补 | P1 #9: 新增 PM 三件套接口级单测 33 条 (TestPMPlanner/TestPMMonitor/TestPMDispatcher), 基线 121→154 |
| 2026-08-16 | iter-30 补 | E5: 新增 TestSecretaryFailover 6 条, 基线 115→121 |
| 2026-08-16 | iter-30 | F1: 新增 TestRoleFreeAlign 10 条, 基线 105→115; 自动升级与 post-merge 回归验证闭环 |
| 2026-08-16 | iter-29 | 新增 .githooks/post-merge: git pull 升级后自动 pytest 回归验证 |
| 2026-08-16 | iter-28 | E4: 新增 TestSecretaryConflict 7 条, 基线 98→105 |
| 2026-08-16 | iter-27 后 | 初建 |
