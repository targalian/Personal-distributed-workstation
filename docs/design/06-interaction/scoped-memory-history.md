# DS-Bot P1-B：有来源的作用域记忆与历史检索

2026-11-12，iter-116，工作区候选版本0.41.4。参考DS-Bot固定提交 `405ee8bbf5c6cbe805340c1dc69186863bfb5939` 的来源锚点、分层记忆与可重建历史索引设计，使用本项目YomkServerPy服务和SQLite实现，没有复制上游代码。

## 用户可见行为

- 秘书回复自动读取当前项目和当前会话的有效记忆，附原消息锚点与修订号。每次读取都检查期限、撤销状态和来源是否仍存在；参考数据上限5000字符。
- 新增历史搜索API可查询完整JSONL记录，不受最近20条prompt或100条内存历史裁剪限制。中文一、二字查询与没有FTS5的SQLite使用字面子串匹配；较长查询使用FTS5 trigram。全角字符与大小写规范化，百分号、下划线和引号作为字面字符处理。
- 搜索返回片段、原文字符数、`anchor`和`next_before_seq`；原文通过锚点分页读取，单页最多16000字符。记录完整文本不因搜索或prompt预算而截断。
- 记忆只能经显式管理API新增、更正和撤销；不会从网页、工具结果或模型回复自动提取、自动批准。来源必须是对应会话或PM线程内的用户原消息，且请求中明确提供布尔值 `confirmed: true`。
- 改动保留修订历史；更正/撤销必须携带 `expected_revision`。并发写入旧版本返回409，撤销条目不可恢复，需有有效来源并明确新建。

## 模块与数据所有权

`MemoryApiService` 是Station管理入口，检查Secretary状态并经 `call_component` 调用ChatHandler所属的 `MemoryService`。后者复用ChatHandler的可重入锁，序列化会话变更、索引同步和记忆读写；SQLite连接仍由Database线程本地连接提供。未新增后台任务、LLM调用或独立线程池。

`history_index.py` 只读取已登记的会话和唯一绑定的PM线程JSONL。路径限制在conversations目录，拒绝目录穿越和指向目录外的符号链接。按文件身份、长度和修改时间判断增量更新，重建时强制重读；文件读取过程中发生变化返回错误供重试。逐行隔离损坏JSON/UTF-8，保留之后的有效行，并在重建报告中统计损坏行数。

`HistoryRepository` 持有可丢弃的索引，`MemoryRepository` 持有权威记忆与审计记录。schema v14新增：

| 表 | 作用 |
|---|---|
| `context_streams` | project/conv/agent范围和文件索引进度 |
| `context_messages` | 原消息完整文本、顺序、角色及不可变锚点 |
| `context_search`及FTS内部表 | 可选trigram加速索引；不可用时降级字面搜索 |
| `context_memories` | 当前内容、范围、有效期限、状态、版本、来源锚点及持久来源流身份 |
| `context_memory_revisions` | 每次变更的完整快照，与当前版本在同一事务内写入 |

原有`chat_history`和`task_memory`统计表、主键、方法及API保持兼容，不把旧全局聊天缓存自动导入有范围的记忆。历史来源以JSONL为准，不能仅凭索引条目提升为权限或执行授权。锚点由完整范围、原消息序号/角色/内容/时间/消息ID计算；老JSONL没有消息ID也可稳定重建，新消息额外写入UUID。来源变更、移走或删除时，旧锚点不能静默绑定新内容。

## 作用域规则

所有查询必须指定有效 `conv_id`；项目ID从会话元数据解析，不接受请求伪造项目范围。空项目ID不是通配符。

| 记忆scope | 写入来源 | 可见范围 |
|---|---|---|
| `project` | 归属项目的普通会话用户消息 | 该项目的会话和所属PM线程 |
| `conversation` | 普通会话用户消息 | 该会话及所属PM线程 |
| `agent` | 该会话唯一绑定PM线程的用户消息 | 该会话下的该PM线程 |

PM线程不能向project/conversation范围写入、修改或撤销条目。Agent以现有PM线程ID作为稳定范围标识，非任意Worker或子Agent的全局身份。一个PM线程绑定多个会话时，新接口拒绝该线程的访问，避免同一日志映射进不同项目。无项目会话只能创建会话记忆。

这里是业务作用域隔离，沿用已有mesh认证和可信工作站管理边界，不新增多用户身份ACL。`confirmed`是管理接口的明确确认要求，不是独立身份凭据。暂未开放模型可调用的记忆写工具，也未新增前端管理面板；PM/子Agent执行prompt的自动记忆分发不在本轮范围。

## API

全部路由在Station装配，继续经过原认证中间件；Secretary停用时返回503。不存在/越界返回404，无效输入返回400，修订冲突返回409。旧会话API路径未修改。

| 方法 | 路径 | 参数/用途 |
|---|---|---|
| GET | `/api/conversations/{conv_id}/history/search` | `query` 1–128字符、可选`agent_id/limit/before_seq` |
| GET | `/api/conversations/{conv_id}/history/sources/{anchor}` | 可选`agent_id/offset/limit`，读取原文 |
| POST | `/api/history/rebuild-index` | 重建所有已登记历史流，不修改JSONL或记忆审计 |
| GET | `/api/conversations/{conv_id}/memories` | 可选`agent_id/include_inactive`，包含有效状态及来源会话 |
| POST | `/api/conversations/{conv_id}/memories` | 明确新增记忆 |
| PATCH | `/api/conversations/{conv_id}/memories/{memory_id}` | `correct`或`revoke`，带期望版本 |
| GET | `/api/conversations/{conv_id}/memories/{memory_id}/revisions` | 查询当前可见范围内的修订快照 |

先搜索获取真实`anchor`，再提交例如：

```json
{
  "confirmed": true,
  "scope": "conversation",
  "content": "本项目的说明文档使用中文",
  "source_anchor": "<搜索返回的msg-锚点>",
  "expires_at": 0
}
```

更正请求使用 `operation: "correct"`，增加 `expected_revision`，并完整提供新 `content/source_anchor/expires_at`；省略expires_at表示不限期。撤销只需 `operation: "revoke"`、`confirmed: true`、`expected_revision`。期限为非负有限Unix秒数，0表示无期限，过去时间立即失效。内容上限1000字符，常见凭据形态拒绝写入；这不是对任意秘密格式的完整识别保证。

## 撤销、删除和恢复语义

- 自动prompt召回不会从历史搜索再次带回曾作为记忆来源的记录，避免已更正、撤销或过期内容绕路回流。显式历史检索仍能查到原记录；近期原始对话按原有聊天规则保留。
- 清空会话、删除会话、删除/解绑PM线程前，先按记忆内持久记录的来源流身份撤销有效记忆（不依赖来源文件或派生索引仍存在）；恢复旧JSONL或重建索引不会恢复已撤销条目。审计快照继续保留，撤销/清空不是物理擦除审计数据。
- 文件暂不可读时，新API显式失败；秘书回复记录检索异常并继续普通对话，不注入缓存的未知有效性记忆。
- 索引可独立重建，但完整恢复仍需JSONL、会话index.json和SQLite记忆两张权威表。重建不会清空记忆修订记录。
- 记忆当前存于Secretary本机SQLite，尚未增加跨主机复制/故障切换同步。共享JSONL可在新节点重建搜索索引，不能据此宣称记忆日志已跨站同步。一个共享历史目录应有一个写入主控；没有实现跨进程文件锁或大型日志流式分段索引。

## 验证记录

专项行为覆盖中文/特殊字符检索、原文分页、FTS缺失降级、损坏行隔离、索引重建保留原文/记忆、项目/会话/Agent隔离、非用户来源拒绝、明确确认、过期/更正/撤销、并发修订与审计失败回滚、API认证与Secretary状态、历史内容不进入意图授权、目录越界和v13兼容升级。完整实测结果随本轮状态文件回写；真实在线模型回复质量及多机记忆同步不属于本轮已验证能力。

实测：新增37项行为测试；Python3.12全量 **749 passed / 2 skipped / 7 warnings，117.17s**。跳过项为缺少resources.yaml和Node.js的环境项，告警为已有框架弃用提示。编译、StationController导入、全生产模块未定义名称扫描、依赖清单与docs221项11域一致性通过。

收尾故障补强：来源文件与索引同时缺失后显式清空，恢复原文件仍不恢复记忆；PM重复绑定时允许安全解绑，解除冲突后原范围可重新检索。

最终门禁：以临时Git索引纳入全部新文件后，**0 Blocker / 16既有Warning**，未修改实际暂存区。门禁仍以尚未推进的gitee/master为基线，因此含前置已同步GitHub内容的原有提醒；本轮7个新生产模块的docstring、公共类型标注及函数长度专项均通过。


## iter121：管理界面、PM消费与大日志索引

Web“记忆与历史”及React `#/memories` 共用静态页面：按会话/PM线程查找原文，分页读取来源，显式勾选确认后创建/更正，支持项目、对话、Agent范围、有效期、撤销、修订记录与失效过滤。修改采用expected_revision防止覆盖他人修订。动态文字使用textContent；viewer不可写，后端继续使用已有权限中间件。

`GET /api/tasks/{task_id}/memory-context?pm_id=…` 由DB任务当前PM、唯一会话绑定及项目一致性确定范围。未绑定、重复绑定、项目不一致不提供记忆；请求不能指定其他conv_id或project_id。PM规划及每次LLM技能/ReAct执行开始重新获取当前有效记忆（最多12条、5000字符），不使用持久缓存正文。子任务只继承task/PM标识，引用携带来源与修订号，明确不是本轮授权。Secretary不可达时省略参考；执行中途已进入模型上下文的引用不会被追溯移除，撤销在下一次读取生效。CLI外部工具不自动读取这些记忆。派发早于绑定时仅在404/409响应后短暂重试；缺失范围则跳过参考，不跨范围回退。来源会话在任务创建时由当前会话或周期计划归属记录，后续切换活跃对话不会改变任务所属线程。

中文JSONL索引改为每批500条，物理行号和来源锚点保持；扫描后的文件指纹不一致或任一批失败，SQLite与FTS整条流事务回滚，旧索引继续可用。损坏行计数，后续正常行仍入索引。变更文件仍做完整扫描（O(n) IO）；仅索引过程内存有界，单条超大JSON行、旧聊天缓存行为不因此变为有界。原JSONL始终是权威来源，不因索引而改写。

本轮不包含跨站复制，下一轮通过显式私有备份/恢复完成，不能将本机测试写为物理多机验收。

本轮实测：12新增专项、全量832 passed/1 skipped（148.76s）；16,000条、19MB以上JSONL索引峰值小于8MB断言通过；Chrome七组、TS/Vite通过。

## iter122：跨站记忆后续完成
通过私有快照显式复制及手工故障恢复，保留项目内原文与修订，不自动多主合并或启动历史任务。页面支持干净备用实例恢复。详见 [私有记忆备份](../07-data-sync/private-memory-backups.md)。
