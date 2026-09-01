# StationController 拆分方案（只出方案，不执行）

- 编写日期：2026-08-29
- 编写人：Quest（前端/文档 Agent）
- 状态：**Phase 1-5 已执行完毕**（Phase 1-2 iter-74 / Phase 3-5 iter-75，2026-09-01，Codex）。
  8 个 mixin 全部落地，81 方法已搬出，壳类 3253 → 246 行（仅剩 docstring / imports / mixin 组合声明 /
  `StationState` / `__init__`）。仅剩 **Phase 6 文档收尾**（repowiki 同步，Quest 承接）。
  落地细节与踩坑记录见 `docs/design/02-station-core/README.md` 「职责域拆分 (iter-74/75)」节。
- 原始状态：提案稿（2026-08-29 Quest 编写，不含代码改动）；执行前须重新确认
  `loop_status.json` 无其他 Agent 占用 lan_mesh/ 文件。
- 依据：`lan_mesh/station_controller.py` 现状（152,739 字节 / 3,253 行 /
  83 个顶层方法）+ AGENTS.md 模块职责表 + repowiki 知识库（架构设计/编码规范卡）。

## 1. 现状与问题

`station_controller.py` 是项目最大单文件（152KB），远超第二名 `database.py`
（97KB）。单文件内耦合了八类互不相干的职责：

| 职责域 | 代表方法（行号） | 估计行数 |
|---|---|---|
| 生命周期（启动/停/端口/App 装配/WS 推流/dev-reload） | `__init__` 98、`start` 2957、`_create_app` 2869、`_ws_push_loop` 2834 | ~580 |
| 自愈闭环 F4.2 | `run_heal_action` 262、`_auto_heal_once` 394、`get_auto_heal_status` 485 | ~245 |
| Secretary 激活/选举/故障转移 | `activate_secretary` 535、`_secretary_election` 716、`_secretary_failover_check` 826 | ~370 |
| 内嵌 PM（本机执行/派发/汇报/孤儿迁移） | `_local_start_pm` 1271、`_dispatch_queued_task` 1307、`_try_periodic_report` 2518 | ~410 |
| PM 远程控制 + 任务图 | `cancel_task` 1585、`pause_task` 1652、`update_task_graph` 1736 | ~240 |
| 任务提交/负载选站/联邦/自动扩容 | `submit_task_from_chat` 989、`_pick_task_host` 1191、`_autoscale_check` 2593、`_federation_loop` 961 | ~565 |
| 密钥/配置/版本同步 | `_align_config_with_peers` 2089、`pull_resource_secrets` 2214、`_auto_upgrade` 2380 | ~510 |
| 发现/主机/Bot/配置刷新/日志修剪 | `_on_device_seen` 1860、`_collect_info` 1838、`_on_bot_command` 1804、`_prune_loop` 2004 | ~235 |

问题表现：单文件 review 困难、git 冲突面大（两个 Agent 并行时几乎必撞）、
函数间隐式依赖靠 self 属性传递难以定位、新职责无家可归只能继续往大文件里塞。

## 2. 拆分原则（硬约束）

1. **不破坏 import 路径**：`from lan_mesh.station_controller import StationController`
   （main.py、tests/test_core.py 16 处、AGENTS.md 验证命令）保持不变。
2. **不破坏运行时引用**：所有路由模块以闭包方式持有 controller 整体对象，
   只访问属性/方法名（见 §4 引用清单）。方法搬移到 mixin 后 `self.xxx` 名字
   不变，路由层零感知。
3. **mixin 组合而非继承树**：`class StationController(MixinA, ..., MixinH)`，
   mixin 之间不互相继承，避免 MRO 复杂度。
4. **渐进迁移**：每搬一个 mixin 跑一次全量验证，任一步失败可独立回滚。
5. **命名沿用拆分先例**：参照 `station_routes_basic/tasks/common` 先例，
   新模块平铺在 `lan_mesh/` 下，`station_<域>.py` 命名，每文件带模块 docstring
   （编码规范第 9 条）。
6. **不改 DB 主键、不删端点、不改 .githooks、不 push**（本项目硬约束）。

## 3. 候选模块划分

### 3.1 目标文件布局

```
lan_mesh/
├── station_controller.py      # 保留: StationState 数据类 + StationController
│                              # 壳类 (class 定义 + __init__ 骨架 + mixin 继承)
├── station_lifecycle.py       # Mixin A: StationLifecycleMixin
├── station_selfheal.py        # Mixin B: StationSelfHealMixin
├── station_secretary.py       # Mixin C: StationSecretaryMixin
├── station_local_pm.py        # Mixin D: StationLocalPmMixin
├── station_pm_control.py      # Mixin E: StationPmControlMixin
├── station_scheduler.py       # Mixin F: StationSchedulerMixin (含联邦/扩容)
├── station_sync.py            # Mixin G: StationSyncMixin
└── station_hosts.py           # Mixin H: StationHostsMixin (发现/主机/Bot/修剪)
```

### 3.2 各 mixin 职责与方法清单（行号以 3253 行版为基准）

| Mixin | 职责 | 搬入方法 | 行数估算 |
|---|---|---|---|
| A. Lifecycle | 进程生命周期、端口、FastAPI 装配、WS 推流、dev-reload、自愈重启入口 | `__init__`(拆成 `_init_*` 片段, 见 §5)、`request_restart`、`_create_app`、`_find_available_port`、`start`、`stop`、`_dev_file_watcher`、`_dev_restart`、`_port_in_use`、`_ws_push_loop`、`_queue_ws_broadcast` | ~580 |
| B. SelfHeal | F4.2 自愈：动作执行/诊断/冷却熔断/守护循环/状态查询 | `run_heal_action`、`_heal_check_peer`、`_heal_probe_balances`、`_heal_rotate_key`、`_heal_switch_pool`、`_heal_guard`、`_auto_heal_once`、`_auto_heal_loop`、`get_auto_heal_status` | ~245 |
| C. Secretary | Secretary 激活/停用、模型资源预加载、选举/让位/查找/故障转移、过期任务恢复 | `_load_model_resources`、`activate_secretary`、`_find_resources_path`、`deactivate_secretary`、`_recover_stale_tasks`、`_secretary_election`、`_converge_mesh_token`、`_yield_secretary_to`、`_find_existing_secretary`、`_secretary_failover_check` | ~370 |
| D. LocalPM | 内嵌 PM：本机启动/停止/恢复/取消/暂停/注入、队列派发、子 Agent、孤儿迁移、周期汇报 | `_local_start_pm`、`_dispatch_queued_task`、`_local_stop_pm`、`_local_resume_pm`、`_local_cancel_pm`、`_local_pause_pm`、`_local_inject_input`、`_auto_attach_pm_thread`、`_local_pm_status`、`_local_create_subagent`、`_local_forward_progress`、`_local_execute_task`、`_start_local_pm_for_task`、`_migrate_orphaned_pms`、`_try_periodic_report` | ~410 |
| E. PmControl | PM 远程反向控制（Worker 上的 PM）+ 任务 DAG 图读写 | `inject_input_to_pm`、`cancel_task`、`pause_task`、`get_task_graph_data`、`update_task_graph` | ~240 |
| F. Scheduler | 任务提交、负载选站、联邦转发与轮询、自动扩容派发 | `submit_task_from_chat`、`_pick_task_host`、`_federation_forward_task`、`_federation_sync_peer`、`_federation_loop`、`_start_autoscaler`、`_autoscaler_loop`、`_autoscale_check`、`_is_worker_busy`、`_dispatch_task_to_worker`、`_next_pending_task`、`_dispatch_next_task_to_worker` | ~565 |
| G. Sync | 密钥/资源/配置/版本四类同步与对齐 | `_startup_sync_once`、`_startup_key_sync`、`_align_config_with_peers`、`_align_loop`、`pull_resource_secrets`、`_sync_with_new_peer`、`_check_version_leadership`、`_auto_upgrade`、`push_resource_secrets` | ~510 |
| H. Hosts | 发现包构造/设备发现回调、主机信息、配置刷新与下发、日志修剪、Bot 配置与命令 | `_collect_info`、`_make_packet`、`_on_device_seen`、`_deploy_config_script`、`_refresh_host_config`、`_config_refresh_loop`、`_prune_logs_if_due`、`_prune_loop`、`_load_bot_config`、`_on_bot_command` | ~235 |

拆分后每个文件 ≤600 行（含 docstring 与 import），函数长度门禁（80 行）不变；
`station_controller.py` 瘦身为 ~120 行壳（类定义 + `__init__` 骨架 + mixin 继承）。

## 4. 对外引用点清单（拆分时必须保持的契约面）

### 4.1 直接 import StationController 的位置

- `main.py:110` — 唯一生产入口
- `tests/test_core.py` — 16 处 `from lan_mesh.station_controller import StationController`
- AGENTS.md 验证命令（import 检查）

### 4.2 路由层经闭包访问的属性/方法（改名即破坏）

| 消费方 | 引用的 controller 成员 |
|---|---|
| `station_routes_basic.py` | `db` `state` `discovery` `station_director` `bot_gateway` `secretary_active` `secretary_host_id` `secretary_host_port` `_local_pm_agent` `_start_timestamp` `_collect_info`；方法：`request_restart` `run_heal_action` `get_auto_heal_status` `_auto_heal_once` `activate_secretary` `deactivate_secretary` `push_resource_secrets` `submit_task_from_chat` `_auto_upgrade` |
| `station_routes_tasks.py` | `db` `state` `discovery` `bot_gateway` `project_manager` `chat_runtime` `secretary_active` `_pm_worker_map` `_local_pm_agent`；方法：`_local_start_pm` `_local_resume_pm` `_local_stop_pm` `cancel_task` `pause_task` `get_task_graph_data` `update_task_graph` `inject_input_to_pm` |
| `station_routes_worker.py` | `cfg` `db` `state` `discovery` `_local_sub_agents`；方法：`_local_start_pm` `_local_stop_pm` `_local_cancel_pm` `_local_pause_pm` `_local_inject_input` `_local_create_subagent` `_local_forward_progress` `_local_execute_task` `_local_pm_status` |
| `station_routes_pm.py` | `db` `state` `bot_gateway` `_dispatch_queued_task` |
| `station_routes_projects.py` | `db` `state` `bot_gateway` `project_manager` `model_router` `mcp_gateway` `skill_registry` `skill_market` `secretary_active` |
| `station_routes_resources.py` | `db` `bot_gateway` `_align_config_with_peers` |
| `station_routes_chat.py` | `state` |
| `station_routes_common.py` | `secretary_active` `_converge_mesh_token` |
| `station_api.py` | `db` `state` `secretary_active` |
| `chat_handler.py`（self.controller.*） | `db` `model_router` `secretary_active`；方法：`activate_secretary` `deactivate_secretary` `submit_task_from_chat` `cancel_task` `pause_task` `inject_input_to_pm` `get_task_graph_data` `update_task_graph` |
| `bot_gateway.py` | `inject_input_to_pm` |

### 4.3 拆分契约结论

以上全部引用都是「属性名 + 方法名」，mixin 方案下名字不变、位置改变，
**引用方零改动**。唯一例外：`station_routes_common.py` 与 `station_api.py`
通过 `check_secretary(controller)` 只读 `secretary_active`，同样不受影响。

## 5. 渐进迁移步骤（每步可独立回滚）

### Phase 0 — 基线冻结（不做代码改动）
1. 确认 `loop_status.json` 当前无其他 Agent 占用 lan_mesh/；在 `AGENT_LOCKS.md`
   登记 `station_controller.py` + 新增 8 个文件的占用窗口。
2. 跑基线验证：`py_compile` 门禁命令 + `pytest -q` 全绿 + `scripts/sync_docs.py`。
3. 提交基线（`refactor(station): baseline before mixin split`），走 `scripts/sync_push.ps1`。

### Phase 1 — 空壳 mixin（行为零变化）
1. 新建 8 个 `station_*.py`，各含 mixin 类与模块 docstring，**方法体暂为空**。
2. `station_controller.py` 改为 `class StationController(MixinA, ..., MixinH):`，
   原 83 个方法保留在类内 → 类内方法优先于 mixin，行为不变。
3. 跑全部验证。此步验证「多继承壳」与门禁兼容性，失败成本最低。

### Phase 2 — 低风险块先行（B/H/G）
按 B SelfHeal → H Hosts → G Sync 顺序搬移：
1. 方法体从 `station_controller.py` 剪切到对应 mixin（保持 docstring/日志前缀/
   类型标注原样）；类内同名方法删除。
2. 顶部 import 跟随移动（如 `BotGateway` 移到 `station_hosts.py`；
   `error_tracker` 相关 import 移到 `station_selfheal.py`）。
3. 每搬一块跑 `py_compile` + import + `pytest -q`。

### Phase 3 — 业务块（E/F）
搬 E PmControl → F Scheduler（含联邦/扩容）。注意 F 内
`submit_task_from_chat` 201 行是超长函数，属既有事实，本次不强制拆分
（如需拆分列为后续独立任务，避免一次改动过大）。

### Phase 4 — 核心块（C/D）
搬 C Secretary → D LocalPM。此两块与路由层引用最密集
（§4.2 表），搬移后重点回归 `station_routes_tasks/worker/basic` 相关用例。

### Phase 5 — 生命周期收尾（A）
1. `__init__` 拆为 8 个 `_init_<域>()` 私有片段，按现有属性初始化顺序
   （db → shared_folder → station_director → skill_registry → bot_gateway →
   secretary 占位 → 同步/版本/自愈状态）在 `StationController.__init__` 中
   依次调用；属性声明顺序不变（`self._auto_heal_*` 等状态字典已在
   `__init__` 头部，注意别打乱对 `bot_gateway` 等的前置依赖）。
2. 搬 `start/stop/_create_app/_ws_push_loop/_dev_*` 到 `station_lifecycle.py`。
3. 全部验证 + 五节点真机冒烟（参照 iter-68 验证路径）。

### Phase 6 — 收尾与文档
1. `station_controller.py` 保留 `StationState` + 壳类 + re-export（如需要可
   `from .station_secretary import StationSecretaryMixin`，无需 `__all__`）。
2. 更新 `docs/design/02-station-core/README.md`（模块职责表新增 8 行）、
   `scripts/sync_docs.py` 的 MAPPING 登记新文件并 `--write`、AGENTS.md 模块
   职责表（若允许，需与另一 Agent 协调）。
3. 回写 `loop_status.json`（`current_phase` 转 completed、`iteration_count++`、
   notes 记录实测行数与测试数字），推送走 `scripts/sync_push.ps1`。

## 6. 风险与规避

| 风险 | 影响 | 规避 |
|---|---|---|
| mixin 间私有方法互调（如 `_auto_heal_once` 用 `self.bot_gateway`） | 跨 mixin 的 `_` 方法调用可行但阅读困难 | 仅通过 `self` 属性交互；必要时把纯函数逻辑抽到模块级函数（参照 `diagnose_records` 先例） |
| `__init__` 拆分顺序错误 | 属性未初始化即使用（如 `activate_secretary` 依赖 `bot_gateway`） | Phase 5 严格保持现有初始化顺序，先搬状态字典再搬组件 |
| 双 Agent 并行冲突 | 对方正在改 station_controller.py | 开工前查 `AGENT_LOCKS.md` 与 `loop_status.json.notes`；本方案要求独占该文件窗口 |
| 测试对类内部结构假设 | 部分单测可能依赖方法在类内定义 | `pytest -q` 全量回归兜底；失败的个别用例改为行为断言 |
| dev-reload 监控范围 | 新增文件自动纳入 `_dev_file_watcher` 扫描（`lan_mesh/*.py`），无需改代码 | 无需处理 |

## 7. 收益预估

- `station_controller.py` 152KB → ~120 行壳 + 8 个 ≤600 行模块，单文件 review
  成本下降一个数量级。
- git 冲突面按职责域隔离：前端 Agent 改 dashboard.html、后端 Agent 改
  station_scheduler.py 不再互相撞车。
- 新职责有明确归属文件，杜绝继续往大文件堆积。

## 8. 明确不做

- 不修改任何 DB 结构、API 端点路径、`.githooks/`、`VERSION.json`。
- 不重命名任何对外属性/方法（§4.2 契约面）。
- 不在本次拆分中重构 `submit_task_from_chat` 等超长函数（独立任务另行排期）。
- 本方案为提案稿：执行拆分须由后续迭代在确认无占用后发起。
