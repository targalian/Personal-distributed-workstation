---
name: repowiki-update
description: 基于 git 新提交同步更新 .qoder/repowiki（Qoder Repo Wiki）。扫描引用已删除/改名文件的过时条目，对照 station_controller/station_api 体系修正 content/*.md 与 knowledge 模块卡片，复扫验证零残留后自动提交。post-commit hook 后台拉起或用户手动请求更新 wiki 时使用。
category: engineering
tags: [wiki, repowiki, git-hook, drift, docs]
default_access: ["station", "secretary"]
version: "1.0"
---

# Repo Wiki 更新技能

## 一、触发条件

- `.githooks/post-commit` 检测到代码文件变更后，写 pending 队列并后台拉起 qoderclicn 执行本技能
- 用户手动请求「更新 repo wiki / 同步 wiki」
- `.qoder/repowiki/.pending/pending-commits.txt` 存在未消费待办（上次任务失败/未执行）

## 二、输入与输出

- **输入**：`pending-commits.txt`，每行 `commit_hash 时间戳 变更文件数`；无待办时改用 `git show HEAD` 的变更
- **输出**：更新后的 repowiki md/yaml + 自动提交 `docs(wiki): 同步 repo wiki <short-hash>`

## 三、工作流

### 1. 分析变更

逐条读 pending 提交，`git show <hash> --stat` 识别：删除/重命名文件、职责迁移、端点迁移、新增模块。非架构性变更（纯 bug 修复、文案微调）可跳过。

### 2. 扫描过时引用

```bash
python .qoder/skills/repowiki-update/scripts/scan_refs.py
```

输出缺失文件引用清单（文件:行号:内容）。零输出 = 无需修复，直接清理 pending 退出。

### 3. 修复映射

- `master.py` / `secretary.py`（已删除）：
  - `API 参考手册/`、`Web API 接口/` 目录下的文档 → `station_api.py`
  - 其余（架构设计/核心模块/部署指南/快速开始等）→ `station_controller.py`
- 失效行号范围 `#Lxx-Lyy` / `:xx-yy` 一并去掉（指向旧代码无意义）
- 同行角色标签同步替换：`MasterController`→`StationDirector`、`Master 控制器`→`Station Director`、`Secretary 控制器`→`Station Director`、`Secretary 节点`→`Station 节点`、`Master/Worker`→`Station/Worker`
- `file://` URL 必须保留 `lan_mesh/` 前缀（指向 `file://lan_mesh/station_controller.py` 而非根目录）
- knowledge 卡同步：`_module.yaml` 的 `source_files` 增删对应文件；卡内 md 的职责描述、角色列表对齐现状
- 映射表外的缺失文件：`git log --diff-filter=D --oneline -- <file>` 查删除提交，从提交信息判定承接文件后手工修复

### 4. 验证（必须，失败不得提交）

```bash
python .qoder/skills/repowiki-update/scripts/scan_refs.py --verify
```

退出码 0 = 零残留引用 + 全部 `_module.yaml` 可解析，才允许进入提交步骤。

### 5. 自动提交

```bash
git add .qoder/repowiki
git reset -q .qoder/repowiki/.pending   # 运行时状态不入库 (.gitignore 已覆盖, 双保险)
git commit -m "docs(station): 同步 repo wiki <short-hash>"
```

提交只含 repowiki 变更 → post-commit 守卫 1 跳过，不会递归触发。scope 用 `station`（commit-msg 白名单），勿用 `wiki`。

### 6. 清理

清空 `pending-commits.txt`、删除 `.pending/.lock`，并回显本次更新摘要。

## 四、守卫与硬约束

- **防递归**：仅 `.qoder/repowiki/` 变更的提交不触发本技能
- **防并发**：`.pending/.lock` 存在且 10 分钟内 → 只追加 pending 不重复拉起
- **禁手改** `zh/meta/repowiki-metadata.json`（加密元数据，面板生成专属）
- **禁重命名** wiki 目录/文件名（`Master API 接口/` 等旧术语目录名需连带 metadata 改造，超出本技能范围，遇此情形在最终摘要中标注留待后续）
- 更新只动 `.qoder/repowiki/`，不得顺手改业务代码

## 五、常见问题

- **hook 没拉起 agent**：pending 已入队，手动按本技能执行即可
- **--verify 失败**：按清单修复后重验，禁止强行提交
- **agent 中途失败**：lock 10 分钟自动过期，pending 保留，下次代码提交自动重试
- **旧术语目录名还在**：属已知遗留（见硬约束），本次修复只覆盖目录内文件引用
