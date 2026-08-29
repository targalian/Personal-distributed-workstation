# 承载中大型项目能力评估 · 补强后复审

> 初评 (iter-52, commit e1e4194): 「具备承载中大型项目的能力，但承载形态是
> "1 Boss + 多机算力网格"，而非"平台级多人协作"」，列出 6 项缺口与补强建议。
> 本文为 iter-53~58 六轮补强全部闭环后的复审 (iter-59)。

## 一、六项补强闭环验收

| # | 原缺口 (初评) | 实施迭代 | 验收证据 | 结论 |
|---|---|---|---|---|
| 1 | 崩溃恢复不完整: PMState 纯内存, 重启丢失中间执行态 (P0) | iter-53 `66c3c36` | PM 执行态快照持久化 + 断点恢复 (快照往返/就地恢复/恢复四场景/multi 生命周期), TestPMSnapshotResume 专项覆盖 | ✅ 闭合 |
| 2 | 日志容量无治理: 5 张日志表无限增长 (P0) | iter-54 `6a0474f` | Database.prune_logs 按保留期清理 (llm_call_log/chat_history/resource_usage_log 仅删已上报) + VACUUM + 手动修剪端点, 配置驱动 | ✅ 闭合 |
| 3 | 多机实测覆盖不足: 跨机链路仅单机验证 (P1) | iter-55 `1e6cc1f` | 双实例隔离真机联验: WS 直推/HTTP 兜底双通道、轮换记账归属、让位主机惰性 Worker runtime、模型资源预加载 | ✅ 闭合 |
| 4 | 前端单文件架构: 7 Tab 单 HTML, 无 SPA/DAG 可视化 (P1) | iter-56 `94d3adb` | React SPA (webui/: Vite+React+TS+xyflow) 三页面 + DAG 可视化编辑器, UI-049 Browser 实测 (真实任务链路 + PUT 落库) | ✅ 闭合 |
| 5 | 任务并发默认 5 未压测: DB 线程安全与队列表现未知 (P2) | iter-57 `887a42f` | 真机压测 1800 req 0 错误 (QPS 549.6) + 20 并发提交 0.22s 全 200 (1 running + 19 排队接力); DB 加固 WAL+busy_timeout 30s; 限流双桶 | ✅ 闭合 |
| 6 | 无多用户权限: 仅 mesh_token 节点级认证 (P2) | iter-58 `163a2f5` | security.users 配置驱动用户表 + 中间件角色分层 (boss/operator/viewer) + auth-token 收紧防提权; 真机 API 13 项 + Browser 5 步实测 (UI-050) | ✅ 闭合 |

**回归基线**: 测试从 269 (iter-52) 增至 **313 全绿**; 文档同步 sync_docs 84 条目 11 域无漂移。

## 二、复审结论

**初评 6 项缺口全部闭合，能力评估升级为:
「具备承载中大型项目的能力 — 六大支柱完整 + 边界约束已消除」。**

初评三大约束逐一复核:

| 初评约束 | 复审状态 |
|---|---|
| 「缺口 1 (崩溃恢复) 是最实质的能力边界 — 长任务重启风险敏感」 | **已消除**: 执行态快照持久化 + resume 恢复 (iter-53) |
| 「缺口 3: 跨机扩展链路的可靠性承诺仍基于单机验证」 | **已消除**: 双机隔离实测闭环 (iter-55) |
| 「缺口 5: 团队协作场景缺失」 | **已消除 (按需启用)**: 多用户角色权限可配置开启, 默认关闭向后兼容 (iter-58) |

承载形态从「1 Boss + 多机算力网格」扩展为
**「1 Boss + 多机算力网格 + 团队角色协作 (配置启用)」**。

## 三、剩余边界 (诚实列出)

1. **团队规模边界**: 用户表为配置驱动静态 token (无自助注册/密码找回/SSO), 适合小型可信团队; 10+ 用户的组织级协作需外部身份系统对接, 超出个人工作站定位。
2. **集群规模效应已实测 (iter-66/67/68/69)**: iter-66 三机集群 (1 Secretary + 2 Worker + mock LLM) 真机实测 F3.1 自动扩缩容 + F3.3 PM 迁移全链路 17/17 (修复 9 个真实 bug A-I); iter-67 五节点集群 (1 Secretary + 4 Worker 单机多实例模拟) 实压 13/13 — 4 积压全部派发无滞留 (Bug J 水位门槛修正)、4 Worker 各承载 1 任务无重复派发、FIFO 顺序、5 任务并发、杀机 F3.3 回归 (Bug K pm_agents 落表); iter-68 扩容同轮批量清空 (30s/轮×N 积压滞后修复) 五节点 14/14 (4 积压 18s 清空 vs 旧 120s+); iter-69 七节点集群 (1 Secretary + 6 Worker 单机多实例) 实压暴露 Bug L — 6 积压同轮派发正常 (13s), 但全 Worker 离线时 F3.3 本机接管分支因 PM 构造签名过期必然 TypeError, 任务滞留 pending, 已修复为复用 `_local_start_pm` 唯一入口 (专项 7/7 + 回归 380); 真物理多机 (>2 台) 的大集群行为仍待实压。
3. **跨网段联邦 (F3.4 多 Secretary) 已实施 (iter-64/65)**: 静态 peer 配置 + /api/federation/info + 联邦轮询同步 (source=fed 隔离/选举仅限本网段/离线检测, iter-64) + 联邦任务跨网段转发 (选站分层 lan 优先/fed 兜底 + forwarded 徽标 + 防环跳数上限 1, iter-65); 跨网段实际通信依赖多网段物理环境, 未真机跨网实压。
4. **产品化选项 (F5.3 插件系统 / F5.4 移动端) 已实施 (iter-61/62)**: F5.3 第三方 Skill 市场 (白名单安装/卸载/来源追踪, iter-61 `dd0becf`) + F5.4 移动端 PWA (Service Worker 离线壳 + 移动导航, iter-62 `bdb1307`), 均已真机验证。

## 四、后续路线建议

- ~~Phase 5 收尾 (P3): F5.3 插件系统 / F5.4 移动端 App~~ **已完成 (iter-61/62)**
- ~~主线深化候选 (P2): F4.2 异常自愈全自动闭环~~ **已完成 (iter-60, 上库 a627dfc)**
- ~~团队场景深化: 用户管理 UI、token 轮换端点~~ **已完成 (iter-63: users 表 + token 哈希存储 + 5 管理端点 + 最后 boss 防自锁, UI-053 Browser 12/12)**
- ~~多机深化 (P3): F3.4 跨网段多 Secretary 联邦~~ **已完成 (iter-64/65)**
- **下一步候选 (按优先级)**: ① 真物理多机实压 (需 ≥2 台真实主机环境); ② 大集群模拟深化 (10+ 节点); ③ 组织级身份系统对接 (10+ 用户, 超出个人工作站定位暂缓)

## 五、证据索引

| 迭代 | commit | 关键证据 |
|---|---|---|
| iter-53 | 66c3c36 | TestPMSnapshotResume (docstring 16. pm-snapshot-resume) |
| iter-54 | 6a0474f | TestLogPruning (docstring 17. log-pruning) |
| iter-55 | 1e6cc1f | 双实例隔离实测 (docstring 18. iter55-multihost) |
| iter-56 | 94d3adb | UI-049 Browser 实测 (SPA 三页面 + DAG 落库) |
| iter-57 | 887a42f | 压测 1800 req 0 错误; 20 并发 0.22s 全 200; 严格桶 119/81 (docstring 20. iter57-concurrency) |
| iter-58 | 163a2f5 | API 13 项 + Browser 5 步 (UI-050, 含 2 缺陷修复); TestIter58Permissions 7 项 (docstring 21) |
| iter-66 | 628c6c7 | 三机集群真机 17/17 (9 真实 bug A-I: auth_headers/task_data/映射含 task_id/取消清映射/派发即置 running/FIFO/控制命令死锁级联等) |
| iter-67 | 331c936 | 五节点集群真机 13/13 (Bug J 扩容门槛 >=1 / Bug K pm_agents 落表) |
| iter-68 | 5d3ae96 | 扩容同轮批量清空 (30s/轮×N 滞后修复): 五节点 14/14 (4 积压 18s 清空 vs 旧 120s+) + 专项 16/16 + 回归 373 |
| iter-69 | (本次) | 七节点集群实压 → Bug L: F3.3 本机接管 PM 构造签名过期 (TypeError, 任务滞留 pending); 修复为复用 `_local_start_pm` 唯一入口 + `_register_local_pm` 统一登记; 专项 7/7 + 回归 380 passed |
