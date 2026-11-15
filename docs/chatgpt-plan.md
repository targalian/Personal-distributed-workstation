# ChatGPT / Codex 订阅模式

模型池支持 `usage_mode: api_key`（默认）和 `usage_mode: chatgpt_plan`。订阅模式通过本机官方 Codex CLI 的 app-server 调用模型，OAuth 凭据由 Codex 自己保存和刷新，不进模型池、不进密钥同步。

## 登录

在运行 Station / Worker 的同一个系统用户下执行：

```powershell
npm install -g @openai/codex
codex login
codex login status
```

无浏览器的执行节点可用 `codex login --device-auth`。需要支持 app-server 动态工具的近期版本（本机实测 0.164.0 可握手）。

## 配置

把条目加入 `lan_mesh/model_pool.yaml`（或复制 `model_pool.chatgpt-plan.example.yaml`）：

```yaml
default_model: chatgpt-codex
models:
  - id: chatgpt-codex
    provider: openai
    usage_mode: chatgpt_plan
    model_name: ""          # 空串用 Codex 默认模型，也可填账号可访问的具体模型
    capabilities: [general, reasoning, coding, long_context]
    quality_score: 0.95
    speed_score: 0.7
    fallback: []
```

`id` 是工作站内部路由名；`api_key_env` 在订阅模式下不需要。改动后重启节点生效。

## 行为

- 路由候选：订阅条目不再看 API Key，改看本机 Codex 登录状态（15 秒缓存）；未登录会被剔除。
- 秘书普通对话（`_call_llm_with_routing`）和带工具的 ReAct 调用（`_call_llm_messages_with_tools`）都支持；每轮一个临时 app-server 线程，历史由 LAN Mesh 传入。
- 每轮最多返回一个工具调用，工具仍由 LAN Mesh 执行；Codex 内置 shell / 网页搜索被禁用，线程用只读沙箱。
- 无偏好模型时 `_call_llm_full` 会先试已登录的订阅条目，再走 API provider。
- 用量在 Codex 返回时记账，提前返回的工具轮次可能记 0，不代表订阅余额。
- 状态接口：`GET /api/models/chatgpt-plan/status`，只读探测，不推理、不返回邮箱或令牌。

## 边界

- 每个执行节点各自登录；现有密钥同步不传 ChatGPT 凭据。
- 订阅额度、限流与模型权限以账号为准；默认不做付费 API 降级，需要时显式配 `fallback`。
- 本机当前代理断开时 `account/read` 会失败，状态接口返回 `ready: false`；这不等于未登录。真实推理验证待网络恢复。

## 验证

`tests/test_codex_plan.py` 用 mock 覆盖：配置校验、可用性判定、`_resolve_provider` 返回订阅模式、文本与工具两条调用路径、`call_plan` 的文本/工具事件解析与用量、传输失败时的状态探测。未做真实联网推理。
