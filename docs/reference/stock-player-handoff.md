# stock_player 项目交接与任务图 (Codex as Boss → 秘书)

- 出具方: Codex CLI (代 Boss 执行交接与监管) ・ 日期: 2026-09-09
- 需求源: `C:\Users\WindowShopper\Desktop\项目描述.txt` (1142 字)
- 目标仓库: `E:\ingobj\stock_player` → https://gitee.com/zhu-longzhuo/shrimp-quantification
- 机器可读任务图: `docs/reference/stock-player-task-graph.json` (schema `lan_mesh.task_graph/v1`)

## 一、为什么是「图」而不是队列

三个开发方向不是线性阶段, 而是**互相供给**的网络:

- 方向3 (基础迭代) 的产物 — 模块分层、指标口径、数据管线提速 — 是方向1 (平台化)
  抽策略 DSL、做回测服务的地基;
- 方向3 的统计口径直接决定方向2 (RL) 的奖励函数能不能定义;
- 方向1 与方向2 又都会反向要求方向3 补基础能力。

用任务队列会丢掉这些横向边 (`informs`), 用 plan 只能表达单链。所以落成
**16 节点 / 22 边的有环检测通过的 DAG**, 边区分三类语义:

| 边类型 | 含义 |
|---|---|
| `depends_on` | 硬依赖, 前置未完成不得开工 |
| `blocks` | 闸门阻塞 (澄清未过 → 三方向全锁) |
| `informs` | 软供给, 前者产出显著降低后者成本/风险 |

关键路径: `C1 → G0 → N1 → N2 → D3 → D3-1 → D1-1 → D1-3`

## 二、图的骨架

```
C2 监管闭环 (running)
 ↑
C1 Codex↔秘书/PM 通道 (done) ──informs──┐
                                        ↓
N1 项目基线接管 ──depends_on──→ N2 工作站接入 ←──depends_on── G0 需求澄清闸门
                                        ↓                        │
                                   D3 方向3 基础迭代 ←──blocks────┤
                                    ├ D3-1 根目录治理与分层        │
                                    ├ D3-2 数据管线加速            │
                                    └ D3-3 统计指标与回归基准      │
                                        │ informs                 │
                                        ↓                         │
                                   D1 方向1 平台化 ←──blocks───────┤
                                    ├ D1-1 策略组合 DSL            │
                                    ├ D1-2 回测引擎服务化          │
                                    └ D1-3 exe 打包流水线          │
                                                                  │
                                   D2 方向2 智能化 ←──blocks───────┘
                                    ├ D2-1 环境与奖励定义 spike
                                    └ D2-2 行情/regime 特征管线
```

## 三、G0 闸门: 12 个必须 Boss 回答的问题

项目描述原文明说「这三个方向的细节都很模糊, 你可以向我提问, **不要默认, 或
揣测我的意思**」。因此 G0 是硬闸门 — 未答完不开工。问题全文见任务图 JSON 的
`open_questions_for_boss`, 摘要:

| # | 主题 | 问题 |
|---|---|---|
| Q1 | 方向优先级 | 先做哪个方向? **建议方向3 先行** (风险最低、可验证、是另两个的地基) |
| Q2 | 方向1 形态 | 「面向大众的平台」是公网 SaaS 还是本地/内网分发? |
| Q3 | 方向1 密钥 | 产出的 exe 是否携带券商密钥? 合规与免责边界? |
| Q4 | 方向2 算力 | RL 训练在哪跑? 与外部开发者如何协作 (同仓/独立仓/只交模型)? |
| Q5 | 方向2 口径 | 「维持一定收益」如何量化 (年化/夏普/最大回撤)? 不可量化则无法进验收自检 |
| Q6 | 方向3 痛点 | 最痛的是下载/入库/回测速度/统计口径中的哪一环? 有实测数字吗? |
| Q7 | 分支策略 | 每方向一条长期分支, 还是单 develop + PR? |
| Q8 | 现状可信度 | 根目录 70+ 散文件 (含 `_test_em1~8.py`、多个 screenshot、两个 .db) 哪些是有效资产? 允许结构治理吗? |
| Q9 | 实盘边界 | 是否已接真实资金? 开发期能碰实盘账号还是一律走 sim? |
| Q10 | 数据源 | 行情源是否有配额/付费限制? 回测需要多少年历史? |
| Q11 | 预算 | 项目 LLM 预算上限与允许模型 (用于 Station 项目预算与路由策略)? |
| Q12 | 首个里程碑 | 第一个可交付形态 (提速报告 / 干净重构 / 能跑的回测 API)? 渐进第一步多大? |

## 四、Agent 数量与角色分配

**结论: 常态 4 个角色 (2 个已在岗 + 2 个按方向启用), 不建议一次拉满。**

| 角色 | 承载 | 归属文件域 | 职责 | 何时启用 |
|---|---|---|---|---|
| **Boss 代理** | Codex CLI (本会话) | `scripts/`、`lan_mesh/**.py` | 与秘书交接、审阅蓝图、监管进展、修工作站 bug、发货前门禁 | 已在岗 |
| **秘书 (Secretary)** | Station 常驻 | — | 需求收集状态机 (iter-78) 多轮澄清 → Brief → 派发; 意图识别与动作执行 | 已在岗 (`/health` secretary=active) |
| **PM Agent** | Station 按任务启停 | 项目工作区 | 任务规划 (iter-89 蓝图驱动)、团队组建、子任务分发、交付前验收自检 (iter-90) | G0 过闸 + N2 完成后, 每个 epic 一个 PM |
| **Quest** | Qoder Quest | `webui/**`、`dashboard.html`、`.qoder/**` | 工作站前端与知识库 (与本项目并行, 已有 UI-064~075 backlog) | 已在岗, 不介入 stock_player |

**stock_player 侧的执行编队 (G0 后逐步启用, 不要一次全开)**:

| 阶段 | Agent 数 | 编队 |
|---|---|---|
| 澄清期 (现在) | 2 | Codex (Boss 代理) + 秘书 |
| 首里程碑 (方向3) | 3 | +1 PM (D3), 下挂 2~3 子 Agent (分层 / profiling / 指标) |
| 方向1 展开 | 4 | +1 PM (D1), 下挂 DSL / 回测 / 打包 子 Agent |
| 方向2 展开 | 5 | +1 PM (D2) + 外部 RL 开发人员 (人, 非 Agent) |

**不建议现在多 Agent 并行的理由**: G0 未过 → 三方向全被 `blocks` 边锁住;
此时拉起多个 PM 只会基于揣测产出返工代码, 正是项目描述明确禁止的。

## 五、Codex 与秘书/PM 的通道 (本轮已交付)

`scripts/boss_channel.py` — 12 个子命令, 详见
`docs/design/11-scripts-subprojects/README.md`。典型用法:

```powershell
python scripts/boss_channel.py diag                        # 每轮监管入口
python scripts/boss_channel.py chat "交接内容..."           # 向秘书投递
python scripts/boss_channel.py blueprint <pid> --set-file bp.json
python scripts/boss_channel.py tasks --status running
python scripts/boss_channel.py progress <pm_id>            # 监管 PM 细节
python scripts/boss_channel.py reply <pm_id> "决策回复"      # PM 提问时回话
python scripts/boss_channel.py watch --interval 15         # 长任务盯盘
```

**用于新功能开发 / 运行测试 / bug 排查的分工**:

- **新功能开发**: Codex 写工作站侧代码 (`lan_mesh/**.py`); 项目侧功能经
  `chat` 交给秘书 → PM 拆解执行, Codex 用 `progress` / `task` 验收。
- **运行测试**: 工作站侧 `python -m pytest -q` 由 Codex 直接跑; 项目侧回归
  由 PM 在其工作区跑, 结果经 `acceptance_review` 回流。
- **bug 排查**: `diag` 一次性体检 → 定位到工作站侧则 Codex 当轮修 + 补专项用例
  + 变异验证; 定位到项目侧则写进 PM 的输入或 `loop_status.notes`。

## 六、本轮监管即抓到的工作站问题

| 编号 | 现象 | 处置 |
|---|---|---|
| **BUG-032** | `/api/runtime/task-stall-alerts` 返回 100 条停滞告警, 其中 99 条 task_id 在 DB `tasks` 表已不存在 (JSONL 残留), 级别多为 Lv3, `idle_min` 高达 11455 分钟 — 真实告警被噪声完全淹没 | **已修**: `check_stall_alerts()` 新增 `_stall_db_filter()` 与 DB 对账, 抑制幽灵与已终态任务; 真实数据实测 100 → 0; 专项 5 例 + 变异 3 处致红 |
| 通道端点错 | `diag` 初版使用 `/api/runtime/errors` → 404 | **已修**: 改为 `/api/errors/recent` |
| 项目 git 不可读 | `E:\ingobj\stock_player` 报 `unsafe repository (owned by someone else)`, 无法读分支/日志 | 记入图节点 N1, 需 Boss 授权 `git config --global --add safe.directory` 后接管 |
| 历史脏数据 | Station 有 6 个项目, 其中 5 个是 `[LoopTest]` 自动化测试残留 | **已归档** (2026-09-09 Quest 执行: `DELETE /api/projects/{id}` ×5 → archived, 正式项目「创建并启动项目」保持 active; `api_tests.py` BTN-009/010 已加创建即清理 (取消任务/归档项目) 防复发) |

## 七、下一步 (等 Boss 回话)

1. **Boss 回答 G0 的 12 个问题** (至少 Q1/Q7/Q8/Q11/Q12 五个, 即可解锁首里程碑);
2. Codex 据回答生成蓝图 JSON → `boss_channel.py blueprint <pid> --set-file` 写入;
3. Codex 用 `chat` 把冻结后的首里程碑范围交接给秘书, 由秘书派发 PM;
4. Codex 转入监管模式: `watch` + `diag` + `progress`, 工作站 bug 当轮修。
