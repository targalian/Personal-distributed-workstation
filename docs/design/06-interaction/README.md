# 06 交互通道

用户与系统的对话入口：Web 秘书聊天、手机 Bot 网关、统一角色人格。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| bot_gateway.py | Bot 网关 — 手机消息通道 |
| chat_handler.py | 秘书聊天处理器 — Web 端对话接口 |
| role_cards.py | 统一角色卡定义 (M6) — Secretary/PM/Worker 人格的单一事实源。 |
<!-- /AUTO:module-list -->
---

## chat_handler.py — 秘书聊天处理器（57KB）

**职责**: Boss 通过 Web 聊天窗口与秘书对话的主入口。

**处理流程**:
1. 接收消息 → 构建工作站状态摘要作为 system prompt 上下文
2. 调用 LLM 生成回复
3. 解析回复中的操作意图并执行
4. 返回回复 + 操作结果

**设计要点**:
- 聊天历史持久化 SQLite（chat_history 表），重启不丢失
- 支持多对话架构（L1 项目对话 / L2 PM 线程），分层混合交互的载体
- 历史曾出现「前端双渲染重复回复」缺陷（已修复，前后端各渲染一次的问题）

**自然语言 DAG 编辑意图** (iter-51, F4.3): `_ACTION_KEYWORDS` 新增
graph 编辑关键词组（编辑图/修改图/加一步/删除步骤/跳过步骤/加依赖等,
置映射首位保证优先级）→ `_action_edit_task_graph()` 执行 —
定位任务 (task-xxx 正则优先 + 名称匹配) → 读图 → `_parse_graph_edit()`
LLM 解析结构化编辑指令 (add_node/remove_node/add_edge/remove_edge) →
TaskDAG 应用 (自带环检测回滚) → `update_task_graph` 落盘；
防幻觉: 真实执行并返回落盘结果, 解析失败/未找到任务均明确报错不虚报。

**优化讨论纯对话通道** (iter-73): `chat()` 新增 `discuss_context` 参数
(契约 `{topic: 优化项 id | __all__}`, 由 `POST /api/secretary/chat` 透传) —
非空即进入讨论模式: (1) `_build_discussion_context()` 把话题对应优化项详情
(标题/来源/优先级/状态/说明/Boss 补充) 或队列总览 (守护状态/队列数/待决策数 +
活跃项前 10 条) 追加进 system prompt; (2) `_apply_chat_action()` 跳过
`_detect_action` 关键词检测, `action_taken` 固定为 `opt_discuss`。
**动因**: `_ACTION_KEYWORDS` 是子串匹配, 讨论文本里的「状态」「帮我写」
「遇到瓶颈」会误触发查询/建任务/优化守护等副作用; 讨论区需要的是纯解释而非执行。
优化器缺失或取数异常时降级为占位提示文案, 不打断对话。

## bot_gateway.py — Bot 网关（手机通道）

**架构**:
```
StationController
  └── BotGateway
        ├── 企业微信群机器人 Webhook (单向推送)
        └── Telegram Bot API (双向: sendMessage 推 + getUpdates 收)
```

**职责**: 工作站事件推送手机、接收手机端命令、Secretary 与手机用户的
异步交互通道。

**优化项**: 消息聚合防刷屏（短时间窗口事件合并）、Telegram Inline
Keyboard（PM 决策交互）。

**事件模板** (iter-44): 新增 `error_burst` (错误突发, high 优先级) —
窗口内错误数超阈值且冷却到期时推送, 与错误追踪闭环联动。
**事件模板** (iter-52): 新增 `cost_budget_warning` (预算适配告警,
normal 优先级, 💰 图标) — 任务提交时预估超预算 (tight/insufficient)
推送任务名/预估 tokens/状态/建议文案, 与预算顾问联动。
**错误追踪埋点** (iter-45, F1.4 数据源): bot_gateway 两处接入 `error_tracker.capture`
— 推送重试耗尽进离线队列前 (module=bot, 携带通道/事件类型)、秘书对话链异常兜底前；
异常隔离不影响原降级行为。

## role_cards.py — 统一角色卡（M6）

**背景**: 角色人设此前散布于 chat_handler（秘书）、pm_planner / agent_prompt
（PM）、agent_prompt（Worker）多处，人格调整需改多份。

**设计**: 三角色（Secretary/PM/Worker）的 identity / mission / sections
集中定义于此，各模块仅引用；**人格调整只改一处**。

## 秘书动作护栏 (BUG-031, iter-77)

**事故**: Boss 多轮澄清股票自动交易系统需求后回「是, 以上信息
应该也已经足够了」, 秘书答「系统正在处理您的项目创建指令」,
但后台零动作 —— `projects` 表无记录, `chat_history.action_taken`
全库只有 `''` 与 `opt_discuss`, 从未出现 `create_project`。
Boss 在等一个从未开始的操作。

**三重根因** (叠加才造成静默失败):
1. `_detect_action` 是纯字面子串匹配, `create_project` 仅认
   「创建项目/新建项目/建立项目」。确认类追问必然漏判。
2. 角色卡 `role_cards.SECRETARY_CARD` 无条件要求「回复收到, 系统
   正在处理您的指令」—— 未区分动作是否真被识别, LLM 照做。
3. `chat()` 先调 LLM 再检测意图, LLM 无从得知后台会不会执行。

**修复** (三层, 均经反向验证):
| 层 | 手法 |
|---|---|
| 时序 | `_detect_action` 提到 LLM 调用之前, 结果经 `_build_action_guard` 注入 system prompt |
| 提示词 | 命中则允许确认; 未命中则明令禁止「正在处理」并要求给出带触发词的指令样例 |
| 兔底 | `_append_no_action_notice` — prompt 约束不是硬保证, LLM 仍谎称时追加⚠️系统提示 |
| 意图继承 | `_detect_action_with_context` — 仅「本轮是纯确认」且「上轮秘书给过引号样例」时继承 |

**为何继承取引号样例而非整段回复**: 本例可继承信号在 assistant
建议里 (`id=116` 含「创建项目: ...」样例), 不在 user 消息里。
但若直接扫整段 assistant 文本, 秘书解释能力边界时提到的
「创建项目」也会误触发, 故只认 `「」`/`『』` 包裹或 `>`
引用行内的片段 (`_extract_quoted`), 并限 30 字以内确认句。

**测试**: `TestIter77SecretaryActionGuard` 8 例 — 护栏注入/兔底追加/
普通回复不动/引号继承/散文不继承/长句非确认/命中时照旧执行/
讨论模式不受影响。反向验证: 护栏不注入、去兔底、去继承
三种改法均即刻 FAIL。

## 秘书需求收集状态机 (iter-78)

**目标**: Boss 提出项目/任务想法后, 秘书先通过多轮对话收集结构化需求,
生成项目 Brief 与可执行最终提示词, 确认后再提交给 PM Agent。显式
`提交任务:` 指令不被拦截, 仍走原即时提交路径。

**状态机**:
`INTAKE (收集) → SYNTHESIZE (总结) → GAP_FILL (补缺) → CONFIRM (确认) → DISPATCH (提交)`

- 命中 `_REQ_INTAKE_KEYWORDS` (如「我想开发」「帮我规划一个项目」) 时创建草稿
- `INTAKE` 阶段按 checklist 逐项追问; 关键项齐全或 3 轮后进入总结
- `GAP_FILL` 阶段针对缺失关键项追问; 补齐或 5 轮后进入确认
- `CONFIRM` 阶段展示 Brief 与最终提示词; 确认则派发, 修改则回到 `INTAKE`
- 「够了」「开始吧」「按最终提示词执行」等快速退出词直接派发
- 「取消需求收集」只清草稿, 不创建任务

**最终提示词**: `_build_final_prompt` 生成
`提交任务: <目标>` 开头的自包含指令, 后跟 `【项目背景】`
等结构化字段。Boss 可直接复制粘贴, 也可回复「按最终提示词执行」;
若粘贴修改后的最终提示词, `_apply_final_prompt` 会把修改回填草稿
再派发, 不会丢失 Boss 的改动。

**持久化**: 草稿写入 `conv_index` 的
`meta["requirement_draft"]`, 重启后由 `_load_req_drafts` 恢复;
派发成功后 `_clear_req_draft` 清除, 避免残留活跃草稿。

**派发链路**: `_dispatch_from_draft` 调
`submit_task_from_chat(..., input_data={{"requirement": brief}})`。
Brief 在任务创建前就写入 `input_data`, 避免「先派发再补写」的竞态;
PM `refine_requirements` 检测到 `requirement` 后跳过重复追问。

**测试**: `TestRequirementGathering` 7 例 — 收集关键词建草稿/
确认派发并携带 Brief/最终提示词修改回填/取消不派发/显式提交不被拦截/
快速退出仍带 Brief/PM 收到 Brief 不再追问。pytest 415 passed。

## LLM 意图分类兜底 (iter-79)

**问题**: 69 个 `_ACTION_KEYWORDS` 是纯字面子串匹配, 口语化指令
（如「帮我建个项目」）必然漏判 → 关键词与确认继承都未命中 →
后台静默不执行, 只能靠 BUG-031 的「未识别」护栏兜底提示。

**方案**: 在 `chat()` 的意图判定链路上加第三层兜底 —
关键词快路径 (零成本) → 确认句继承上轮样例 →
`_classify_action_llm` LLM 意图分类。分类结果必须落在
`_ACTION_DESCRIPTIONS` 动作白名单内才生效, 输出非 JSON、
动作越权或调用异常一律回退无意图, 维持「宁可明说不执行,
不可虚报已执行」底线。

**成本闸门**: `_looks_like_command` 用动词/领域名词信号 +
200 字长度上限做廉价预筛, 纯闲聊不产生任何额外模型调用;
命中关键词的消息也直接走快路径, 不进分类器。分类 prompt
只含动作白名单与最近 4 轮上下文 (消解「那就建吧」类指代)。

**架构约束**: 分类发生在主回复 LLM 调用之前, 结果经
`_build_action_guard` 注入 system prompt (命中则告知后台将真实
执行), 因此 BUG-031 的时序与护栏语义完全保留; 分类器只负责
「是否执行」, 不改变执行器与操作结果展示链路。

**测试**: `TestIter79LlmIntentClassifier` 6 例 — 口语指令分类并执行/
闲聊零分类成本/分类 none 不执行/非 JSON 输出忽略/白名单拦越权/
关键词快路径不进分类器。pytest 421 passed (415 基线 + 6 新增)。

## 创建对话失败与让位同步 (iter-80)

**现象**: Boss 手动激活 Secretary 后点击「新建对话」失败。端到端
复现链路为：激活接口先返回成功；约 2 秒后发现本网段已有 `device_id`
更小的 Secretary，E4 仲裁将本机降级；`POST /api/conversations` 因此
返回 503。此前 dashboard 未监听 `secretary_yielded` 事件，界面仍显示
Secretary 可用；同时 `createConversation()` 不检查 `response.ok`，
后端 `detail` 被吞掉，只表现为创建失败。

**修复**: 激活前执行 E4 仲裁预检，已有优先 Secretary 时直接返回
`ok:false + conflict + secretary_url`，不再经历「成功→让位」竞态；
dashboard 监听 `secretary_yielded`，立即执行 `updateSecretaryUI()`
隐藏 Secretary 功能并 toast 提示对端接管；新建对话接口非 2xx 时
解析并展示 `detail/message`，503 语义可见。

**测试**: `TestSecretaryConflict` 新增 2 例 — 优先 Secretary 拒绝手动
激活并返回地址、在线 Secretary 过滤 self/offline/fed；全量 pytest
423 passed。真实隔离 Station 端到端复现验证：手动激活返回
`ok:false` 与 `http://192.168.1.206:45470`，后续创建对话 503。

## 需求派发结构化上下文 (iter-81)

**问题**: 真项目联测中, Boss 已在需求描述里给出
`E:\ingobj\stock_player` 与远端仓库, 但需求收集派发只传
`requirement` 字符串, PM 模板把项目路径折叠为 `.`; 同时 Brief
写「高」优先级却落库为 `normal`。

**修复**: `_dispatch_from_draft()` 从原始消息与 checklist 提取
`project_path`、`repo_url`, 识别「只做规划 / 不直接修改」为
`execution_mode=planning_only`; 优先级关键词补齐「高」。
PM 规划消费这些结构化字段, planning-only 任务不再生成代码实现
与单元测试子任务。

**测试**: `TestRequirementGathering` 新增 1 例, 覆盖项目路径、
仓库、planning-only 与高优先级派发。

## 秘书聊天线程池隔离 (iter-82)

**问题**: `/api/secretary/chat` 是 async 路由, 但直接同步调用
`chat_handler.chat()`。LLM、意图分类与需求提取耗时期间会阻塞
FastAPI 事件循环, 导致其他 API (含认证端点) 同时超时。

**修复**: 路由通过 `run_in_threadpool()` 调用 `chat_handler.chat()`,
请求与响应结构保持不变; 长耗时聊天不再阻塞事件循环。该修复不改变
HTTP 同步等待语义, 客户端仍需自行设置足够超时时间。

**测试**: `TestIter73OptimizationDiscuss` 扩展路由回归 — 校验
`chat()` 在非请求线程执行且 `discuss_context` 原样透传。

## 秘书对话派发任务的项目归属 (iter-93)

`ChatHandler._resolve_project_from_message()` 在 `submit_task` 动作执行前解析
Boss 消息中的项目线索, 三级匹配 (命中即返回, 全未命中返回空串保持旧行为):

1. 完整 `project_id` (uuid) 出现在消息中;
2. `project_id` 前 8 位短码 (Boss 惯用写法, 如「项目 821230b9」);
3. 活跃项目名称包含于消息中, 取**最长匹配** (防「股票」误命中「股票自动交易系统」)。

**已知局限 (实测发现, 待排期)**:

- 秘书角色卡 (`role_cards.py` SECRETARY_CARD「行为约束」) 明确要求
  「如果用户询问工作站能力范围外的问题 (如股票交易、编程开发等), 礼貌地说明
  你的职责是管理分布式 AI 工作站, 无法处理该类问题」。这与「秘书作为项目
  交接入口」的定位冲突 — 实测交接股票项目时秘书虽未拒答, 但回复趋于简短。
- 同一角色卡要求「回复必须简洁明了」, 导致要求秘书输出完整任务拆解计划时
  回复被压缩 (实测 605 字后截止, 非 max_tokens 限制, `max_tokens` 为 4096)。
- 结论: 秘书适合做**指令入口与状态问答**, 不适合承担长篇规划输出; 长篇拆解
  应由 PM Agent 产出并经 `/api/pm/{id}/progress` 回流。

## 秘书角色定位修正: 项目管理入口而非领域专家 (iter-95)

iter-93 记录的「角色卡与项目交接定位冲突」经 Boss 定调后修正。**关键区分**:
「拒答股票交易类问题」的本意是防止秘书对**交易策略**产生幻觉 (该买哪只股票、
什么策略能赚钱、涨跌预测), 这与**开发一个股票自动交易系统**并不冲突 ——
后者是业务软件项目管理, 正是秘书本职。

原约束把「(如股票交易、编程开发等)」整类划为拒答范围, 属过度收敛, 已移除。

### 确立的协作模式

```
Boss ←→ 秘书    讨论产出并维护「项目蓝图」(使命/目标/非目标/验收标准/约束
                + 路线图阶段 + 决策记录)
         ↓ 蓝图驱动规划 (iter-89)
        PM       依蓝图执行生产, 交付前按验收标准自检 (iter-90)
         ↓ 交付结论回流 (iter-95)
      蓝图对应阶段   verdict/unmet 写回 roadmap[i].deliveries
         ↓
Boss 据此提出下一轮蓝图修改 → 多轮迭代
```

蓝图与项目尽可能保持**一一对应**关系。

### 角色卡改动

- 移除「如果用户询问工作站能力范围外的问题 (如股票交易、编程开发等), 礼貌地
  说明...无法处理该类问题」
- 新增「你负责管理项目, 不负责充当领域专家」——讨论业务软件的需求/目标/验收
  标准/里程碑是本职; 领域专业判断则引导 Boss 转化为蓝图里的验收标准, 交由 PM
  与领域 Agent 落实
- 新增「与 Boss 讨论项目的产出物是项目蓝图, 而不是你直接写方案」并说明蓝图与
  项目一对一、交付回流、Boss 迭代的完整链路
- 「回复必须简洁明了」→「回复以简洁为默认, 但 Boss 要求梳理蓝图、需求、验收
  标准或阶段计划时应完整输出, 不要为了简短而截断要点」(修正 iter-93 实测
  拆解计划被压到 605 字的问题)
- 能力范围新增第 7 项「与 Boss 讨论并维护项目蓝图」

## BUG-035 询问句误触发执行动作 (iter-96)

### 现象 (iter-95 真机验证时实测抓到)

向秘书提问「M1 阶段的**验收标准**有哪些?」, 回复内容正确, 但末尾追加了
「📋 操作结果: ✅ 已验收任务「[回流验证] 无项目任务」的交付物」——
**一次提问执行了一次验收**。

### 根因

`_detect_action` 是朴素子串匹配:

```python
for keyword, action in _ACTION_KEYWORDS.items():
    if keyword in message or keyword in msg_lower:
        return action
```

`_ACTION_KEYWORDS` 把「验收」单列为 `accept_delivery` 触发词, 而「验收标准」
必然包含「验收」→ 询问被判定为执行。同类风险词还有「退回」「取消」「暂停」等,
凡是「X 是什么 / X 有哪些 / X 标准」形式的提问都会命中。

注: 模块内第 178 行本就把「验收标准」列为需求收集问句 (`acceptance`),
两处语义直接冲突, 属设计遗漏而非偶发。

### 修复

新增 `_looks_like_question()`, 在关键词匹配**之前**拦截询问句:

- `_QUESTION_MARKERS` — 是什么/有哪些/哪些/如何/吗/?/标准/口径/定义/查看 等
- `_IMPERATIVE_MARKERS` — 帮我/请/麻烦/立即/马上/执行 等, **优先级更高**

祈使优先保证「帮我验收一下任务A好吗」这类含疑问词的真指令仍照常执行,
只有纯询问才被拦下。

### 验证

6 专项 + 6 变异全部致红。变异首轮有两处未致红 (M3 移除「标准」、M4 削减疑问词表),
原因是样本句同时命中多个标记, 冗余掩盖了单点失效 —— 已补
`test_each_marker_independently_effective`, 每条样本只依赖一个标记,
任一标记从表中丢失都会变红。494 pytest passed。

## BUG-036 否定/复述/议论语气误触发执行动作 (iter-97)

BUG-035 只堵住了疑问句。本轮系统性扫描全部 69 个 `_ACTION_KEYWORDS`, 用 21 句
真实非指令样本实测, **仍有 15 句误触发**。

### 最危险的一类: 否定句

真机实测向秘书说「**先不要**创建项目, 我们再讨论一下蓝图」, 结果:

```
📋 操作结果: ✅ 项目已创建!
  名称: 暂缓建项讨论蓝图
  描述: 暂不进行创建项目的操作，需进一步讨论蓝图
```

语义完全相反 —— 明确说不要做, 系统却做了, 还把「暂不进行创建」当成了项目名。
秘书回复中甚至写明「我本身无法拦截或取消该动作」。
(历史残留项目「创建并启动项目」应属同类误触发产物。)

### 三类非指令语气

| 类别 | 样本 | 命中的执行关键词 |
|---|---|---|
| 否定/推迟 | 先不要创建项目 / 不需要暂停任务 / 没必要退回 | 创建项目 / 暂停任务 / 退回 |
| 复述过去 | 刚才那次验收是误触发的 / 上次取消任务导致数据不一致 | 验收 / 取消任务 |
| 议论提议 | 我们要做个决定: M2 先做结构治理 / 加一步审计是不是更稳妥 | 做个 / 加一步 |

### 修复

`_looks_like_non_directive()` 统合四类语气判定, 在关键词匹配后、动作返回前拦截:

- `_NEGATION_MARKERS` — 不要/不用/不需要/没必要/别急/先不/暂时不/无需 等
- `_RETROSPECT_MARKERS` — 刚才/上次/上轮/之前/已经/那次/导致 等
- `_DELIBERATION_MARKERS` — 做个决定/应该/建议/考虑/打算/更稳妥 等
- 疑问标记沿用 BUG-035, 并补齐反复问句 (会不会/是不是/要不要) 与疑问代词 (谁/哪个)

**关键设计**: 否定与复述**不受祈使信号豁免**。「请先不要创建项目」同时含祈使
「请」与否定「不要」, 语义是「不要做」, 必须拦住 —— 只有疑问才让祈使优先。

### 同时修正 BUG-035 的过度拦截

初版把「查看」列为疑问标记, 导致「查看任务列表」这类合法只读指令被一并拦下。
新增 `_READONLY_ACTIONS`(query_status/progress/hosts/tasks) 白名单直接放行:
**护栏的目的是防止误改状态, 不是让查询失灵**。因此「不用管别的, 就看进度」
虽含否定词, 仍正常返回 `query_progress`。

### 护栏必须覆盖两条路径, 否则被绕过

`_detect_action` 只是第一条路径。关键词未命中时消息会继续走 iter-79 的
LLM 意图分类兜底 (`_classify_action_llm`), 实测四句否定/复述**全部能通过**
其成本闸门 `_looks_like_command` —— 关键词路径拦住了, 模型却可能把
「先不要创建项目」重新判成 `create_project`, 护栏形同虚设。

因此在 `_looks_like_command` 开头同样调用 `_looks_like_non_directive` 拒闸,
顺带省掉一次无谓的 LLM 调用。关键词未命中的口语化真指令 (「帮我建个项目」
「弄一个数据校验的活」) 仍正常进入分类, iter-79 的价值不受影响。

### 验证

- 21 句非指令样本: 变更类误触发 15 → **0**
- 18 条真实指令 + 3 条口语化变体: 全部保持 (0 回归)
- 8 专项 + 8 变异全部致红 (含「LLM 闸门护栏移除」一处); 503 pytest passed

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-09-09 | iter-97 | BUG-036 否定/复述/议论语气误触发修复: 系统扫描 69 个关键词后实测 21 句非指令样本仍有 15 句误触发, 真机实测「先不要创建项目」竟真建了个名为「暂缓建项讨论蓝图」的项目; 新增 `_looks_like_non_directive` 统合否定/复述/议论/疑问四类语气, 否定与复述不受祈使豁免; 同时以 `_READONLY_ACTIONS` 白名单修正 BUG-035 把「查看任务列表」一并拦掉的过度拦截; 变更类误触发 15→0, 18 条真实指令 0 回归; 护栏同时覆盖关键词与 LLM 分类两条路径 (否则模型会把否定句重新判成动作); 8 专项 + 8 变异致红 |
| 2026-09-09 | iter-96 | BUG-035 询问句误触发执行动作修复: 「验收标准有哪些」因子串匹配命中「验收」→ 误执行 accept_delivery (iter-95 真机验证时实测把测试任务验收了); `_detect_action` 前置 `_looks_like_question` 拦截, 祈使信号优先于疑问标记保证真指令不受影响; 6 专项 + 6 变异致红 |
| 2026-09-09 | iter-95 | 秘书角色定位修正 (Boss 定调): 移除「编程开发等」整类拒答 — 拒答**交易策略**幻觉与开发**交易系统**不冲突; 确立 Boss↔秘书产出蓝图 → PM 依蓝图执行 → 交付回流蓝图 → Boss 迭代 的闭环, 蓝图与项目一一对应; 简洁改为默认而非硬约束, 蓝图梳理须完整输出; 新增第 7 项能力 |
| 2026-09-09 | iter-93 | 秘书对话派发任务补齐项目归属解析 (uuid/短码/名称最长匹配), 回复回显绑定结果; 记录秘书角色卡「拒答股票交易类问题」与「回复必须简洁」两条约束同项目交接定位的冲突 (实测拆解计划 605 字被压缩) |
| 2026-09-05 | iter-82 | 秘书聊天线程池隔离: async 路由改用 run_in_threadpool 调用 ChatHandler, 避免同步 LLM 阻塞事件循环; 专项 3 passed |
| 2026-09-05 | iter-81 | 需求派发结构化上下文: 提取 project_path/repo_url/planning_only, 高优先级映射修正; 专项 29 passed |
| 2026-09-03 | iter-80 | 创建对话失败修复: 激活前 E4 仲裁预检 + secretary_yielded 前端同步 + conversation 非响应状态 detail 展示; 专项 2 例 + 全量 423 passed |
| 2026-09-03 | iter-79 | LLM 意图分类兜底: 关键词未命中且过成本闸门时做一次意图分类, 白名单校验防越权, 失败回退无动作; _looks_like_command 动词/名词/长度预筛让闲聊零额外成本; 分类在主回复前完成, BUG-031 护栏时序不变; 专项 6 例; pytest 421 passed |
| 2026-09-02 | iter-78 | 秘书需求收集状态机: INTAKE/SYNTHESIZE/GAP_FILL/CONFIRM/DISPATCH + checklist 模板 + 草稿持久化 + 最终提示词生成与修改回填 + 取消路径; submit_task_from_chat 支持 input_data, Brief 在创建前入库; PM 收到 requirement 后跳过重复追问; 专项 7 例; pytest 415 passed |
| 2026-09-02 | iter-77 | BUG-031 秘书静默失败修复: 意图检测提到 LLM 调用前 + _build_action_guard 把「后台是否会执行」注入 prompt; _append_no_action_notice 兔底追加清晰提示; _detect_action_with_context 仅对纯确认句继承上轮引号样例意图; 角色卡那条无条件「回复正在处理」改为以意图已识别为前提; 专项 8 例 + 三处反向验证; pytest 408 passed |
| 2026-08-27 | iter-44 | 新增 error_burst 事件模板与优先级 (错误突发告警, 与错误追踪闭环联动) |
| 2026-08-27 | iter-45 | bot_gateway 两处错误追踪埋点 (推送重试耗尽/秘书对话链异常, 异常隔离) |
| 2026-08-28 | iter-51 | chat_handler 自然语言 DAG 编辑意图 (F4.3): 图编辑关键词组 + _action_edit_task_graph (任务定位/LLM 指令解析/TaskDAG 应用/落盘, 防幻觉真实执行) |
| 2026-08-28 | iter-52 | bot_gateway 新增 cost_budget_warning 事件模板 (预算适配告警, normal 优先级, 任务提交预估超预算时推送) |
| 2026-09-01 | iter-73 | chat_handler 优化讨论纯对话通道: chat() 增 discuss_context (话题优化项/队列总览注入 system prompt + 跳过命令关键词检测, action_taken=opt_discuss), /api/secretary/chat 透传 |
| 2026-08-16 | iter-27 后 | 初建 |
