# 持久跨站投递与结果确认（DS-Bot P1-A / iter-115）

实现参考 [DS-Bot 对照路线](../dsbot-optimization-assessment.md)，服务名为
`DurableDeliveryService`，模块为 `mesh_delivery_service.py`。既有
`delivery_service.py` 继续负责最终交付物上报，两者职责独立。

## 行为与兼容

新 Station/Worker 的 `/pm/create-subagent` 返回 `delivery_protocol: 1`。
绑定主机 owner 的 PM 看到该声明后，将派发消息先写入本机 outbox，由后台线程投递。
请求失败、断网、HTTP 200 但 `ok/ack_id/ack_sha256` 不匹配均保留重试；重发间隔
2～60 秒，HTTP 超时 10 秒。租约 30 秒，发送进程崩溃后租约到期可重新领取。
旧节点未声明 v1 时继续走原 `/tasks/execute`，该兼容路径不具备新协议的持久性。

新增端点由 Station/Worker 的相同认证中间件保护，无白名单例外：

| 端点 | 方法 | 语义 |
|---|---|---|
| `/mesh-delivery/task` | POST | 校验并保存任务收据后确认；后台才执行 |
| `/mesh-delivery/result` | POST | 保存完整结果收件后确认；PM 未恢复时保留待应用 |
| `/mesh-delivery/cancel` | POST | 持久化取消，即使取消先于任务到达也不执行 |
| `/mesh-delivery/status` | GET | 返回 outbox/inbox/execution 各状态数量，不返回任务正文或密钥 |

结果直接回到发起 PM 所在主机，`reply_url` 根据目标网段选择本机可达 IPv4，
本机目标使用 loopback。Secretary 地址仍用于其他进度、团队和交付接口；不把
Secretary 的“当前本机 PM”误当成远程任务的 PM。尚不支持地址变化后的自动迁移。

## 身份与幂等边界

`parent_task_id` 是父任务，`subtask_id` 是逻辑子任务，`attempt_id` 是一次执行尝试，
`result_id` 根据 attempt 确定生成。网络重发及断点恢复复用原尝试；主动重试生成
新尝试，保留逻辑子任务 ID。规范 JSON 的 UTF-8 SHA-256 覆盖完整 envelope；
传输校验用于发现内容不一致，身份认证仍由原 mesh 中间件承担。

同一执行 ID 携带不同输入会被拒绝。执行收据通过 SQLite 唯一键及条件更新领取，
重复请求不再次运行。相同子 Agent 的多个尝试不能同时领取执行，避免重试共享
一个 Runtime 并发调用。创建相同 `preferred_agent_id` 时复用已有 Runtime。

PM 在应用结果前校验 PM、父任务、逻辑子任务和当前尝试，忽略旧尝试和旧式无
作用域回调。快照保存 received_results/settled_attempts，一次尝试最多应用一个
终态结果。PM 状态与已处理的收件标记在同一事务提交。取消先落本机 checkpoint，
旧派发标记 superseded；持久取消另外重试。已取消 PM 不接收迟到结果，聚合等待
模型返回后再次检查取消，不会继续交付。

## 存储、恢复与关闭

Schema v13 只新增四张 `mesh_*` 表和两个索引；原 v12 表/索引契约不变。
Station 使用现有 DB；独立 Worker 使用 `data_dir/worker-delivery.sqlite3`。
同一 journal 由一个主控进程拥有，不支持多个 Worker 进程共用同一 journal。
普通 `DeliveryRepository` 通过 Database `_get_conn()` 获取线程本地连接，业务
由 owner 的 Yomk 组件调用。后台投递/执行/结果应用线程登记在 owner `_threads`，
共用停止信号，关闭先取消并等待线程，再回收服务；停机后未确认记录留到下次启动。

| 状态 | 恢复行为 |
|---|---|
| outbox pending / sending 租约过期 | 原 ID 重发，只有匹配确认才 acked |
| execution queued | 重启后继续领取执行 |
| execution started | 标为 interrupted，保存失败结果并回传，不自动重跑副作用 |
| execution cancel_requested | 重启后保存 cancelled 结果，不执行 |
| execution completed / failed / cancelled | 保留结果收据，重复任务只确认 |
| inbox received | 等待同一个 PM 恢复，再校验和应用 |
| inbox applying 时崩溃或处理异常 | 标为 review_required，禁止自动重复 PM 副作用 |

执行结果与结果 outbox 在一个事务落盘，断在两者之间会整体回滚。输出、用量、
错误与 SHA-256 随完整结果保存，网络投递不使用 ReAct 的上下文截断规则。
PM 本地 checkpoint 优先于远端较旧快照；已终结 checkpoint 禁止恢复为运行态。

这提供持久收件、至少一次传输和已完成尝试的去重，**不保证任意 shell、外部 API、
模型调用的全局 exactly-once**。执行开始后进程崩溃、PM 收件已领取但副作用与
checkpoint 之间崩溃，都必须人工核对。前者进入 PM `review_required`，禁止自动
重试及聚合；后者在诊断端点显示 review_required。当前可核对产物后取消原任务并
明确派发新任务；专门的逐条核对/补偿 UI 尚未实现。

账本正文和去重收据默认持续保留，无隐式过期或启动时删除。长结果会占用本机
磁盘；停机备份/归档需同时保留 journal 和相关 PM checkpoint。暂不提供自动
压缩、分页下载、跨主机 journal 复制或垃圾回收，以免提前删除幂等依据。

## 验证边界

`tests/test_mesh_delivery.py` 验证并发重复任务、租约恢复、校验/确认错误、事务
回滚、取消栅栏、旧尝试隔离、恢复复用 ID、应用中断待核对、真实子进程
`os._exit` 后 queued/started 两条恢复路径，以及两套独立 SQLite + 实际 HTTP
服务的断网、重启、300,000 字符 Unicode 长结果完整回传与重复确认。

上述为本机隔离实例/进程验收。至少两个真实隔离节点、真实在线模型、长时网络
分区、地址变化及部署升级/回滚仍待环境；当前源码候选未部署或发布。

本轮 Python 3.12 全量 **712 passed / 2 skipped / 102.19s**，新增投递测试 36 项。
两项跳过分别为缺 resources.yaml 与缺 Node.js；7 条警告来自既有 Starlette/FastAPI
弃用接口（Worker 新启动回调沿用现有 on_event 模式）。编译、StationController 导入
和未定义名称扫描通过。Schema 契约测试保留 v12 原表/索引逐项比对，只允许 v13
新增 journal，不重写旧 golden fixture。

设计清单同步 213 条目/11 域、依赖清单一致性及 pre-push 门禁通过（0 Blocker，
7 项既有 Warning：历史超长函数、类型缺注和 sync_docs 的输出前缀）。新四个
生产模块的 docstring、公共类型标注和函数长度另行检查通过。
