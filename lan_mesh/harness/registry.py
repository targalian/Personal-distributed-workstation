"""Registry of CLI harness specs and the legacy runtime compatibility view."""

import os

from .adapter import HarnessAdapter, LegacyAdapter
from .spec import Cap, HarnessSpec

_SPECS: dict[str, HarnessSpec] = {}


def register(spec: HarnessSpec) -> None:
    """Register or replace a backend by its unique name."""
    _SPECS[spec.name] = spec


def names() -> list[str]:
    """Return backend names in registration order."""
    return list(_SPECS)


def get(name: str) -> HarnessAdapter:
    """Return an adapter, raising KeyError for an unknown backend."""
    return LegacyAdapter(_SPECS[name])


def legacy_specs() -> dict[str, dict]:
    """Return fresh dictionaries containing exactly the four legacy fields."""
    return {
        name: {
            "detect": spec.detect,
            "build_cmd": spec.build_cmd,
            "timeout": spec.timeout,
            "description": spec.description,
        }
        for name, spec in _SPECS.items()
    }


def capabilities(name: str) -> frozenset[Cap]:
    """Return the explicitly declared capabilities of a registered backend."""
    return _SPECS[name].capabilities


register(HarnessSpec(
    name='claude',
    detect="claude",
    build_cmd=lambda prompt, cwd: [
        "claude", "--print", "--output-format", "json",
        "--model", "qwen3.8-max",
        "--max-turns", "30", prompt,
    ],
    timeout=600,
    description="Claude Code CLI — 自主编码 Agent (Token Plan qwen3.8-max)",
    capabilities=frozenset({Cap.SYS_PROMPT, Cap.TOOL_ALLOW, Cap.USD}),
    env_keys=frozenset({
        'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN',
        'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
    }),
))


register(HarnessSpec(
    name='aider',
    detect="aider",
    build_cmd=lambda prompt, cwd: [
        "aider", "--yes", "--no-auto-commits",
        "--model", "openai/qwen3.8-max",
        "--architect-model", "openai/qwen3.6-flash",
        "--message", prompt,
    ],
    timeout=600,
    description="Aider — Git-aware 双模型编码 Agent (Token Plan 零成本)",
    capabilities=frozenset({Cap.SYS_PROMPT}),
    env_keys=frozenset(['OPENAI_API_KEY', 'OPENAI_API_BASE', 'OPENAI_BASE_URL']),
))


register(HarnessSpec(
    name='codex',
    detect="codex",
    build_cmd=lambda prompt, cwd: [
        "codex", "exec",
        "-s", "workspace-write",
        "-C", cwd,
        "--output-last-message", os.path.join(cwd, ".codex_last_message.txt"),
        prompt,
    ],
    timeout=600,
    description="OpenAI Codex CLI - 沙箱编码 Agent (exec 非交互模式)",
    capabilities=frozenset({Cap.MODEL_SWAP, Cap.SANDBOX}),
    env_keys=frozenset(['OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_HOME']),
))
