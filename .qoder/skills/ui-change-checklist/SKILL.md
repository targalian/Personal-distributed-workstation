---
name: ui-change-checklist
description: UI 改动待检登记规范。每当 dashboard.html 等前端文件发生 UI 改动后，必须在 test_bug/test_checklist.csv 登记一条「未检测」条目，并在浏览器行为验证后标记结果。防止 UI 行为回归被遗漏。
category: engineering
tags: [ui, test, checklist, regression, loop-engineering]
default_access: ["station", "secretary"]
version: "1.0"
---

# UI 改动待检登记规范

## 一、触发条件

**只要修改了以下任一文件，且改动涉及用户可见的界面/交互，就必须执行登记**：

- `lan_mesh/web/templates/*.html`（主要是 dashboard.html）
- `lan_mesh/web/static/*`（CSS/JS/manifest）
- 任何新增/修改前端渲染逻辑的后端端点（如返回给前端渲染的新字段、新弹窗数据源）

**不触发**的情况：纯后端逻辑、日志、注释、测试脚本本身的改动。

## 二、登记流程（改动完成后立即执行）

### 1. 登记为未检测

```bash
python test_bug/ui_change_log.py add "<改动点名称>" --detail "<改动内容与预期行为>" --level P2
```

- 编号自动递增（UI-xxx），状态自动置为「未检测」
- `--level`：核心交互链路 P1；普通面板/弹窗 P2；文案/样式 P3
- 一次迭代包含多个独立 UI 改动时，逐个登记（一个改动点一条）

### 2. 行为检测（必须实际打开浏览器验证）

静态分析（ui_tests.py）只能保证结构完整性，**不能替代行为检测**。验证方式：

- 优先：Browser subagent 实际操作页面并截图
- 其次：用户手动操作反馈

验证内容至少包括：新增/修改的按钮可点击且有预期反馈、弹窗正常打开关闭、Console 无红色报错。

### 3. 标记结果

```bash
python test_bug/ui_change_log.py check UI-xxx              # 检测通过
python test_bug/ui_change_log.py check UI-xxx --fail "原因"  # 检测失败
```

- 检测失败 = 回归：立即修复后重新验证，直到通过
- 检测通过后该项进入历史档案，日常 loop 不再提醒

## 三、状态流转

```
UI 改动 → 未检测 → 检测通过 (归档)
                  ↘ 检测失败 (回归, 计入待修复清单与健康分扣分)
```

## 四、与其他工具的联动

| 工具 | 联动方式 |
|------|---------|
| `run_loop.py` | 每日报告含「📋 UI 改动待检测」区块；`--ui-pending` 单独列出待检项 |
| `ui_tests.py` | 静态分析结束后打印未检测项提醒 |
| `loop-engineering` | 迭代 Phase D 验证阶段：若有 UI 改动，登记 + 检测是收尾必做项 |

## 五、常见问题

- **改动后没空验证怎么办**：先登记为未检测即可，每日 loop 报告会持续提醒，不允许跳过登记。
- **用户反馈"按钮点了没反应"类问题**：先排查浏览器缓存（页面是否有 Cache-Control: no-cache），再登记检测失败项排查代码。
- **一次大改 UI 怎么拆**：按"用户可感知的独立交互单元"拆，如「配置向导弹窗」「资源池卡片」各记一条。
