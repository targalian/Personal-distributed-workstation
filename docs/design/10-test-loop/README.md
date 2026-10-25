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
| tests/frontend/ | Node认证并发与Tauri IPC契约回归 |
| tests/test_application_lifecycle.py | Cooperative lifecycle and draining regressions with controlled blocking windows. |
| tests/test_core.py | 核心模块单元测试 |
| tests/test_execution_services.py | Worker concurrency, responsive execution API and native execution boundaries. |
| tests/test_frontend_assets.py | Dashboard source/markup compatibility and release version resolution. |
| tests/test_infrastructure_adapters.py | Compatibility and event-loop isolation for remaining infrastructure HTTP adapters. |
| tests/test_interaction_services.py | Interaction contracts, common ingress and real project domain persistence. |
| tests/test_network_service_integration.py | Real loopback HTTP integration of Worker API, native execution and shutdown. |
| tests/test_pm_services.py | Concurrency and integration regressions for shared PM ownership and scheduling. |
| tests/test_repositories.py | Database compatibility: unchanged schema and shared repositories with isolated connections. |
| tests/test_resource_services.py | Resource contracts, secret-free metadata and scoped execution accounting. |
| tests/test_services.py | Behavioral regression for real Yomk routing, isolation and task API compatibility. |
| tests/test_shadow_dev.py | Shadow development guardrail tests. |
| tests/test_workstation_optimizer.py | Tests for the resident workstation optimization workflow. |
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

## iter-104：服务化回归

tests/test_services.py 新增 23 项行为回归，使用真实 YomkServerPy 和隔离 SQLite。
tests/fixtures/task_api_contract.json 是迁移前保存的任务域完整 OpenAPI 快照，
用于验证端点路径、参数、默认值、描述和响应契约均无漂移。
专项还覆盖总线路由/内省、线程与事件循环边界、错误传播、关闭重建、应用隔离、
角色/发现组件热替换、任务派发多路径、交付闭环、预算拒绝、记忆聚合与网络等待期间并发查询。

结果：全量 577 passed（123.35s）；核心编译、导入、86 个 Python 文件未绑定名称扫描
通过。Windows 沙箱的本地 socketpair 初始化会卡住 TestClient，基线同样受影响，
因此经授权在沙箱外运行测试。该结论不等价于真实多主机压测或线上升级验收。

## iter-105：PM 并发与统一派发回归

tests/test_pm_services.py 新增 16 项行为用例，构造阻塞窗口验证 12 路并发只有一个
PM 构造进入；还覆盖恢复竞争、同 task_id 重试幂等、取消后线程退出屏障、回调重入、
失败清理、身份隔离、统一选站与并发 claim。Worker 新可选 pm_id body 和 X-PM-ID
输入 header 同时验证旧调用兼容。tests/test_services.py 复用统一传输测试接缝。

全量 593 passed（122.98s）；最终服务专项 39 passed（7.12s）。编译、控制器导入、
89 个 Python 文件未绑定名称扫描通过；设计清单 120 条目、11 域通过。
测试为隔离 DB/受控线程与模拟网络，不等于真实跨机或在线 LLM 负载测试。

## iter-111：Yomk迁移本机验收

隔离环境621 passed/125.29s；Node6 passed；真实loopback HTTP、浏览器、两套前端与wheel通过。详见[完整评估](../yomk-refactor-assessment.md)。实机联调尚待用户提供环境。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-10-25 | iter-111 | 隔离环境621 passed/125.29s；Node6 passed；真实loopback HTTP、浏览器、两套前端与wheel通过 |
| 2026-10-25 | iter-105 | 新增16项PM专项；全量593 passed，服务专项39 passed，原任务OpenAPI契约保持 |
| 2026-10-25 | iter-104 | 新增 23 项服务化行为回归及 OpenAPI 快照；全量 577 passed |
| 2026-08-29 | iter-65 | test_checklist.csv 数据恢复: iter-64 上库误截断为单行 GBK (丢 54 行历史) → 从 git 历史恢复全量并统一 utf-8-sig (工具链标准编码); UI-055 登记检测通过 |
| 2026-08-16 | iter-30 补 | P1 #9: 新增 PM 三件套接口级单测 33 条 (TestPMPlanner/TestPMMonitor/TestPMDispatcher), 基线 121→154 |
| 2026-08-16 | iter-30 补 | E5: 新增 TestSecretaryFailover 6 条, 基线 115→121 |
| 2026-08-16 | iter-30 | F1: 新增 TestRoleFreeAlign 10 条, 基线 105→115; 自动升级与 post-merge 回归验证闭环 |
| 2026-08-16 | iter-29 | 新增 .githooks/post-merge: git pull 升级后自动 pytest 回归验证 |
| 2026-08-16 | iter-28 | E4: 新增 TestSecretaryConflict 7 条, 基线 98→105 |
| 2026-08-16 | iter-27 后 | 初建 |
