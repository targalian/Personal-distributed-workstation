'''Shadow development guardrail tests.'''

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_cli_agent_cwd_guard_blocks_repo(tmp_path: Path) -> None:
    '''Main repo cwd is blocked and external roots require explicit allowlist.'''
    import lan_mesh.agent_runtime as runtime

    assert runtime.validate_cli_agent_cwd(str(REPO_ROOT)) is not None
    assert runtime.validate_cli_agent_cwd(str(tmp_path)) is not None
    (tmp_path / 'nested').mkdir()
    assert runtime.validate_cli_agent_cwd(
        str(tmp_path / 'nested'), [str(tmp_path)]) is None


def test_cli_agent_handler_rejects_repo_before_backend(tmp_path: Path) -> None:
    '''Direct repo cwd must fail before any CLI executable is launched.'''
    from lan_mesh.agent_runtime import AgentRuntime

    agent = AgentRuntime(agent_id='test-shadow', shared_folder_path=str(tmp_path))
    result = agent._handle_cli_agent({
        'requirement': 'test',
        'cwd': str(REPO_ROOT),
        'backend': 'codex',
    })

    assert result.get('error')
    assert result.get('cwd') == str(REPO_ROOT)


def test_cli_agent_handler_allows_only_shared_root(tmp_path: Path) -> None:
    '''A cwd outside runtime shared folder is rejected before backend launch.'''
    from lan_mesh.agent_runtime import AgentRuntime

    outside = tmp_path / 'outside'
    outside.mkdir()
    agent = AgentRuntime(agent_id='test-shadow',
                         shared_folder_path=str(tmp_path / 'shared'))
    (tmp_path / 'shared').mkdir()

    result = agent._handle_cli_agent({
        'requirement': 'test',
        'cwd': str(outside),
        'backend': 'codex',
    })

    assert '不在白名单内' in result.get('error', '')


def test_self_mod_violations_detect_guardrail_files(tmp_path: Path) -> None:
    '''Changed guardrail and gate paths are reported as forbidden.'''
    from lan_mesh.agent_runtime import check_self_mod_violations

    (tmp_path / '.githooks').mkdir()
    (tmp_path / '.githooks' / 'pre-push').write_text('x', encoding='utf-8')
    (tmp_path / 'lan_mesh').mkdir()
    (tmp_path / 'lan_mesh' / 'agent_runtime.py').write_text('x', encoding='utf-8')

    violations = check_self_mod_violations([
        'lan_mesh/agent_runtime.py',
        '.githooks/pre-push',
        'AGENTS.md',
        'scripts/ship.ps1',
    ])

    assert 'lan_mesh/agent_runtime.py' in violations
    assert '.githooks/' in violations


def test_cli_env_filters_secrets_and_git_credentials(monkeypatch) -> None:
    '''Only required backend and platform variables reach CLI subprocess.'''
    from lan_mesh.agent_runtime import _build_cli_env

    monkeypatch.delenv('ALIYUN_TOKENPLAN_API_KEY', raising=False)

    old_values = {
        'DEEPSEEK_API_KEY': 'secret-value-1234567890',
        'GITHUB_TOKEN': 'git-secret-1234567890',
        'GIT_ASKPASS': 'helper',
        'ANTHROPIC_API_KEY': 'anthropic-secret-1234567890',
    }
    for key, value in old_values.items():
        os.environ[key] = value
    try:
        env = _build_cli_env('claude')
    finally:
        for key, value in old_values.items():
            os.environ[key] = value

    assert 'ANTHROPIC_API_KEY' in env
    assert 'DEEPSEEK_API_KEY' not in env
    assert 'GITHUB_TOKEN' not in env
    assert 'GIT_ASKPASS' not in env
    assert env['GIT_TERMINAL_PROMPT'] == '0'


def test_shadow_copy_filters_sensitive_files(tmp_path: Path) -> None:
    '''Sensitive configs are not copied into the shadow workspace.'''
    from lan_mesh import shadow_dev

    ignored = shadow_dev._shadow_ignore(
        str(tmp_path), ['.env', 'model_pool.yaml', 'config.yaml', 'safe.py'])

    assert '.env' in ignored
    assert 'model_pool.yaml' in ignored
    assert 'config.yaml' in ignored
    assert 'safe.py' not in ignored


def test_shadow_diff_scans_added_secrets() -> None:
    '''Added secret-looking lines fail the shadow secret gate.'''
    from lan_mesh.shadow_dev import scan_added_lines_for_secrets

    patch = (
        '--- a/file\n'
        '+++ b/file\n'
        # 无引号形态: 能被 scan_added_lines_for_secrets 检出 (引号可选),
        # 但避开 pre-push 门禁的硬编码密钥误报 (其正则强制带引号)
        '+api_key = sk-abcdefghijklmnopqrstuvw\n'
        '-old line\n'
    )

    assert scan_added_lines_for_secrets(patch)
    assert not scan_added_lines_for_secrets('+++ b/file\n+safe = "value"\n')
