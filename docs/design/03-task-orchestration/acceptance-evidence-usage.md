# 验收证据与按尝试记账

iter-118 / DS-Bot P1-D，2026-11-12。沿用 PMPlanner、Yomk 组件与既有资源账本，不将模型自评当作测试执行结果。

## 验收绑定

项目蓝图的全部非空验收标准进入审查，不再只取前五/六条。解析时只接受 JSON 布尔 true；字符串 "false"、序号不对应、缺项都不能算达成。模型不可用或审查内容超过32000字符时仍返回全量标准，并明确未确认。

`AcceptanceEvidenceService`从 PM 已接收的子任务输出中收集当前 task/project 的 ReAct 产物。先按执行范围读取磁盘并验证 SHA-256，只接受 tool_result；不存在、损坏、跨范围或只存在远端的记录不会成为“已校验”引用。目录最多包含200条、每条预览1200字符，模型只能引用目录里存在的ID。

每条检查新增：

- `assessment_kind=model_self_review`：原 met/verdict 仍是模型自评，保留旧页面兼容。
- `source_refs`：完整交付物的不可变来源，以及模型选择的有效工具观察引用。正常写入 `.runtime_artifacts`，没有存储能力时随 delivery 内嵌完整原文及SHA-256。
- `evidence_verified`：是否引用了校验通过的原始观察，仅说明来源可核对。
- `verification_status=observations_linked|unverified`：是否存在这种观察。

整体 `independent_verification=not_performed` 明确未独立执行验收测试。工具返回“5 passed”可以作为观察，不能自动证明它对应当前所有验收条件。交付物 prompt 仍只展示前4000字符；保存完整来源使操作者能继续核对未展示部分。验收报告保持在原交付结果中持久化，沿用既有 API。

## 调用与尝试身份

每个真实模型 HTTP 调用创建新 usage_id；同一任务尝试中多轮 ReAct、供应商重试与辅助调用分别计费。重复上报同一个调用只入账一次，不能用 attempt_id 把整个尝试中的多次收费合并成一笔。

`runtime_usage` 在调用 finally 中记账，覆盖正常、取消、无效响应和网络失败。响应已经报告的用量即使随后解析/流连接失败也保留；没有报告用量时计数暂为0且 `usage_known=false`，不能解释为供应商免费。流式响应不再用字符数猜测 token；中途连接断开或达到超时不能报告完整成功。

任务执行注入 attempt 范围；规划、质量检查、结果聚合、验收审查通过 `call_for_task` 注入 task/project/purpose，结束时恢复外层线程归因。旧信封无attempt时生成本次执行身份，不承诺跨进程恢复后延续同一身份。模型上下文预算跳过的候选没有HTTP调用，因此不会产生收费记录。

schema v15增加 `usage_call_details` 与 attempt 索引，保留原 resource_usage_log 所有列和主键。资源记录和调用元数据同事务写入，唯一 usage_id 下的并发重报只计一次。相同ID却改变模型、token或任务范围会被拒绝。旧 API、数据库和第三方调用仍可省略元数据；老记录保留原统计行为。

## 上报与查询

HTTP/WS 批量上报携带 `usage_metadata`；服务端返回 `accepted_ids`，发送方只推进已明确接收的记录。部分失败仍留本机待重试。旧服务端不支持元数据确认时，新记录继续保留，避免将归因字段静默丢失；应先升级 Secretary 再升级 Worker。HTTP沿用配置的mesh token认证。

新增 `GET /api/resources/usage/attempts?task_id=...&project_id=...&limit=100`，由 `UsageAuditService` 返回 attempt/purpose/outcome 的调用数、报告token、报告费用及未知用量调用数。必须提供任务及项目范围；Secretary检查、节点认证/角色策略沿用现有入口。既有cost接口保持兼容。

RuntimeTrace JSONL记录相同usage_id/attempt/purpose/outcome/usage_known，便于关联。实际用量仍以供应商报告为准，资源池费用使用本地配置价格。未配置资源池的模型仍按原策略不计资源额度；CLI外部计费、供应商账单补录及进程在网络调用后/入账前硬崩溃造成的未知成本，需要供应商账单对账，不能据此宣称金融级 exactly-once。

## 验证

新增行为覆盖正常/取消/失败调用、已知/未知用量、断流、真实重试与传输重报的区别、并发SQLite去重和重开、四类辅助调用归因、HTTP/WS部分确认、范围查询、全量验收条件、缺项/类型防误判、来源校验及自评标识。在线模型、物理节点和供应商实际账单不在本机验证结论内。

实测：15项新增专项；Python 3.12全量790 passed、2 skipped、128.13秒；编译/导入/名称扫描/docs清单通过。在线供应商账单与真实跨机环境仍未验收。
