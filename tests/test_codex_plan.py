"""ChatGPT 订阅传输：配置、可用性判定、路由与响应解析（全部 mock，不联网）。"""
import pytest
from pydantic import ValidationError

import lan_mesh.codex_plan as codex_plan
from lan_mesh import agent_runtime as policy
from lan_mesh.agent_runtime import AgentRuntime
from lan_mesh.config import ModelEntryConfig


def plan_entry(**overrides):
    values = dict(id="chatgpt-codex", provider="openai", api_key_env="",
                  usage_mode="chatgpt_plan", model_name="", quality_score=0.95)
    values.update(overrides)
    return ModelEntryConfig(**values)


def test_example_pool_loads_without_api_key_env():
    from lan_mesh.config import load_model_pool
    pool = load_model_pool("lan_mesh/model_pool.chatgpt-plan.example.yaml")
    assert [entry.id for entry in pool.models] == ["chatgpt-codex"]
    assert pool.models[0].usage_mode == "chatgpt_plan"


def test_server_args_disable_inherited_tool_surfaces(monkeypatch):
    monkeypatch.setattr(codex_plan, "_command", lambda: ["codex"])
    args = codex_plan._server_args()
    assert 'features.shell_tool=false' in args
    assert 'web_search="disabled"' in args
    assert "mcp_servers={}" in args


def test_default_tools_chain_includes_subscription_entries(tmp_path, monkeypatch):
    seen = {}

    def fake_call(entry, messages, tools=None):
        seen["model"] = entry.id
        return {"message": {"content": "", "tool_calls": []}, "model": entry.id,
                "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(codex_plan, "call_plan", fake_call)
    monkeypatch.setattr(codex_plan, "model_available", lambda entry: True)
    monkeypatch.setattr(policy, "_load_model_pool_entries", lambda: {"chatgpt-codex": plan_entry()})
    runtime = AgentRuntime("plan-default", str(tmp_path))
    try:
        result = runtime._call_llm_messages_with_tools([], [], {})
        assert result["model"] == "chatgpt-codex"
        assert seen["model"] == "chatgpt-codex"
    finally:
        runtime.services.close()


def test_config_rejects_unknown_usage_mode():
    with pytest.raises(ValidationError):
        ModelEntryConfig(id="x", provider="openai", api_key_env="K", usage_mode="oauth")


def test_plan_entry_needs_no_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_plan, "model_available", lambda entry: True)
    monkeypatch.setattr(policy, "_load_model_pool_entries", lambda: {"chatgpt-codex": plan_entry()})
    runtime = AgentRuntime("plan-model", str(tmp_path))
    try:
        provider = runtime._resolve_provider("chatgpt-codex")
        assert provider["usage_mode"] == "chatgpt_plan"
        assert provider["api_key"] == ""
    finally:
        runtime.services.close()


def test_tools_call_routes_to_codex_plan(tmp_path, monkeypatch):
    seen = {}

    def fake_call(entry, messages, tools=None):
        seen.update(entry=entry.id, messages=messages, tools=tools)
        return {"message": {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "dir_list", "arguments": "{}"}}]},
                "model": entry.id, "input_tokens": 3, "output_tokens": 4}

    monkeypatch.setattr(codex_plan, "call_plan", fake_call)
    monkeypatch.setattr(policy, "_load_model_pool_entries", lambda: {"chatgpt-codex": plan_entry()})
    runtime = AgentRuntime("plan-tools", str(tmp_path))
    try:
        result = runtime._call_llm_messages_with_tools(
            [{"role": "user", "content": "hi"}],
            [{"type": "function", "function": {"name": "dir_list", "parameters": {}}}],
            {"_model_preference": "chatgpt-codex"})
        assert result["model"] == "chatgpt-codex"
        assert result["input_tokens"] == 3 and result["output_tokens"] == 4
        assert seen["tools"][0]["function"]["name"] == "dir_list"
    finally:
        runtime.services.close()


def test_text_routing_prefixes_system_prompt(tmp_path, monkeypatch):
    seen = {}

    def fake_call(entry, messages, tools=None):
        seen["messages"] = messages
        return {"message": {"content": "PONG"}, "model": entry.id,
                "input_tokens": 1, "output_tokens": 2}

    monkeypatch.setattr(codex_plan, "call_plan", fake_call)
    monkeypatch.setattr(policy, "_load_model_pool_entries", lambda: {"chatgpt-codex": plan_entry()})
    runtime = AgentRuntime("plan-text", str(tmp_path))
    try:
        result = runtime._call_llm_with_routing(
            "ping", {"_model_preference": "chatgpt-codex", "_system_prompt": "你是秘书"})
        assert result["content"] == "PONG" and result["model"] == "chatgpt-codex"
        assert seen["messages"][0] == {"role": "system", "content": "你是秘书"}
        assert seen["messages"][-1] == {"role": "user", "content": "ping"}
    finally:
        runtime.services.close()


def test_call_plan_parses_text_and_usage(monkeypatch):
    events = [
        {"method": "thread/tokenUsage/updated",
         "params": {"tokenUsage": {"last": {"inputTokens": 11, "outputTokens": 5}}}},
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "你好"}}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]

    class FakeServer:
        def __init__(self, *args, **kwargs):
            self.seq = 0
            self.pending = list(events)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, params):
            if method == "account/read":
                return {"account": {"type": "chatgpt"}}
            if method == "thread/start":
                return {"thread": {"id": "th-1"}}
            return {}

        def send(self, event):
            pass

        def receive(self):
            return self.pending.pop(0)

    monkeypatch.setattr(codex_plan, "_Server", FakeServer)
    result = codex_plan.call_plan(plan_entry(), [{"role": "user", "content": "hi"}])
    assert result["message"] == {"role": "assistant", "content": "你好", "tool_calls": []}
    assert result["input_tokens"] == 11 and result["output_tokens"] == 5


def test_call_plan_relays_tool_call(monkeypatch):
    events = [
        {"method": "item/tool/call",
         "params": {"tool": "dir_list", "arguments": {"path": "."}, "callId": "call-9"}},
    ]

    class FakeServer:
        def __init__(self, *args, **kwargs):
            self.seq = 0
            self.pending = list(events)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, method, params):
            if method == "account/read":
                return {"account": {"type": "chatgpt"}}
            if method == "thread/start":
                return {"thread": {"id": "th-1"}}
            return {}

        def send(self, event):
            pass

        def receive(self):
            return self.pending.pop(0)

    monkeypatch.setattr(codex_plan, "_Server", FakeServer)
    result = codex_plan.call_plan(
        plan_entry(), [{"role": "user", "content": "list"}],
        [{"type": "function", "function": {"name": "dir_list", "parameters": {}}}])
    call = result["message"]["tool_calls"][0]
    assert call["id"] == "call-9" and call["function"]["name"] == "dir_list"


def test_plan_status_reports_not_ready_when_transport_fails(monkeypatch):
    class Broken:
        def __init__(self, *args, **kwargs):
            raise OSError("no codex")

    monkeypatch.setattr(codex_plan, "_Server", Broken)
    status = codex_plan.plan_status()
    assert status["ready"] is False and status["usage_mode"] == "chatgpt_plan"
    assert "codex" in status["message"].lower() or "Codex" in status["message"]
