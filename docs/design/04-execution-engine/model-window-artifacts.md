# ReAct 模型窗口、执行检查点与完整工具产物

iter-117 / DS-Bot P1-C，2026-11-12。参考 DS-Bot 的上下文回收和证据保留行为独立实现，延续 LAN Mesh 的 Yomk 服务、任务取消和模型降级链。本轮适用于 `react_agent`，不改变 CLI Agent 或普通流式聊天的上下文策略。

## 请求预算

模型池已有 `max_context_tokens`，新增可选 `max_output_tokens`（默认 4096）。每个候选模型分别从原消息副本计算预算；小窗口候选无法容纳必需内容时跳过 HTTP 请求，继续尝试后续模型，不污染其他候选的输入。

- 窗口取本地配置；未登记模型采用 32768，并在报告中标明 `configured_model=false`。这里没有自动发现供应商限制。
- 回复预留为模型配置与 `input_data.reply_max_tokens` 的较小值，最多占窗口一半，至少 1。安全余量为窗口的 5%，限于 256–2048。
- 输入大小使用序列化 messages/tools 的 UTF-8 字节数，再加消息和工具封装余量。它是保守估算，不是供应商 tokenizer 的精确值，也不用于费用结算。
- 请求必须同时满足字符上限及 `estimated_input_tokens + reply_tokens + safety_tokens <= context_window`；实际 HTTP `max_tokens` 使用本次回复预留。`context_max_chars`（默认 64000）和 `tool_result_max_chars`（默认 12000）继续有效。
- 所有候选都不满足时明确失败。system/user 原文、蓝图要求和工具定义不被静默删减。

模型配置示意（数值仅演示本地策略，需按部署模型填写）：

```yaml
max_context_tokens: 32768
max_output_tokens: 4096
```

输出 `context` 保留字符预算统计，新增 `compacted_rounds/checkpoint_ref`。`model_requests` 列出各候选的窗口、输入估算、回复预留、是否容纳和压缩统计。原 `usage` 只累计模型响应报告的用量。

## 历史回收与检查点

模型调用前先归档完整历史。超限时先缩短工具观察，再将较旧的完整 assistant tool_calls / tool 结果组折叠为结构化检查点摘要：工具名、参数片段、观察片段、原始检查点引用及“观察不等于完成”的标记。保留最新完整工具组以及全部 system/user 消息；不完整或 ID 不匹配的组不折叠。摘要是确定性数据整理，不额外调用模型，也不把工具内容提升为用户授权。

`ReactArchive` 保存独立于模型工作上下文的原始会话；字符裁剪、候选预算调整不会覆盖它。摘要失败或压缩后仍超限会失败退出，已保存的完整需求与结果仍可读取。

执行顺序：

1. 生成随机 execution_id，保存运行清单。
2. 模型请求前保存 `before_model` 检查点；实际候选请求保存 `model_request`，包含最终 messages、工具定义及预算，不包含认证请求头。
3. 工具前保存 `tool_started`、pending_call，再复查取消信号。
4. 工具返回后，完整保存参数、所有 content 块、结构化 result 和错误标记；随后保存 `tool_observed`，再向模型提供有限观察。
5. 正常、失败、取消、达到轮次上限均尝试保存最终检查点和状态。归档失败不能报告成功；工具返回却未完成归档时保留 `review_required/pending_call`，不得自动重放副作用。

工具执行期间收到取消，先完成本次结果的原子保存，然后返回 cancelled，不发起下一轮。取消不能撤销已经执行的工具。HTTP 工具不再预先截取一万字符；Shell 超时保留已捕获的 stdout/stderr，并延续旧 stderr 超时提示。这里“完整”指 ToolRegistry 返回的观察；run_code 沙箱自身的输出上限、file_read 的请求分页仍然有效。

## 存储、读取与作用域

产物存于配置的共享目录下 `.runtime_artifacts/<execution_id>/`，已加入 Git 忽略。每个 JSON 记录以内容 SHA-256 命名，包含 agent/task/project/attempt 范围。临时文件写入、flush/fsync、原子替换和 POSIX 目录 fsync 后，才更新运行清单。作用域来自外层任务信封；input_data 不能覆盖它。旧调用缺少 task/project/attempt 时仍可运行，空范围由独立 execution_id 隔离。

`steps[].artifact` 返回 execution_id、artifact_id、sha256、bytes、kind；`checkpoint` 指向最终检查点。被截断的观察带 artifact_id，可调用本次执行专属的 `artifact_read` 工具，按 offset/limit 读取原文，或用 `view=record` 读取完整 JSON。offset 按字符计，默认 limit=4000，上限 16000；`next_offset=null` 表示结束。

Station 和配置了 role_manager 的 Worker 增加只读端点：

| 路径 | 返回 |
|---|---|
| `GET /api/runtime/artifacts/{execution_id}` | 运行状态、phase、latest_checkpoint |
| `GET /api/runtime/artifacts/{execution_id}/{artifact_id}` | 验证 SHA-256 后的字符分页 |

两个端点都要求 query 明确提供 agent_id/task_id/project_id/attempt_id（允许空串），并沿用节点现有认证中间件。范围不匹配或不存在返回 404，标识/校验/分页错误返回 400，存储不可读返回 503。客户端不能指定磁盘根目录。`RuntimeArtifactService` 经所属实例的 Yomk runtime 注册、派发与关闭，不创建全局单例。

普通 `/shared` 下载与云同步排除产物目录及其符号链接别名，防止绕过专用读取接口或被同步覆盖。范围检查是完整性边界；现有 mesh token 不是租户权限系统，已有 file_read/shell_exec 也不是操作系统级隔离。

## 恢复与成本边界

重启后可从同一目录读取清单与原始观察，不依赖旧进程内存。遗留 running/tool_started 不自动改成成功，也不自动重跑；操作者应核对 latest_checkpoint 及 pending_call 后决定是否重新派发。本轮没有“自动恢复正在执行的模型/工具”的入口。

产物暂不自动清理或跨站复制；必须保留目录才能继续引用。检查点保存完整历史，长任务会产生额外磁盘写入和空间成本；超大输出也增加内存和 I/O。记录可能包含工具读取的敏感业务内容，应使用部署主机既有访问和备份控制。后续需按引用关系制定保留/清理策略，不能直接按天删除被引用记录。

本机验收覆盖模型窗口与降级隔离、原要求保留、工具配对、工具分页重读、跨实例恢复、取消竞态、模拟磁盘失败、完整 content 块、校验/范围/符号链接边界、认证 API、共享与云同步排除。模拟长任务完成 12 次工具调用及最终回复，逐次检查实际请求预算并核对全部原文；这是离线行为测试，不代表真实供应商的完成率、token 费用或断电恢复验收。完整测试数字在本轮状态记录中同步。

验收记录：26新增行为；相关75项通过；全量775 passed / 2 skipped / 120.41s；随后只补齐两处旧测试导入，相关17项通过。编译/导入、31变更Python文件名称检查、依赖清单及docs226项通过，门禁0 Blocker/17既有Warning。未进行发布或节点重启。

## iter123 后续完成
产物保留清理、跨站复制、只读恢复和真实SIGKILL检查点演练已补齐，见 [生命周期设计](artifact-lifecycle.md)。真实物理断电与在线模型成本/质量对比仍为环境验收项。
