"""RuntimeCliService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import agent_runtime as policy
from .component_service import ComponentService

class RuntimeCliService(ComponentService):
    """RuntimeCliService business operations with explicit state ownership."""
    OPERATIONS = ('_handle_cli_agent', '_parse_cli_output', 'list_cli_agents')

    def _handle_cli_agent(self, input_data: dict) -> dict:
        """调用外部 CLI Agent 自主完成编码任务。

        与单轮 LLM API 调用不同, CLI Agent 能够:
        - 自主探索代码库、读取文件
        - 编辑多个文件、创建新文件
        - 运行命令、执行测试
        - 迭代修复错误直到任务完成

        支持后端: claude (Claude Code) / codex / aider
        通过环境变量 CLI_AGENT_BACKEND 指定, 或自动检测。

        input_data 字段:
            requirement/description: 任务描述
            cwd: 工作目录 (默认 shared_folder)
            backend: 强制指定后端 (可选)
            timeout: 超时秒数 (可选)
            allowed_tools: 允许的工具列表 (claude 专用, 可选)
        """
        requirement = input_data.get('requirement', input_data.get('description', ''))
        if not requirement:
            return {'error': '未提供任务描述 (requirement)'}
        cwd = input_data.get('cwd', self.owner.shared_folder)
        timeout = min(input_data.get('timeout', policy.CLI_AGENT_TIMEOUT), 1800)
        allowed_roots = [self.owner.shared_folder, *policy.CLI_AGENT_ALLOWED_ROOTS]
        reject = policy.validate_cli_agent_cwd(cwd, allowed_roots)
        if reject:
            policy.logger.warning('[CLI Agent] cwd 校验拒绝: %s', reject)
            return {'error': reject, 'cwd': cwd, 'status': 'failed'}
        backend, backend_err = policy.resolve_cli_backend(input_data.get('backend', ''))
        if backend_err:
            return backend_err
        cfg = policy.CLI_AGENT_BACKENDS[backend]
        policy.logger.info('[CLI Agent] 后端=%s, cwd=%s, timeout=%ds', backend, cwd, timeout)
        policy.logger.info('[CLI Agent] 任务: %s', requirement[:200])
        cmd = self._build_cli_command(cfg, backend, requirement, cwd, input_data)
        from .harness import registry
        from . import runtime_usage
        adapter = registry.get(backend)
        request = dict(argv=cmd, cwd=cwd, env=policy._build_cli_env(backend), timeout=timeout,
                       cancel_event=self.owner._cancel_event,
                       budget_usd=input_data.get('budget_usd'), max_output=policy.CLI_AGENT_MAX_OUTPUT)
        started = policy.time.monotonic()
        output = {}
        for event in adapter.stream(request):
            if event.kind == 'run.completed':
                output = event.data
        output['backend'] = backend
        usage = output.get('usage', {})
        runtime_usage.record_external_usage(
            owner=self.owner, model_id=f'cli:{backend}',
            input_tokens=usage.get('input_tokens', 0), output_tokens=usage.get('output_tokens', 0),
            call_type='cli', outcome=output['status'], usage_known=output['usage_known'], started=started)
        print(f"[AgentRuntime] CLI {backend}: {output['status']} ({output['elapsed_secs']}s)")
        return output

    def _build_cli_command(self, cfg: dict, backend: str, requirement: str, cwd: str, input_data: dict) -> list:
        """Compose backend arguments while retaining configured safety policy."""
        cmd = cfg['build_cmd'](requirement, cwd)
        resolved_exe = policy._which_with_fallback(cfg['detect'])
        if resolved_exe:
            cmd[0] = resolved_exe
        if backend == 'codex':
            cmd.insert(2, '--json')
        if backend == 'claude' and '--output-format' in cmd:
            cmd[cmd.index('--output-format') + 1] = 'stream-json'
            cmd.insert(-1, '--verbose')
        if backend == 'claude':
            allowed_tools = input_data.get('allowed_tools', [])
            if allowed_tools:
                for tool in allowed_tools:
                    cmd.insert(-1, f'--allowedTools={tool}')
            system_prompt = self.owner._build_system_prompt(requirement)
            if system_prompt and len(system_prompt) < 2000:
                cmd.insert(-1, f'--system-prompt={system_prompt[:1500]}')
        return cmd

    def _parse_cli_output(self, backend: str, stdout: str, stderr: str, returncode: int) -> dict:
        """解析 CLI Agent 的输出。

        Claude Code 使用 --output-format json 时输出 JSON 结构:
        {"type": "result", "result": "...", "cost_usd": 0.05, ...}
        """
        if backend == 'claude' and stdout.strip():
            try:
                data = policy._json.loads(stdout)
                if isinstance(data, dict):
                    return {'result': data.get('result', stdout), 'cost_usd': data.get('cost_usd', 0), 'duration_ms': data.get('duration_ms', 0), 'num_turns': data.get('num_turns', 0), 'session_id': data.get('session_id', ''), 'status': 'completed' if returncode == 0 else 'failed'}
            except policy._json.JSONDecodeError:
                pass
        return {'result': stdout if stdout else stderr, 'status': 'completed' if returncode == 0 else 'failed', 'stderr': stderr[:2000] if stderr else ''}

    def list_cli_agents(self) -> dict:
        """列出可用的 CLI Agent 后端及其状态。"""
        agents = []
        for name, cfg in policy.CLI_AGENT_BACKENDS.items():
            path = policy._which_with_fallback(cfg['detect'])
            installed = path is not None
            agents.append({'name': name, 'description': cfg['description'], 'installed': installed, 'path': path or ''})
        preferred = policy.get_preferred_cli_agent()
        return {'agents': agents, 'preferred': preferred, 'timeout': policy.CLI_AGENT_TIMEOUT}
