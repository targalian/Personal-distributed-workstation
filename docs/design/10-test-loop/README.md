# 10 测试与验证循环

四层验证体系：pytest 基线、专项验证脚本、UI 走查、Loop Engineering 每日循环。

## 模块清单

| 文件/目录 | 职责一句话 |
|---|---|
| tests/test_core.py | pytest 基线测试 (当前 98 条) |
| test_bug/run_loop.py | Loop 循环调度入口 |
| test_bug/dev_loop.py / nightly_loop.py / daily_loop.bat | 开发/夜间/每日循环形态 |
| test_bug/api_tests.py / ui_tests.py / discover_tests.py | API / UI / 发现层测试 |
| test_bug/ui_change_log.py | UI 变更登记 (test_checklist.csv) |
| test_bug/loop_config.yaml | 循环配置 |
| test_bug/test_checklist.csv | UI 变更清单 (UI-0xx 编号) |

---

## tests/test_core.py — pytest 基线

**规范**: 全部改动后必须全绿（当前 98/98）。覆盖核心链路:
协议/DB 迁移/评级/任务 DAG/路由评分/密钥加解密/启动同步幂等等。

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
current_phase / notes / next_tasks）、.githooks/（commit-msg / pre-push）。

## 四层验证流程（每个迭代必走）

1. 专项 `_xN_check.py`（编号递增，通过后删除）
2. pytest 基线全绿
3. 涉服务改动 → Station 重启冒烟
4. UI 改动 → checklist 登记 + Browser 实测

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-08-16 | iter-27 后 | 初建 |
