"""Compatibility and adapter contract tests without launching CLI processes."""

import os
from dataclasses import FrozenInstanceError

import pytest

from lan_mesh.harness import Cap, HarnessEvent, HarnessSpec, registry


@pytest.mark.parametrize("prompt,cwd", [("p", "c"), ("中文\n'quoted'", "C:/a b")])
def test_legacy_commands(prompt: str, cwd: str) -> None:
    """Every argv element and legacy metadata field retains its old meaning."""
    from lan_mesh.agent_runtime import CLI_AGENT_BACKENDS

    expected = {
        "claude": ["claude", "--print", "--output-format", "json", "--model",
                   "qwen3.8-max", "--max-turns", "30", prompt],
        "aider": ["aider", "--yes", "--no-auto-commits", "--model",
                  "openai/qwen3.8-max", "--architect-model", "openai/qwen3.6-flash",
                  "--message", prompt],
        "codex": ["codex", "exec", "-s", "workspace-write", "-C", cwd,
                  "--output-last-message", os.path.join(cwd, ".codex_last_message.txt"), prompt],
    }
    descriptions = {
        "claude": "Claude Code CLI — 自主编码 Agent (Token Plan qwen3.8-max)",
        "aider": "Aider — Git-aware 双模型编码 Agent (Token Plan 零成本)",
        "codex": "OpenAI Codex CLI - 沙箱编码 Agent (exec 非交互模式)",
    }
    assert registry.names() == list(expected)
    assert list(CLI_AGENT_BACKENDS) == list(expected)
    for name, argv in expected.items():
        for legacy in (registry.legacy_specs(), CLI_AGENT_BACKENDS):
            assert set(legacy[name]) == {"detect", "build_cmd", "timeout", "description"}
            assert legacy[name]["build_cmd"](prompt, cwd) == argv
            assert legacy[name]["detect"] == name
            assert legacy[name]["timeout"] == 600
            assert legacy[name]["description"] == descriptions[name]


@pytest.mark.parametrize("name,caps", [
    ("claude", {Cap.SYS_PROMPT, Cap.TOOL_ALLOW, Cap.USD}),
    ("aider", {Cap.SYS_PROMPT}),
    ("codex", {Cap.MODEL_SWAP, Cap.SANDBOX}),
])
def test_capabilities_and_preparation(name: str, caps: set[Cap], monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = registry.get(name)
    assert registry.capabilities(name) == frozenset(caps)
    assert all(isinstance(key, str) for key in adapter.spec.env_keys)
    with pytest.raises(FrozenInstanceError):
        adapter.spec.timeout = 1
    monkeypatch.setattr("lan_mesh.harness.adapter.shutil.which", lambda cmd: None)
    assert not adapter.detect()
    monkeypatch.setattr("lan_mesh.harness.adapter.shutil.which", lambda cmd: "/bin/" + cmd)
    assert adapter.detect()
    env = {"PATH": "example"}
    prepared = adapter.prepare({"prompt": "p", "cwd": "c", "env": env})
    assert prepared == {"argv": adapter.spec.build_cmd("p", "c"), "cwd": "c", "env": env}
    prepared["env"]["PATH"] = "changed"
    assert env["PATH"] == "example"
    assert adapter.prepare({"prompt": "p", "cwd": "c"})["env"] == {}


@pytest.mark.parametrize("code,stderr,expected", [
    (127, "", "not_installed"), (1, "command not found", "not_installed"),
    (1, "USAGE LIMIT", "usage_limit"), (1, "rate limit", "usage_limit"),
    (1, "authentication failed", "auth"), (1, "please login", "auth"),
    (1, "HTTP 401", "auth"), (1, "ConnectionError", "network"),
    (1, "connection timed out", "network"), (1, "network unavailable", "network"),
    (1, "sandbox denied", "sandbox"), (1, "other failure", "unknown"),
])
def test_error_classification(code: int, stderr: str, expected: str) -> None:
    adapter = registry.get("codex")
    outcome = adapter.parse("partial output", stderr, code)
    assert outcome.status == "failed"
    assert outcome.result == "partial output"
    assert outcome.error_class == expected
    assert adapter.classify_error(code, stderr, outcome) == expected


def test_plain_output_does_not_invent_telemetry() -> None:
    outcome = registry.get("claude").parse("raw output", "", 0)
    assert outcome.status == "completed"
    assert outcome.result == "raw output"
    assert outcome.error_class == ""
    assert outcome.usage == {"input_tokens": 0, "output_tokens": 0}
    assert not outcome.usage_known
    assert outcome.cost_usd == 0.0
    assert outcome.cost_estimated
    assert outcome.session_id == ""
    assert outcome.elapsed_s == 0.0
    assert "usage_unavailable" in outcome.degraded
    assert HarnessEvent("output", 1, 0.5, {"text": "p"}).seq == 1


def test_registry_extension_and_legacy_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_SPECS", dict(registry._SPECS))
    snapshot = registry.legacy_specs()
    snapshot["claude"]["timeout"] = 1
    snapshot.pop("codex")
    assert registry.get("claude").spec.timeout == 600
    assert "codex" in registry.names()
    spec = HarnessSpec("test", "test", lambda prompt, cwd: ["test", prompt], 10, "test")
    registry.register(spec)
    assert registry.get("test").spec is spec
    assert registry.capabilities("test") == frozenset()
    assert registry.legacy_specs()["test"]["build_cmd"]("p", "c") == ["test", "p"]
    with pytest.raises(KeyError):
        registry.get("unknown")
