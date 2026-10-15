# 10 测试与验证循环

四层验证体系：pytest 基线、专项验证脚本、UI 走查、Loop Engineering 每日循环。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| .githooks/ | commit-msg / pre-push / post-merge 钩子 |
| loop_status.json | 迭代状态机 (根目录) |
| test_bug/api_tests.py | Loop Engineering — 自动化白盒 API 测试 |
| test_bug/daily_loop.bat | 每日循环定时任务形态 (Windows) |
| test_bug/dev_loop.py | Dev Loop Engineering — 开发+测试闭环编排器 |
| test_bug/discover_tests.py | 测试项自动推导工具 — 从源码中机械式提取所有可测试点 |
| test_bug/local_checks.py | Local development loop: syntax, unit tests, UI and docs checks without a Station. |
| test_bug/loop_config.yaml | 循环配置 |
| test_bug/nightly_loop.py | Loop Engineering — 夜间自动巡检 (定时任务用) |
| test_bug/project_brief_browser.py | Manual browser integration check with temporary storage and a simulated LLM. |
| test_bug/run_loop.py | Loop Engineering — 每日验证循环编排器 |
| test_bug/setup_scheduler.bat | 定时任务安装脚本 |
| test_bug/test_checklist.csv | UI 变更清单 (UI-0xx 编号) |
| test_bug/ui_change_log.py | Loop Engineering — UI 改动待检登记工具 |
| test_bug/ui_tests.py | Loop Engineering — 前端 UI 静态白盒验证 |
| tests/test_backup.py | Restore real task, brief and receipt stores; reject corrupt or unsafe backup archives. |
| tests/test_core.py | 核心模块单元测试 |
| tests/test_delivery_replay.py | Concurrent delivery retries must preserve the reviewed artifact and acceptance decision. |
| tests/test_execution_receipts.py | Verify durable execution claims, restart ambiguity and Station/Worker HTTP receipts. |
| tests/test_local_checks.py | Verify local-loop failure propagation, environment isolation and offline reports. |
| tests/test_pm_delivery.py | Delivery outcomes must reflect persisted acknowledgements, not child progress or model errors. |
| tests/test_pm_lifecycle.py | Exercise PM worker and monitor threads through completion and cooperative stop. |
| tests/test_pm_start.py | Verify task/PM registration precedes execution and startup retries never create another PM. |
| tests/test_pm_transport.py | Exercise lost dispatch responses, receipt polling and concurrent stale callbacks. |
| tests/test_project_brief.py | Artifact correctness: graph integrity, concurrency, isolation, and chat integration. |
| tests/test_startup.py | Launch the shell wrapper in temporary projects to verify errors, arguments and cleanup. |
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
| 2026-10-15 | iter-47 | 新增备份恢复真实存储联验、WAL、损坏归档、源变化、路径/链接保护、恢复中断及CLI回归 |
| 2026-10-15 | iter-46 | 新增快速完成/失败/验收、事务回滚、并发提交与启动去重、旧启动/掉线保护；真实 HTTP 联验三类 PM 启动与响应丢失 |
| 2026-10-15 | iter-45 | 新增持久化执行回执并发/重启/磁盘故障；真实本机 HTTP 验证 Station/Worker 响应丢失后依赖链完成；并发旧回调不重复重试 |
| 2026-10-15 | iter-44 | 本地/远端有界恢复、质量拒绝、依赖失败、计划校验和未知超时回归；线程+API验证失败诊断落库；Chrome验证错误转义及桌面手机展示 |
| 2026-10-15 | iter-43 | 新增并发交付重放、验收/旧快照竞争、回滚及重启回归；Chrome验证重放后验收结论仍在 |
| 2026-10-15 | iter-42 | 新增实际 PM 工作/监控线程回归：依赖回退、晚到结果、停止、澄清唤醒、全局超时；API 验证终态保护 |
| 2026-10-15 | iter-41 | 新增模型错误、HTTP/回执/写盘失败及真实任务库状态的交付验证，覆盖单任务与聚合路径 |
| 2026-10-14 | iter-40 | 新增下发一致性、并发、磁盘异常及会话保护测试；UI-039 覆盖真实任务/交付接口和模拟执行 |
| 2026-10-14 | iter-39 | 新增 5 项持久化历史后端测试及 UI-038 浏览器比较/恢复/冲突回归 |
| 2026-10-14 | iter-38 | 新增 7 项建议审阅后端回归及 UI-037 浏览器差异审阅场景 |
| 2026-10-13 | iter-37 | UI-036 浏览器回归：编辑历史、保存/冲突/会话边界、键盘与手机布局 |
| 2026-10-13 | iter-35/36 | Unix 启动回归、本地无服务验证 loop、状态 JSON 检查与 UTC 时间戳修复 |
| 2026-10-10 | 项目描述工作区 | 新增对话产物、三层需求 DAG、版本保存、Markdown 导出与集成验证 |
| 2026-08-16 | iter-30 补 | P1 #9: 新增 PM 三件套接口级单测 33 条 (TestPMPlanner/TestPMMonitor/TestPMDispatcher), 基线 121→154 |
| 2026-08-16 | iter-30 补 | E5: 新增 TestSecretaryFailover 6 条, 基线 115→121 |
| 2026-08-16 | iter-30 | F1: 新增 TestRoleFreeAlign 10 条, 基线 105→115; 自动升级与 post-merge 回归验证闭环 |
| 2026-08-16 | iter-29 | 新增 .githooks/post-merge: git pull 升级后自动 pytest 回归验证 |
| 2026-08-16 | iter-28 | E4: 新增 TestSecretaryConflict 7 条, 基线 98→105 |
| 2026-08-16 | iter-27 后 | 初建 |

## 项目描述验证

`tests/test_project_brief.py` 验证图结构、原子持久化、冲突、会话隔离、聊天产物和已审阅版本下发。`test_bug/project_brief_browser.py` 使用临时目录与模拟 LLM，在真实 Chrome 和 FastAPI 接口上验证编辑器及完整仪表盘；截图保存在 temp_resault/，UI 清单登记为 UI-035。

`TestRoleFreeAlign` 通过临时资源配置隔离文件存在性检查，避免测试依赖真实工作站的 resources.yaml；上库验证为 205 passed、1 skipped。

## 本地开发验证循环（iter-36）

先执行 `./scripts/start_workstation.sh --install-dev --check`，再执行 `.venv/bin/python test_bug/dev_loop.py --local`。该入口无需正在运行的 Station 或模型密钥，也不会调用在线 API 测试套件。它与 `--plan / --verify / --cycle / --close / --status` 互斥。

`local_checks.py` 依次校验 Python 语法、`loop_status.json` 可解析性、完整 pytest、UI 静态规则、设计文档清单、Bash 语法和前端 JavaScript 语法。Bash/Node 缺失时明确记录 skipped；检查失败或超时均返回非零退出码。子进程移除 PYTHONPATH/PYTHONHOME 干扰，并关闭外部 pytest 插件自动加载。每项命令默认最多 300 秒，可通过独立入口 `local_checks.py --timeout 600` 调整。

报告默认写入 `.lan_mesh/local-checks.json`（不入库），含 UTC 时间、解释器、检查命令、结果、耗时及输出。该命令不自动修改开发迭代状态或在线验证 CSV；开发者根据证据单独记录完成项。在线模型、跨主机联调和真实浏览器验收仍须分别执行。

本轮修复历史 `loop_status.json` 备注未转义引号导致的解析失败，并将状态保存时间改为带真实 UTC 时区的 ISO 时间。启动回归测试用临时项目及可控子进程覆盖依赖失败、路径空格、参数透传、异常退出和信号清理；真实服务启动另做冒烟验证。
