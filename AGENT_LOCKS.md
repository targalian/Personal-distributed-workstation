# Agent 占用登记表

多 Agent（Codex CLI / Qoder Quest）并行开发时的文件占用与交接看板。
**开工前必读，认领后立即回写，完工后立即释放。** 规则见 AGENTS.md「多 Agent 协作」。

- 更新时间：2026-09-02
- 当前迭代：`iter-76`（已完工：start_workstation 三端脚本对齐，待 Boss 发货）

## 一、职责边界（长期约定）

| 范围 | 归属 | 说明 |
|---|---|---|
| `lan_mesh/**.py` | **Codex** | 后端主控/PM/调度/DB 逻辑，改动即需跑 pytest 全量 |
| `tests/test_core.py` | **Codex** | 专项用例随代码同轮提交（追加在文件尾部，减少冲突面） |
| `docs/design/**` | **Codex 主** / Quest 补 | 代码行为变更由改代码方同步；纯表述修订 Quest 可动 |
| `.qoder/repowiki/**` | **Quest** | post-commit hook 拉起 `qoderclicn` 自动维护，Codex 不得手改 |
| `.qoder/skills/**` | **Quest** | 技能手册（loop-engineering / repowiki-update 等） |
| `docs/reference/**` | **Quest** | 审查报告、能力评估、重构提案（只出结论不改代码） |
| `webui/**`、`quicklan-main/**` | **Quest** | 前端 SPA / Tauri 子项目 |
| `AGENTS.md`、`loop_status.json`、`VERSION.json` | **共享·串行** | 单次只允许一方持有，改完立刻提交释放 |
| `.githooks/**`、`scripts/sync_push.ps1` | **需先登记** | 门禁基础设施，改动前在下表登记并说明理由 |

## 二、当前占用

| Agent | 占用文件 | 任务 | 开始 | 状态 |
|---|---|---|---|---|
| Codex | 空闲 | 无 | —— | 已释放 |
| Quest | 空闲 | 无 | —— | 已释放 |

> 认领格式：一行一个 Agent，`占用文件` 写通配范围（如 `lan_mesh/station_*.py`），
> `状态` 取 `进行中` / `待验证` / `已释放`。释放后把该行改回 `——`。

## 三、待推送内容归属（工作区当前脏文件）

`scripts/sync_push.ps1` 要求工作区干净，因此下列改动必须**分两次提交**、
按归属各自提交，不要互相 `git add .`：

**Quest（iter-73 优化讨论窗口 UI + iter-74 发送通道点亮，已验证待 Boss 发货）**
- `lan_mesh/web/templates/dashboard.html`（opt-panel 两段式 + 优化讨论窗口 + 💬 话题切换 + iter-74 发送通道点亮: optDiscussSend 调真实端点 / WS chat_reply 分流 / loadChatHistory 过滤 opt_discuss 历史防串台 / 503 提示）
- `docs/design/09-frontend/README.md`（iter-73/74 段落 + 变更记录）
- `test_bug/test_checklist.csv`（UI-059 + UI-060，检测通过）
- `loop_status.json`（iter-74 `[Quest]` 段落）

**Codex（iter-75 拆分 Phase 3-5 收官，已验证待 Boss 发货）**
- `lan_mesh/station_controller.py`（壳类终态 246 行：docstring / imports / mixin 组合声明 / StationState / __init__；WEB_DIR、TEMPLATES_DIR、STATIC_DIR 改为从 station_lifecycle re-export）
- `lan_mesh/station_{pm_control,scheduler,secretary,local_pm,lifecycle}.py`（Phase 3-5：53 方法搬入，分别 5/12/10/16/10）
- `tests/test_core.py`（12 处 monkeypatch 目标随方法迁移：11 处 `sc_sched` + 1 处 `sc_pmctl`；pytest 400 passed）
- `docs/design/02-station-core/README.md`（节标题 iter-74/75、8 mixin 完整落地表、常量迁移与 monkeypatch 踩坑两节、变更记录）
- `docs/reference/controller-split-plan.md`（状态 → Phase 1-5 已执行完毕，仅剩 Phase 6；属 Quest 目录，已仅改状态行）
- `AGENTS.md`（模块职责表：壳类描述 + 8 mixin 按职责合并为两行）
- `loop_status.json`、`AGENT_LOCKS.md`（iter-75 收尾 + 锁释放）

## 四、下一轮排期建议（避免同文件竞争）

| 任务 | 建议归属 | 冲突面 |
|---|---|---|
| ✅ 点亮优化讨论发送 UI（iter-74 已完成: `optDiscussSend()` 调 `POST /api/secretary/chat` 带 `discuss_context`，WS 分流 + 历史过滤 + 503 提示，UI-060 通过） | ~~Quest~~ | ~~`dashboard.html` 单文件~~ |
| ✅ `station_controller.py` 拆 8 mixin Phase 1-2（iter-74 已完成：组合接线 + SelfHeal/Hosts/Sync 三块 28 方法搬入，壳类 3253→2322 行） | ~~Codex~~ | ~~已释放~~ |
| ✅ `station_controller.py` 拆分 Phase 3-5（iter-75 已完成：53 方法入 5 mixin，壳类 3253→246 行；修复 12 处测试 monkeypatch 目标迁移） | ~~Codex~~ | ~~已释放~~ |
| Phase 6 收尾：repowiki 同步 8 个 mixin 模块卡片 + 架构图（design 文档 Codex 已同步） | **Quest 承接** | 仅文档/知识库，代码侧已完结；等 Boss 推送后启动 |
| 真物理多机实压 F3.1/F3.3 | Codex | 需真实主机，与拆分互斥（勿同轮） |
| 前端 Tab 与新端点字段对齐复查 | Quest | `webui/`、`dashboard.html` |

## 五、人在回路外（Boss 观察与发货）

Codex 的沙箱把 `.git` 挂为只读（NTFS 层其实有 Modify 权限），因此**提交/推送是
唯一必须由非 Codex 执行的步骤**。为此提供两个脚本，Boss 无需参与开发决策：

| 场景 | 命令 |
|---|---|
| 看全局（版本/迭代/归属/待推送/占用/队列） | `python scripts/dev_status.py` |
| 看全局 + 跨 Agent 交接详情 | `python scripts/dev_status.py --notes` |
| 看全局 + 门禁自检 | `python scripts/dev_status.py --verify` |
| **一键发货**（分批提交 + 推送，逐步 y/N） | `powershell -File scripts/ship.ps1` |
| 发货预演（只看不做） | `powershell -File scripts/ship.ps1 -DryRun` |
| 完全无人值守 | `powershell -File scripts/ship.ps1 -Yes` |
| 只提交不推送 | `powershell -File scripts/ship.ps1 -NoPush` |

`ship.ps1` 的固化逻辑（无需人工判断归属）：
1. 门禁前置（编译 + `sync_docs`），FAIL 即中止，不留半成品提交；
2. 批 1 = Quest/共享（wiki、文档、协作机制、配置示例），期间置
   `LAN_MESH_WIKI_DRY_RUN=1` 静音 post-commit，避免 Quest 后台任务与批 2 抢
   repowiki 脏文件；
3. 批 2 = Codex 代码（`lan_mesh/` + `tests/` + `docs/design/`），此时工作区已净，
   hook 可安全拉起 Quest 同步 wiki；
4. 工作区干净后调 `scripts/sync_push.ps1` 推双仓库（自动补 VERSION.json 同步提交）。

## 六、交接纪律

1. **结论落盘**：跨 Agent 结论写 `loop_status.json.notes`（带 `[Quest]` / `[Codex]` 前缀）
   或 `docs/reference/`，会话历史对方读不到。
2. **单轮单任务**：一轮只认领一个路线图任务，缩短占用窗口。
3. **推送前 rebase**：`git pull --rebase` 后再走 `scripts/sync_push.ps1`，
   撞 `loop_status.json` 时手工合并两方 notes，不要择一覆盖。
4. **hook 联动**：Codex 提交代码 → post-commit 入队 `.qoder/repowiki/.pending/` →
   Quest 侧 `qoderclicn` 后台消费。Codex 不要清空该队列。
5. **锁超时**：占用超过 24h 未更新视为失效，接手方在本表注明「接管原因」后可强占。
