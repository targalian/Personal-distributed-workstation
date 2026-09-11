# 04 执行引擎

Worker 侧的任务执行能力：守护进程、Agent 运行时、能力卡片、Prompt 定制、
工具/MCP/沙箱/技能体系。

## 模块清单

<!-- AUTO:module-list -->
| 文件/目录 | 职责一句话 |
|---|---|
| agent_card.py | Agent Card 生成与管理 — 借鉴 A2A 协议的 Agent Card 机制 |
| agent_prompt.py | 子 Agent 通用 Prompt 模板与定制构建器 |
| agent_runtime.py | Agent 运行时 — Worker 端任务执行引擎 |
| mcp_client.py | MCP 客户端 — 轻量级 JSON-RPC 2.0 客户端 |
| mcp_gateway.py | MCP 网关 — 中央工具调度枢纽 |
| sandbox.py | F2.2: 代码执行沙箱 — 安全隔离执行 Agent 生成的代码。 |
| skill_market.py | 技能市场 — 第三方 Skill 插件浏览/安装/卸载 (F5.3 插件系统, iter-61) |
| skill_registry.py | 技能库注册表 — 中央技能管理与分发系统 |
| tool_registry.py | 工具注册表 — 插件化工具管理系统 |
| worker.py | Worker Agent - 部署在各主机上的守护进程 |
<!-- /AUTO:module-list -->
---

## worker.py — Worker 守护进程

**启动流程**: 生成 device_id → 采集 host_info → 创建 shared_folder →
启动 FastAPI → UDP 发现 Secretary → HTTP 注册 → 心跳循环。

**职责**: 本机配置采集、共享文件夹暴露、接受 Secretary 的任务分发与文件下载。

## agent_runtime.py — Agent 运行时（52KB，Worker 侧最大模块）

**职责**: 接收分发子任务并按技能类型执行。

**执行策略**: code_generation / code_review / document_summary（外部 LLM API）、
shell_exec、file_ops、monitoring、rag_search（预留）。

**关键设计**: `custom_system_prompt` 注入点 —— PM 分发的定制 prompt 在此覆盖
默认人设；LLM API Key 经环境变量/资源池配置获取（S1/S3 密钥同步的受益方）。

**项目蓝图约束注入** (iter-89): PM 规划后把蓝图提示写入 `input_data.
_project_blueprint`, `_build_blueprint_prompt()` 将其渲染为 system prompt
尾缀 —— `_call_llm_with_routing` 两条分支 (路由链 / 无路由回退) 与
`_handle_react_agent` 自建 prompt 处各追加一次, 非字符串/空值一律忽略,
截断上限 1200 字符; 无蓝图项目行为与旧版完全一致。
**错误追踪埋点** (iter-45, F1.4 数据源): `_call_llm_with_routing` 降级链耗尽时
capture 到 module=`llm` (context 携带失败链), 异常隔离不影响降级返回。

**多机实测加固** (iter-55, 补强#3):
- `PROVIDER_CONFIG` 补 `volcengine-ark` 置首位 (coding/v3 端点 +
  ARK_API_KEY) — 无路由信息路径 (`_call_llm_full`, pm_planner 规划走
  此路径) 不再跳过 default_model 所在 provider
- 补 `_get_default_model(provider)` 定义 (全库缺失的 AttributeError
  隐患, defaults 含 ark-code-latest 兜底)
- `_ensure_env_loaded` 重写: key_envs 补 ARK_API_KEY; 不再因「部分 key
  已有值」提前 return (否则仅 aliyun key 环境跳过 .env 加载致 ark
  key 缺失); dotenv 缺失时手动解析 .env 兜底
- `main.py` 启动 dotenv ImportError 时同样手动解析兜底 (基础解释器
  无 python-dotenv 场景, 否则 Key 全部缺失)

**SSE 流式中文解码修复** (iter-74, Boss 报告「秘书回复乱码」): `_call_openai_compatible`
以 `resp.iter_lines(decode_unicode=True)` 读流式输出, 解码用的是 `resp.encoding` —
而 requests 对 `text/*` 且不带 charset 的响应一律推断为 ISO-8859-1
(RFC 2616 遗留默认, 见 `requests.utils.get_encoding_from_headers`)。
火山方舟等端点返回 `Content-Type: text/event-stream` 不带 charset, 于是 UTF-8
中文被逐字节拆成 Latin-1 字符 (「你好秘书」→ `ä½ å¥½ç§ä¹¦`)。
**修复**: `raise_for_status()` 后, 仅当响应头未显式声明 charset 时兜底
`resp.encoding = "utf-8"` (SSE 规范强制 UTF-8); 服务端显式声明的仍按声明走,
不武断覆盖。requests 的增量解码器使用 `errors='replace'`, 故跨 chunk 截断的
多字节字符不会抛异常。
**影响面**: 全库仅此一处流式 LLM 调用 (`iter_lines` / `stream=True` 唯一出现处),
但秘书聊天 / PM 规划 / Worker 子 Agent 的中文回复全部经由此处, 属全局性缺陷。
此前未暴露: default_model 于 2026-08-30 切至无 charset 声明的端点, 且 iter-74
讨论通道点亮后中文对话量骤增才显形。回归锚点见 `TestIter74SseUtf8Decoding`
(三例: 无 charset 兜底 / 显式 charset 尊重 / ASCII 不受影响; 已验证移除修复即 FAIL)。

**子 Agent 作业目录注入** (iter-100, BUG-038): `_handle_react_agent` 的
`cwd` 原为 `input_data.get("cwd", self.shared_folder)`, 而全仓库无人写入
该键 —— 子 Agent 恒在空的共享目录里摸索, 明明从描述文本读得到项目路径却
进不去, 耗尽 30 轮被质量门禁判不合格 (M1 重跑连续 3 次, 约 414 万 tokens)。

运行时侧三处改动 (写入侧见 03 域 `attach_blueprint_context`):
- `cwd = input_data.get("cwd") or self.shared_folder` —— 空串也回落,
  避免把 Agent 放进当前进程目录
- 规则段抽为模块级 `_build_react_rules_prompt(cwd)`, 新增「相对路径以此为
  基准」「不要去其他盘符或上级目录漫游」「内容不符即说明并结束, 不要靠反复
  试探消耗轮次」—— 仅注入 cwd 不够, 模型看不到它仍会自由探索
- 新增模块级 `_resolve_tool_path(cwd, raw_path)`: `file_read`/`file_write`
  不接受 `cwd` 参数, 相对路径原按 Station 进程启动目录 (即本仓库) 解析,
  既读不到目标文件也有越界写入风险; 绝对路径与 `~` 展开后为绝对的路径原样
  返回, `cwd` 为空时不改写

与 `validate_cli_agent_cwd` / `CLI_AGENT_ALLOW_SELF_REPO` 是两条独立路径:
后者只作用于 `_handle_cli_agent` 防自举改写本仓库, 本改动不放宽该护栏。
回归锚点 `TestIter100AgentWorkdir` (10 例, 17 变异全致红)。

**CLI Agent 分派链路修复** (iter-101, BUG-039): Boss 把子任务执行切到 CLI Agent
后本机验证, 暴露五个缺口 —— 其中根因3 属**全局性**缺陷, 影响所有技能:

1. **失败被上报为成功** (根因3, 影响面最大): 各 handler 用**返回值**
   (`{"error": ...}` / `{"status": "failed"|"timeout"}`) 而非异常表达失败,
   而 `execute` 原先只要 handler 未抛异常就一律记 `completed` —— 护栏拒绝、
   CLI 非零退出、执行超时全部上报成功, PM 侧 `handle_subagent_failure` 的重试
   与升级链**永不触发**。新增模块级 `infer_result_status` +
   `_finalize_subtask` 采纳内层信号, `cancelled` 保持为独立终态。
2. **未知后端静默回退** (根因2): 原实现把「拼错的后端名」与「未指定」同等对待,
   实测 `backend="nonexistent_backend_zz"` 真的拉起 claude 跑了 180s 才 rc=1。
   抽出 `resolve_cli_backend`, 未知后端名与「指定但未安装」一律显式失败。
3. **业务项目目录被护栏拒绝** (根因1): iter-100 起 PM 会把蓝图 `repo_path`
   注入子 Agent 的 `cwd`, 而 CLI 白名单默认只含 `shared_folder` —— 于是
   iter-100 的修复在 CLI 路径上被打回。解法是配置层放行 (`.env` 的
   `CLI_AGENT_ALLOWED_ROOTS`), 拒绝信息同步补上该变量名, 不动护栏逻辑。
4. **「装了但不可用」仍被选中** (根因4): `detect_cli_agents` 只检查可执行文件
   是否存在。实测本机 claude/aider 均 `--version` rc=0 却未登录, 真实调用跑满
   超时才失败。凭据探测既慢又不可靠, 故新增 `CLI_AGENT_DISABLED_BACKENDS`
   显式停用清单 (detect 与 `get_preferred_cli_agent` 两处分支都生效);
   自动顺序改为 **codex > claude > aider** —— codex 是唯一同时带沙箱
   (`-s workspace-write`) 与显式工作目录 (`-C cwd`) 的后端, 契合护栏意图。
5. **codex 配置目录被环境净化滤掉** (根因5): codex 用 `CODEX_HOME` 定位
   `auth.json`, 不认 `HOME`; 缺失时报「Error finding codex home」直接 rc=1 ——
   与「未登录」是两种失败, 不补会误判成凭据问题。`_build_cli_env` 兜底为
   `~/.codex` (仅 codex 后端、仅目录存在、不覆盖用户显式值)。

> 自举护栏未放宽: 主仓库 cwd 仍被拒绝 (需影子模式或 `CLI_AGENT_ALLOW_SELF_REPO=1`)。
> 回归锚点 `TestIter101CliDispatch` (13 例, 21 变异全致红)。

## agent_card.py — Agent Card（借鉴 A2A 协议）

每个 Worker 启动时生成能力卡片（技能声明、可用工具、模型偏好），
Secretary 据此做任务匹配与分发。

## agent_prompt.py — Prompt 定制体系

**组件**:
- `BASE_SUBAGENT_PROMPT`: 所有子 Agent 共享通用部分（身份/准则/协议/约束）
- `build_subagent_prompt()`: PM 按任务定制角色、上下文、依赖、质量要求
- `PROGRESS_REPORT_FORMAT`: 标准化进度上报格式
- `build_dispatch_context()`: 分发时附加上下文

**链路**: PM 调用 build → `/pm/create-subagent` 端点 system_prompt 字段 →
Worker 注入 AgentRuntime.custom_system_prompt。

## tool_registry.py — 工具注册表

参考 Anthropic MCP Tool 概念：内置工具（file_read/file_write/shell_exec/
http_request）+ YAML 插件 + 运行时动态注册 + 执行调度。每个工具含
name/description/input_schema/handler。

**超时护栏 (iter-85)**: `normalize_tool_timeout()` 统一把 shell_exec /
http_request / run_code 的 timeout 规范到 1-120s（默认 30s，非法值回退默认）；
AgentRuntime 专用 shell_exec 同步使用该规范。超时返回 `timed_out=true`，
`ToolRegistry.call_tool()` 将其映射为 `isError=true`；ReAct system prompt
要求超时后不要原样重试，应缩小范围或明确说明超时原因。

**Git 安全边界 (iter-86)**: shell_exec 输出命中 Git `safe.directory` /
`dubious ownership` / `unsafe repository` 时，返回
`git_safe_directory_denied=true` 与 remediation，并映射 `isError=true`；
ReAct prompt 明确禁止修改 git config 或原样重试 Git 命令，要求改用
file_read / dir_list 做只读分析。项目约定「不修改 git config」由此保持不变。

**本地取消传播 (iter-87)**: `AgentRuntime` 持有协作式取消 Event。PM
`start_task()` 先 reset，`cancel()` 经 `PMDispatcher.cancel()` 调用
`AgentRuntime.cancel()`，并按 `task_agent/task_station` 映射向已分发子 Agent
所在 Station/Worker 调用 `POST /pm/cancel-subagent`。Runtime 在 `execute()`
入口短路，ReAct 在每轮开始与工具调用后检查取消并返回 `status=cancelled`；
本地回退与本机子 Agent 执行均保持 `cancelled` 状态，不再误标为 `failed`。
`/role/cancel-pm` 进入线程池执行，避免同进程取消请求阻塞事件循环。

## mcp_client.py + mcp_gateway.py — MCP 体系

- **mcp_client.py**: JSON-RPC 2.0 客户端，支持 stdio（本地子进程）与
  HTTP（远程 Server）两种传输；initialize → tools/list → tools/call。
- **mcp_gateway.py**: 中央工具调度枢纽。维护全部 MCP Server 连接池 →
  聚合工具列表（统一 /tools/list）→ 路由调用（统一 /tools/call）→
  自动重连；工具描述按模型强弱动态调整示例数量。

**架构链路**: Agent → POST /tools/call → MCP Gateway → JSON-RPC → MCP Servers

## sandbox.py — 代码执行沙箱（F2.2）

subprocess 隔离执行 Agent 生成代码（非 eval）：超时保护（默认 30s / 上限 120s）、
临时工作目录隔离、可选 venv 隔离、输出截断 10KB。

## skill_registry.py — 技能库注册表

**分发链路**: Station Director 扫描 `skills/` 目录 → 注册 SQLite
（skills + skill_assignments 表）→ Worker HTTP 拉取
（GET /api/station/skills/download?role=worker）→ 缓存
`~/.lan_mesh/skills_cache/` → AgentRuntime 读取缓存构建 system prompt。

**技能文件结构**: `skills/{skill_id}/SKILL.md`（含 YAML front matter）+
可选 reference.md。

## skill_market.py — 技能市场 (iter-61, F5.3 插件系统)

**第三方 Skill 插件全链路**: 市场源 `skills_market/` 目录（可配置
`skill_market_dir`, 每子目录一个插件包, 可经任意第三方渠道放入）→
`list_market()` 浏览（含 installed 标记/体积/有效性）→ `install()`
校验后白名单复制（仅 SKILL.md/reference.md）到 `skills/` 并注册 DB
（skills 表 origin=market 列, 迁移 v8）→ 纳入 SkillRegistry 统一的
权限分配与 Worker 分发; `uninstall()` 仅 market 来源可卸载（内置
builtin 拒绝, 403）。

**安全护栏** (第三方内容最终注入 Agent system prompt):
- 包体积上限 `skill_max_size_kb` (默认 200KB), skill_id 白名单字符
  (防路径穿越), front matter 必填 name, 与内置技能同名拒绝覆盖
- 安全默认: 市场包未声明 default_access 时仅 `["station"]` 可用,
  需显式 assign 后才分发 Worker/Agent; 扫描注册 (scan_and_register)
  对未声明 default_access 的技能保持 DB 现值, 防止重扫覆盖安全默认
- 端点 (market 路由定义在 /{skill_id} 前防路径参数捕获):
  GET /api/station/skills/market · POST /api/station/skills/market/
  install · DELETE /api/station/skills/{skill_id} (卸载)

## PM 执行态快照与断点恢复 (iter-53)

**背景**: 修复 multi 模式下聚合永不触发的真实缺陷 — 原先 `_run_task`
finally 无条件停 `running`, 而 `aggregate_results` 只在 progress_loop
中触发, 多子任务任务分发后 progress_loop 10s 内退出、聚合/交付
永不执行; 同时补齐 PM 重启断点恢复 (补强评估缺口 1)。

**快照序列化 (pm_state.py)**:
- `PMState.to_snapshot()`: 16 字段序列化 (plan/task/subtask_outputs/
pending/dispatched/task_station/task_agent/teams/subagents/retry/
超时/启动时间/clarification_question, 线程安全)
- `restore_from()`: 就地恢复 — 只重写字段不替换对象, 保持
planner/dispatcher/monitor 的共享引用有效 (resume 关键约束)

**快照写点 (pm_agent.py)**:
| 阶段 | 写点 |
|---|---|
| planning_done | 规划完成 |
| monitoring | 分发完成 (multi 模式保持 running 等聚合) |
| executing | 子任务结果注入 |
| awaiting_input | 澄清等待 (携带 clarification_question) |
| paused | 暂停 |

快照经 HTTP POST 到 Secretary `/api/pm/{pm_id}/snapshot` 落库
(pm_snapshots 表 UPSERT 一 PM 一快照, 异常静默降级); 完成/失败/取消
时清除; 聚合收尾 (pm_monitor.aggregate_results 末尾) 清快照 + 停 running。

**断点续跑 (resume_from_snapshot → _run_resumed)**: 解析快照就地恢复后按四
场景执行 — 澄清等待重发问题; 无分解标记失败; 全部完成直接聚合; 部分完成
保留已完成输出、重分发未完成 (依赖未满足挂回 pending, 远端子 Agent 随进程
消失不能等回报)。

## 变更记录

| 日期 | 迭代 | 摘要 |
|---|---|---|
| 2026-09-07 | iter-89 | 项目蓝图驱动执行: PM 下发 `_project_blueprint`, `_build_blueprint_prompt` 渲染为 system prompt 尾缀并覆盖路由链/无路由回退/ReAct 三条构建路径; 专项 8 passed |
| 2026-09-07 | iter-87 | 本地子任务取消传播: AgentRuntime 协作式取消 Event, PM cancel 经 dispatcher 传播并广播 /pm/cancel-subagent, execute/ReAct 轮询间隙短路, 本地回退与本机子 Agent 保持 cancelled, cancel-pm 路由线程池隔离; 专项 9 passed |
| 2026-09-06 | iter-86 | Git safe.directory 只读降级: shell 输出识别 dubious ownership/unsafe repository, 标记 git_safe_directory_denied + remediation 并映射 isError; ReAct 禁止改 git config/原样重试, 引导 file_read/dir_list 只读分析; 专项 4 passed |
| 2026-09-06 | iter-85 | 工具超时统一限幅: ToolRegistry shell/http/run_code 与 AgentRuntime shell 共用 1-120s 规范, 超时结果带 timed_out 并映射 isError, ReAct 提示禁止原样重试; 专项 5 passed |
| 2026-09-01 | iter-74 | SSE 流式中文乱码修复 (Boss 报告, Quest 定位): requests 对 text/event-stream 无 charset 响应按 ISO-8859-1 解码致 UTF-8 中文逐字节拆成乱码; 改为响应头未声明 charset 时兜底 resp.encoding='utf-8' (显式声明仍尊重); 全库唯一流式调用点, 影响秘书/PM/Worker 全部中文回复; 新增 3 例回归 (移除修复即 FAIL), pytest 400 passed |
| 2026-08-29 | iter-61 | F5.3 插件系统: skill_market 第三方技能市场 (浏览/白名单安装/卸载) + skills 表 origin 列 (迁移 v8) + 安全护栏 (体积/ID/内置冲突/安全默认仅 station) + dashboard 技能库 Tab 市场 UI |
| 2026-08-29 | iter-55 | 多机实测加固 (补强#3): PROVIDER_CONFIG 补 volcengine-ark 置首位; _get_default_model 补齐定义; _ensure_env_loaded 重写 (ARK key + 部分 key 不再提前 return + dotenv 缺失手动解析); main.py dotenv 兜底 |
| 2026-08-28 | iter-53 | PM 执行态快照持久化 + 断点恢复: PMState 序列化/就地恢复 + 六阶段快照写点 + resume_from_snapshot/_run_resumed 四场景续跑 + multi 模式聚合修复 (_multi_monitoring) |
| 2026-08-27 | iter-45 | agent_runtime 降级链耗尽错误埋点 (module=llm, 携带失败链) |
| 2026-08-16 | iter-27 后 | 初建 |
| 2026-09-10 | iter-100 | BUG-038 子 Agent 作业目录注入 (运行时侧): ReAct `cwd` 改为 `get("cwd") or shared_folder` (空串也回落) + 规则段抽为 `_build_react_rules_prompt` 并新增作业目录基准/禁止跨盘漫游/禁止试探消耗轮次三条 + `_resolve_tool_path` 把 file_read/file_write 相对路径锚定 cwd (原落在 Station 启动目录, 有越界写入风险); 不放宽 CLI 自举护栏; 10 专项 + 17 变异致红 |
| 2026-09-10 | iter-101 | BUG-039 CLI Agent 分派链路五缺口修复: `infer_result_status` + `_finalize_subtask` 采纳 handler 内层失败信号 (原 execute 无条件判 completed, 致护栏拒绝/CLI 非零退出/超时全部上报成功, PM 重试与升级链永不触发, 属全局性缺陷) + `resolve_cli_backend` 让未知/未安装后端显式失败 (原静默回退, 实测拼错后端名真跑了 claude 180s) + `CLI_AGENT_DISABLED_BACKENDS` 停用「装了但未登录」的后端且自动顺序改为 codex 优先 (唯一带沙箱+显式 cwd) + `CODEX_HOME` 兜底 ~/.codex (环境净化滤掉后必然 rc=1) + 护栏拒绝信息补 `CLI_AGENT_ALLOWED_ROOTS` 指引; 本机实测发现, 自举护栏未放宽; 13 专项 + 21 变异致红 |
