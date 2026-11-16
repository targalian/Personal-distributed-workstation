# 协作完成通知与证据边界

状态接口返回独立 execution、delivery、acceptance、read 维度，以及优先 code/label。
执行 completed 不证明结果送达；没有可靠最终交付记录时显示 undelivered（结果未送达）。
failed、cancelled、awaiting_input、review_required、interrupted 分别展示。
review_required 与澄清信息读取 PM 真实 state_json，快照 phase 支持 interrupted。

最终 PM deliver API 校验任务存在；任务已绑定 PM 时拒绝其他 PM 的结果，历史无绑定调用保持兼容但不作为可核对的入站证据。当前 PM 的 `_delivery` 包含
交付时间及 deliverable 时构成 station_received 证据，不代表远端 ACK、用户已读
或验收通过。人工 accepted 与模型 acceptance_review 独立；重复相同交付保留人工验收。
历史交付没有可核验的当前 PM 归属时不作为入站证据。

subtask_transport 单列 mesh_delivery_outbox/inbox 数据。只收录 payload.parent_task_id
精确匹配本任务，且 subtask_id/attempt_id 与快照 active_attempts 完整 payload 相等的记录。
不以 mesh_task_executions 的执行完成推断最终交付。当前源码 v13 执行表存在
execution_id/task_id/status，但它们不是最终任务交付的证据。

GET overview 不生成通知，不执行 schema 迁移或 commit。保持原有 v16 schema；已读回执以现有通知表中的独立内部事件保存，兼容旧版已迁移数据库，无需 ALTER 或 GET 中迁移。
read 为 Station 共享身份的通知已读状态，不声称用户阅读了交付物全文。
POST /api/collaboration/{task_id}/read 沿用统一权限中间件：viewer 禁写，operator 可写。

save_task、update_task_status、PM 状态写入、最终交付路径触发通知。
按任务与通道持久记录连续状态，连续重复不刷新，离开后再进入同状态生成新通知。
业务已提交后通知异常只记录日志；最终交付广播或 Bot 失败不令入库交付返回 500。
前端使用 textContent，保留回复输入及错误显示。

## 验证与限制

新增真实 Database 行为用例 18 项（含参数化），不自建替代生产 schema。
18 项全部实际通过，包含 HTTP overview、真实最终交付路由及 viewer/operator 权限；连同既有协作、服务和基准专项共 86 passed。
Chrome桌面1280/移动390各14项行为断言通过，使用API替身；HTTP后端单独验证。Python编译通过，未部署、未调用真实模型或物理多机。
同 PM 内最终任务的历史重跑缺少生产协议级最终 attempt 标识，不能据此证明远端
最终交付 ACK；当前接口只报告 Station 本地持有的交付记录。

普通运行/排队等状态只保留内部去重事件，不在用户通知列表刷屏；终态、等待输入和待核对才显示执行通知。交付事件以内容SHA标识去重，不把完整交付物复制进事件ID。通知已读为共享身份，不是个人读回执。
