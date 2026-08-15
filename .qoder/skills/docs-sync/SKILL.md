---
name: docs-sync
description: 设计文档同步规范。修改 lan_mesh 等代码模块的职责/接口/设计决策时，必须同步更新 docs/design/ 对应功能域文档；增删脚本时须在 scripts/sync_docs.py 的 MAPPING 登记并运行 --write。防止文档与代码漂移。
category: engineering
tags: [docs, design, sync, drift, loop-engineering]
default_access: ["station", "secretary"]
version: "1.0"
---

# 设计文档同步规范

## 一、触发条件

**改动以下任一内容时，必须执行文档同步**：

- 模块职责变化（模块做什么变了）
- 公开接口变化（新增/删除/改签名/改返回语义）
- 设计决策变化（算法替换、数据流调整、架构模式切换）
- 增删脚本文件（`lan_mesh/*.py`、`tests/*.py`、`test_bug/*.py`、`scripts/*.py`）

**豁免**（不需要动文档）：

- 纯实现优化（性能调优、内部重构不改外部行为）
- 日志文案、注释、常量微调
- 纯 bug 修复（行为符合原设计，未改变接口语义）

## 二、同步流程

### 1. 定位目标文档

映射表唯一事实源：`scripts/sync_docs.py` 的 `MAPPING`（文件 → 功能域编号）。
域文档位置：`docs/design/{编号}-{域名}/README.md`。

### 2. 更新内容

- 职责/接口/设计决策变化 → 修改对应章节（职责/设计要点/关键接口）
- 每次更新在文档末尾「变更记录」表追加一行（日期 | 迭代 | 摘要）

### 3. 增删脚本时的额外步骤

- 在 `sync_docs.py` 的 `MAPPING` 登记/删除一行（文件 → 域）
- 新脚本必须有模块 docstring 首行（清单表的数据源）
- 运行 `python scripts/sync_docs.py --write` 同步清单
- 运行 `python scripts/sync_docs.py` 确认 PASS

## 三、强制校验（pre-push hook）

- **第 8 项**：代码变更时自动跑 `sync_docs.py`，清单漂移/未登记 → 阻断上库
- **第 9 项**：代码变更但 `docs/design/` 无变更 → 警告提醒（豁免类改动可忽略）

## 四、常见问题

- **改了代码但忘了改文档**：push 时第 9 项会警告，补上即可
- **增删脚本忘了登记**：push 时第 8 项直接阻断，按 MAPPING 提示登记一行
- **纯 bug 修复也要改文档吗**：不需要（豁免类）；但若修复改变了接口语义则要改
- **清单表能手动编辑吗**：不能。`<!-- AUTO:module-list -->` 区块由生成器管理，手动改会被 `--write` 覆盖；想改清单描述就改模块 docstring
