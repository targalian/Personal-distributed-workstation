# DS-Bot 对照研究与 LAN Mesh 优化

研究日期：2026-11-12；迭代：iter-114。

更新至iter-123：P1/P2既定开发范围已完成，工作区候选为0.41.11。下文“本轮”和对照表状态记录iter-114研究时点；最终实现、测试证据及保留的实机验收见[连续迭代交付清单](dsbot-loop-completion.md)。

## 结论与研究范围

参考项目是 [Fei2411/DS-Bot](https://github.com/Fei2411/DS-Bot)。本次获取了包含 846 个文件的源码归档，并重点审阅下表列出的模块和相关测试；不是对全部文件逐行审计，也未运行 DS-Bot 桌面端或连接真实模型。归档的 Git 注释和 GitHub tree API 均指向 `405ee8bbf5c6cbe805340c1dc69186863bfb5939`，`ds-bot/package.json` 为 `0.3.5`。归档 SHA-256：`59ba1d46d173431f1c4960557394736ab02f77c0e5abdab375744044783218fc`。以下源码链接固定到该提交，避免主分支变动后无法复核。

DS-Bot 最值得学习的是长任务连续性：明确区分用户要求、工作进展、待办和交付证据，将记忆、会话、任务交接和用量记录连接起来。它还把能力启用、可见范围和实际执行权限分别管理。LAN Mesh 已有异构主机调度、模型路由、PM 状态恢复、DAG 与蓝图验收，适合借鉴这些机制补齐运行细节。

本轮认领一个路线图任务：**执行可靠性与上下文管理**。已实现内容见下一节。长期记忆、消息投递账本、群组交互等涉及独立数据模型和验收，列入后续路线，没有将它们标为已实现。既有 iter-113 YomkGraph 改动保留；没有更换框架、运行上游安装脚本、复制上游代码、部署或重启线上节点。

## 功能对照与取舍

| 方向 | DS-Bot 的机制与优势（源码依据） | LAN Mesh 现状与优化方向 | 本轮状态 |
|---|---|---|---|
| 长任务上下文 | [steward.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/steward.js) 按模型窗口扣除回复预算，设置检查线和硬限制；区分继续压缩与新建会话部分，避免中途误收尾 | `runtime_react_service.py` 原先每轮重复发送完整工具输出；先加入确定性字符预算，再考虑模型窗口与摘要检查点 | 已加入预算与明确停止原因；未实现语义摘要 |
| 原始要求保留 | 同模块 `CHECKPOINT_PROMPT`、`checkpointText` 保留用户输入与精确标识；[parts.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/parts.js) 管理会话分段与交接 | 已有蓝图注入；本轮保留 system、原始 requirement、assistant 调用参数与所有调用配对，只削减工具观察结果 | 已实现；需求本身超预算明确失败 |
| 长期记忆 | [memory.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/memory.js) 分 Bot/群组/团队，条目有来源，变更有 journal；冻结每部分的前缀，后续变更以增量输入，兼顾缓存 | `task_memory` 目前偏任务成败与模式统计，尚非带来源的用户偏好/决策记忆；应新增作用域和修订记录，不复用统计表冒充长期记忆 | 后续 P1 |
| 记忆更新与防污染 | [memory-review.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/memory-review.js) 增量处理新对话、检查写入作用域；[memory-entries.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/memory-entries.js) 负责条目清理与敏感值判断 | 将用户明确陈述和工具内容分开，保存来源、期限和撤销信息；自动提取只能生成候选，需独立预算和污染测试 | 后续 P1 |
| 历史检索 | [chat-index.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/chat-index.js) SQLite FTS5/trigram 搜索，返回 part/seq 锚点，可再读取上下文 | `ChatHistoryService` 已有 JSONL 会话，`ChatContextService` 最近 20 条拼接；应提供按会话权限过滤的检索和消息锚点，支持中文短查询回退 | 后续 P1；本轮未改变秘书会话历史 |
| 任务交接 | [handoffs.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/handoffs.js) 明确父子工作与待回结果；长结果先原子落文件再保存引用，缺失结果标为不完整；迟到结果不二次结算 | PM 已有 snapshot 和依赖派发，但跨站回传需要完整的持久投递、幂等确认协议；应将任务、尝试和结果 ID 分开 | 后续 P1 |
| 消息持久化 | [ledger.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/ledger.js) 每会话有追加账本，写入按序排队；处理半行、写失败回滚与再次投递 | `BotDeliveryService._pending_queue` 在内存；不能直接把单进程 JSONL 设计当作跨主机一致性协议，宜用本地 SQLite outbox/inbox 与确认去重 | 后续 P1 |
| 多 Agent 协作 | [groups.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/groups.js) 群组轮次独立管理，管理员调度与成员工作分开，成员无新信息可不发言 | LAN Mesh 已有 Secretary/PM/Worker 分层及 L1/L2 对话；可补任务参与者视图、定向邀请与避免无意义广播，不需要另起一套群聊执行器 | 后续 P2 |
| 完成证据 | [goals.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/goals.js) `passGoal` 要求 evidence；`completeGoal` 拒绝未通过阶段 | LAN Mesh 蓝图验收已有基础；本轮先修“失败仍被当作 completed”的底层漏洞，后续把每条验收与产物/测试证据绑定 | 底层终态已修；证据索引后续 P1 |
| 技能与 MCP | [capabilities.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/capabilities.js) skill/MCP/pack 库与每 Agent 的启用集合分离，各 Agent 独立串行协调挂载，防止一个慢服务拖住全队 | 缓存技能兜底曾把所有正文拼入 prompt；改为当前技能/显式 activation 规则加载正文，其他技能只列入口；保持现有 Yomk ComponentService | 已实现提示词按需加载；未改变工具授权 |
| 模型与本地 CLI | [local-agents/](https://github.com/Fei2411/DS-Bot/tree/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/local-agents) 将多种本地 Agent 适配到同一协作系统；[handoff-note-budget.test.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/test/handoff-note-budget.test.js) 覆盖输出截断和回退预算 | 已有 CLI 安装/停用/路径检查与多模型路由；本轮去除 fallback 重复模型，透传模型错误、识别 ReAct 的 length/content_filter，保留取消时消耗 | 已实现 API 路径修复；CLI 适配保持原状 |
| 权限与秘密 | [permissions.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/permissions.js)、[privacy.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/privacy.js) 分会话权限与隐私范围；[secrets.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/secrets.js) 用秘密引用与输出遮罩减少直接暴露 | LAN Mesh 已有鉴权、密钥加密分发、CLI 根目录护栏；后续统一工具/会话/记忆作用域与遮罩；私有 Bot 路径约束也不是 OS 沙箱 | 后续 P1；本轮未放宽权限 |
| 浏览器/桌面交互 | [browser/gate.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/browser/gate.js)、[browser/script-child.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/browser/script-child.js) 将动作许可和脚本进程分离，产品支持人工接管 | 当前工作站以任务执行与管理界面为主；适合作为独立能力插件，需验证会话隔离、暂停接管与执行主机归属，不能只加一个无限制浏览器工具 | 后续 P2 |
| 调度与消息入口 | [schedules.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/schedules.js) 调度携带时区/投递对象，会话更换时迁移任务，并限制只能操作所属会话的调度 | 现有任务队列、自愈和 Telegram/企微可复用；周期任务宜有 owner、timezone、next_run 和补跑策略，入口统一到 ChatRequest | 后续 P2 |
| 成本与可观测性 | [usage.js](https://github.com/Fei2411/DS-Bot/blob/405ee8bbf5c6cbe805340c1dc69186863bfb5939/ds-bot/src/host/usage.js) 按付费尝试记账，含压缩/交接等辅助调用，避免只看最终答复；会话继承避免重复计费 | LAN Mesh 已有资源记账、任务归因和 YomkGraph；本轮保留失败/取消用量，返回上下文裁剪统计，后续补 attempt_id 与辅助调用细分 | 已补运行结果统计；后续 P1 |

## 本轮已经实现

### 1. 限制 ReAct 请求体，保留任务要求

新增 `runtime_context.ReactContextBudget`，每次执行独立创建，不向 YomkServer 添加全局单例，不改服务寻址。`RuntimeReactService` 在每次模型调用前进行预算校验。

| input_data 字段 | 默认值 | 可用范围 | 意义 |
|---|---:|---:|---|
| `context_max_chars` | 64000 | 4096..256000 | `messages` 与 `tools` JSON 序列化后的字符预算 |
| `tool_result_max_chars` | 12000 | 512..32000 | 单次工具结果进入上下文的字符上限 |
| `max_iterations` | 30 | 1..100 | ReAct 模型轮次上限；非法值使用默认值 |

工具结果保留开头、结尾和显式裁剪提示。总预算不足时，从较旧的工具结果开始缩减到 256 字符；不删除 tool-call/result 配对，不改原始需求、系统规则、蓝图或工具参数。仍无法容纳时返回 `context_limit`，让 PM 收到真实失败并拆分任务。统计在 `output.context` 返回：预算、发生裁剪的次数、移除字符数、候选请求最大字符数。一次输出可能被先限幅、后缩减，因此次数不等于独立工具调用数。

这是保守的字符预算，**不是 tokenizer 计数，也不保证所有供应商上下文窗口都足够**；默认应结合实际模型调整。它不是语义压缩，没有自动摘要或跨重启恢复 ReAct 会话。被裁剪的工具结果不自动归档；对于无法重现的工具结果，仍需后续独立产物存储。当前使用显式缺失提示，避免模型误以为看到完整结果。

验证期间还补齐了 `ResourceConfigService` 的 `Database` 类型检查导入；该修正不引入运行时循环依赖或改变业务行为。

### 2. 分段读取长文件

`file_read` 增加可选 `offset`、`limit`，单位为 Unicode 字符，返回 `next_offset`、`truncated`。不传新参数时保持旧返回结构和完整读取行为。分页范围为 offset >= 0、limit 1..32000；工具说明建议每页 4000 字符，按返回的 next_offset 继续。文件必须在分页过程中保持稳定，才能顺序拼回原文。

分页通过顺序读取跳过前缀，内存不随 offset 增长；深分页仍有 O(offset) I/O。不是基于字节的 seek，也没有更改文件工具的权限边界。极端转义内容可能使 JSON 后的单页结果再被裁剪，此时应缩小 limit。

### 3. 失败与取消贯穿执行链

`infer_result_status` 增加 `max_iterations_reached`、`_error`、`timed_out`、非零 `returncode` 识别。`RuntimeSkillService` 的代码生成/审查/摘要保留底层模型失败信息；`ToolExecutionService` 对这些结构化失败返回 `isError=true`。这些状态从 handler → RuntimeExecutionService → Worker/PM 传递，成功结果结构保持兼容。

ReAct 的模型链耗尽、空最终回复、输出截断/过滤、轮次耗尽不再伪装为正常完成。fallback 模型去重；工具 LLM HTTP 超时按仓库约定设为 10 秒。取消在模型返回后、每个工具调用前后检查；已消费的 token 保留到最终 usage。10 秒超时对慢模型比旧的 120 秒更严格，需上线前用目标模型验证。

### 4. 终止无效错误循环，禁止猜测工具参数

工具参数 JSON 无效或不是对象时，返回可供模型修正的工具错误，不以 `{}` 默认参数真的执行工具。只有连续三轮**整批工具均失败，且工具名、规范化参数和完整结果相同**时，触发 `repeated_tool_failure`。call ID 不参与比较，正常重复读/轮询不会因此停止。有进展、错误变化或参数变化会重置计数；没有宣称覆盖交替循环或所有无进展场景。

### 5. 缓存技能按需加载

保持 PM 自定义 prompt 优先、Yomk 内置技能任务匹配。对于其他缓存技能，只给当前执行技能或匹配显式 `activation` 规则的技能加载正文，正文累计预算 16000 字符；超过预算时整份转为入口，避免截断规则后误用。其余最多 24 个技能展示名称、简介和 `SKILL.md` 入口。

兼容注意：以前靠“缓存中所有正文自动拼入”生效的技能，需要设置 activation，或者通过当前技能选择/明确读取入口。目录显示不授予工具权限。损坏编码或格式的技能文件会记录读取失败并跳过，避免无关任务连带失败。没有为不具备读取工具的普通 LLM 技能增加隐式文件读取或额外模型调用。

## 验证与可复现指标

新增 35 项专项用例位于 `tests/test_runtime_reliability.py`，覆盖真实兼容门面、工具注册表和提示词组合；模型和 HTTP 返回采用确定性桩，**没有使用真实 API Key 或验证在线模型质量**。最终 Python 3.12 全量：**676 passed / 2 skipped / 87.40s**。跳过项分别缺少私有 `resources.yaml` 和 Node.js；5 条 warning 来自既有 FastAPI `on_event` 弃用提示。编译、StationController 导入、F821/F822/F823 名称扫描、依赖一致性和 docs/design 208 条目/11 域校验通过。仓库 pre-push 门禁为 0 Blocker、2 个既有 Warning（Station.start 函数长度、sync_docs 日志前缀）。本机异步/HTTP 测试在允许 loopback socket 的隔离测试进程执行，未访问线上节点。

合成对照：30 次请求，每轮工具产生 100010 字符文本，初始规则 720 字符、原始用户要求 13 字符，使用单工具 schema；同一消息序列分别采用旧的无限积累方式与新预算。统计包含工具 schema 与 JSON 转义，最后一轮工具结果尚未进入后续请求，因此累计值只覆盖实际模拟的 30 次请求。

| 指标 | 原方式 | 本轮实现 |
|---|---:|---:|
| 累计发送字符 | 43,621,850 | 1,580,290 |
| 最大单次请求字符 | 2,907,219 | 63,813 |
| 原始 system/user 内容 | 保留 | 保留 |
| 累计字符减少 | — | 96.38% |

该数字仅说明“大输出重复回填”场景中的请求体上界，**不能换算成实际 token 费用或宣称所有任务节省 96.38%**。输入缩小也可能丢失工具细节，分段重读和未来的结果归档是必要配套。

## 后续路线与验收标准

按优先级独立迭代。P1-A 于 iter-115 完成源码及本机隔离验证，见
[投递协议、故障语义与实测边界](03-task-orchestration/durable-mesh-delivery.md)；
P1-B/C/D及P2-A/B已完成开发和本机隔离验证。物理双节点、在线模型与断电验收仍待环境；各轮实现与边界见下文及[最终交付清单](dsbot-loop-completion.md)。

| 次序 | 任务 | 拟修改范围 | 验收条件 |
|---|---|---|---|
| P1-A | 持久投递与结果确认 | PM 分发/回传、Worker、SQLite repository | 写入后断网/进程退出仍能恢复；重复投递只结算一次；迟到结果不复活已取消任务；长结果有校验与生命周期；至少两个真实隔离节点验证 |
| P1-B | 有来源的分层记忆 + 历史检索 | Conversation/Memory 服务、SQLite、秘书 prompt | project/conv/agent 范围隔离；条目回链原消息；支持更正/撤销/过期；中文检索；重建索引不丢原文；网页指令不变成用户授权 |
| P1-C | 模型窗口、语义检查点与完整产物 | RuntimeContext、模型配置、工具结果存储 | 预留输出窗口；摘要失败不丢要求；工具调用配对一致；跨重启可读原始观察；取消不中断原子写；用真实长任务比较完成率与成本 |
| P1-D | 验收证据与按尝试记账 | PM 验收、资源记账、RuntimeTrace | 每条验收有证据引用；模型自评与测试结果可区分；重试/辅助调用只计一次；失败/取消计入成本 |
| P2-A | 协作工作台和人工接管 | L1/L2 对话、现有 Web/React、独立浏览器能力 | 用户可看清谁在做什么、谁等待谁；接管后自动动作暂停；重复通知不刷屏；前端浏览器实测后交付 |
| P2-B | 持久周期任务和 IM 扩展 | Bot/调度服务、通道适配层 | 时区、归属、停机补跑和并发规则明确；各入口复用相同动作授权与任务模型；失败可重试而不重复执行 |

## 不直接移植的部分

DS-Bot 的宿主是 DSH，单主进程状态文件、Electron 浏览器分区、插件生命周期和具体 CLI 适配都依赖其运行环境。LAN Mesh 的故障范围包含多个主机和网络分区，不能只借用同样的 UI 或 JSONL 文件就认为获得同样的可靠性。DS-Bot 的隐私约束也明确存在 shell/OS 边界限制，不应宣传为系统级沙箱。

本轮为独立实现，只参考公开设计和行为，不引入 Apache-2.0 上游代码；将来若复制实现或资源，应按其 LICENSE/NOTICE 保留相应声明。

### iter-116：P1-B实现进展

新增Secretary侧作用域记忆与中文JSONL检索，支持来源回链、显式更正/撤销/过期、并发修订和索引重建；已接入秘书回复参考上下文，保持操作意图输入边界。Agent范围采用现有PM线程，跨主机记忆同步与PM执行prompt分发未实现。详见[设计说明](06-interaction/scoped-memory-history.md)。

### iter-117：P1-C实现进展

新增按候选模型窗口预留回复预算、持久完整产物、确定性执行检查点及作用域分页读取。取消与归档失败均保留真实执行状态；共享和云同步不暴露产物目录。实测边界与兼容说明见 [设计文档](04-execution-engine/model-window-artifacts.md)，真实长任务完成率与成本对比仍待在线环境。

### iter-118：P1-D实现进展

验收标准完整覆盖，来源绑定区分模型自评与原始观察；实际HTTP调用拥有独立用量ID，失败/取消和辅助调用纳入归因，跨站部分确认保留未接收记录。范围与供应商账单边界见 [设计说明](03-task-orchestration/acceptance-evidence-usage.md)。

### iter-119：P2-A实现进展

协作工作台、退出确认后人工接管、任务隔离的可选浏览器和通知去重已实现，详见[设计与验证边界](06-interaction/collaboration-workbench.md)。

### iter-120：P2-B实现进展

新增时区周期计划、停机补跑与并发策略、稳定任务触发标识、统一IM收件账本、Telegram限定来源和回复重试，以及共享管理界面。详见[设计与实际边界](03-task-orchestration/periodic-im.md)。

### iter121：P1-B本机补齐
记忆管理页、PM作用域读取、大日志分段索引及来源会话绑定已实现。12新增专项、全量832 passed/1 skipped；Chrome七组实测通过。跨站复制单独进入iter122。

### iter122：P1-B跨站记忆
项目范围私有快照、原始JSONL与修订复制、分片续传、手工备用实例恢复及管理界面已实现。明确不包含自动主备选举/网络分区共识，见 [故障语义](07-data-sync/private-memory-backups.md)。

### iter123：P1-C后续
执行产物保留与清理预览、验收证据保护、跨站只读副本和真实SIGKILL恢复验证已实现，详见 [生命周期设计](04-execution-engine/artifact-lifecycle.md)。新增14项回归，全量860 passed/1 skipped，门禁0 Blocker/21 Warning；安装包隔离验证和浏览器验证通过。开发路线P1/P2既定范围全部落地；物理多机、在线模型和生产发布仍需独立验收。
